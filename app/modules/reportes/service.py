"""
Service layer — módulo reportes

Cálculo de utilidad real e estimada por venta.

PRINCIPIOS:
1. Costo histórico inmutable: reconstruimos desde MovimientoStock, NO desde Lote actual.
   Si alguien edita Lote.costo_unitario, las ventas viejas no se afectan.
2. Legacy = venta con AL MENOS UN movimiento de salida sin lote_id o sin costo_unitario.
   En ese caso, cost_real/profit/margin a nivel venta son NULL (Opción A confirmada).
3. Descuento se prorratea proporcionalmente al subtotal de cada línea (Opción 1 confirmada).
4. Utilidad = total (post-descuento) - cost_real, no subtotal - cost_real (decision: total).
5. Decimal exacto en backend, sin redondeo. UI redondea para mostrar.
"""

from decimal import Decimal, ROUND_HALF_UP
from collections import defaultdict

from sqlalchemy import and_
from sqlalchemy.orm import Session, joinedload

from app.modules.ventas.models import Venta, DetalleVenta
from app.modules.inventario.models import MovimientoStock, Variante, Producto


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
ZERO = Decimal("0")
HUNDRED = Decimal("100")
TWO_PLACES = Decimal("0.01")


def _safe(value: Decimal | None) -> Decimal:
    """Defensa contra columnas nullable. Convierte None a Decimal('0')."""
    return value if value is not None else ZERO


def _round_2(value: Decimal) -> Decimal:
    """Redondeo bancario a 2 decimales para campos monetarios de salida."""
    return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Carga de datos: venta + detalles + movimientos por línea
# ---------------------------------------------------------------------------
def _cargar_venta_completa(db: Session, negocio_id: str, venta_id: str) -> Venta | None:
    """
    Carga venta con detalles y los datos de variante/producto necesarios.
    No carga movimientos acá — los traemos en query separada por eficiencia.
    """
    return (
        db.query(Venta)
        .options(joinedload(Venta.detalles))
        .filter(
            Venta.id == venta_id,
            Venta.negocio_id == negocio_id,
        )
        .first()
    )


def _cargar_movimientos_de_venta(
    db: Session, venta_numero: int, almacen_id: str,
) -> dict[str, list[MovimientoStock]]:
    """
    Trae todos los SALIDA_VENTA de la venta agrupados por variante_id.

    Devuelve: {variante_id: [movimiento1, movimiento2, ...]}

    Una línea de venta puede generar N movimientos por FEFO repartido.
    """
    movs = (
        db.query(MovimientoStock)
        .filter(
            MovimientoStock.referencia_id == str(venta_numero),
            MovimientoStock.tipo == "SALIDA_VENTA",
            MovimientoStock.almacen_id == almacen_id,
        )
        .all()
    )

    agrupado: dict[str, list[MovimientoStock]] = defaultdict(list)
    for m in movs:
        agrupado[m.variante_id].append(m)
    return dict(agrupado)


def _cargar_metadata_variantes(
    db: Session, variante_ids: list[str],
) -> dict[str, dict]:
    """
    Trae nombre del producto y SKU de cada variante.
    Devuelve: {variante_id: {"producto_nombre": ..., "variante_sku": ...}}
    """
    if not variante_ids:
        return {}

    rows = (
        db.query(Variante.id, Variante.sku, Producto.nombre)
        .join(Producto, Producto.id == Variante.producto_id)
        .filter(Variante.id.in_(variante_ids))
        .all()
    )
    return {
        var_id: {"producto_nombre": prod_nombre, "variante_sku": sku}
        for var_id, sku, prod_nombre in rows
    }


# ---------------------------------------------------------------------------
# Detección de legacy
# ---------------------------------------------------------------------------
def _movimientos_son_legacy(movs: list[MovimientoStock]) -> bool:
    """
    Una lista de movimientos es legacy si CUALQUIERA de ellos tiene
    lote_id NULL o costo_unitario NULL.
    """
    for m in movs:
        if m.lote_id is None or m.costo_unitario is None:
            return True
    return False


# ---------------------------------------------------------------------------
# Prorrateo de descuento
# ---------------------------------------------------------------------------
def _calcular_descuentos_proporcionales(
    detalles: list[DetalleVenta], descuento_total: Decimal,
) -> dict[str, Decimal]:
    """
    Prorratea el descuento de la venta entre las líneas según su subtotal.

    Devuelve: {detalle_id: descuento_atribuido_a_esta_linea}

    Usa Decimal para precision. La suma de los prorrateos debe igualar
    descuento_total exactamente — para evitar errores de redondeo, la última
    línea absorbe la diferencia residual.
    """
    if descuento_total <= ZERO or not detalles:
        return {d.id: ZERO for d in detalles}

    subtotales = {d.id: _safe(d.subtotal) for d in detalles}
    suma_subtotales = sum(subtotales.values())

    if suma_subtotales <= ZERO:
        # Caso defensivo: no podemos prorratear por subtotales 0
        # Repartimos en partes iguales (raro pero posible si todo es regalo)
        n = len(detalles)
        parte = descuento_total / Decimal(n)
        result = {d.id: parte for d in detalles}
    else:
        result = {}
        for d in detalles:
            proporcion = subtotales[d.id] / suma_subtotales
            result[d.id] = (descuento_total * proporcion)

    # Ajuste residual: la suma debe ser exacta
    suma_calculada = sum(result.values())
    diferencia = descuento_total - suma_calculada
    if diferencia != ZERO and detalles:
        ultimo_id = detalles[-1].id
        result[ultimo_id] = result[ultimo_id] + diferencia

    return result


# ---------------------------------------------------------------------------
# Cálculo principal — utilidad REAL
# ---------------------------------------------------------------------------
def calcular_utilidad_venta(
    db: Session, negocio_id: str, venta_id: str,
) -> dict | None:
    """
    Calcula utilidad real de una venta usando costo de lote.

    Devuelve dict con la estructura UtilidadVentaResponse, o None si la venta
    no existe o no pertenece al negocio.
    """
    venta = _cargar_venta_completa(db, negocio_id, venta_id)
    if not venta:
        return None

    descuento = _safe(venta.descuento)
    subtotal = _safe(venta.subtotal)
    revenue = _safe(venta.total)

    movs_por_variante = _cargar_movimientos_de_venta(
        db, venta.numero, venta.almacen_id,
    )

    var_ids = [d.variante_id for d in venta.detalles]
    metadata = _cargar_metadata_variantes(db, var_ids)

    # Prorrateo de descuento
    descuentos_prop = _calcular_descuentos_proporcionales(venta.detalles, descuento)

    # Procesar cada línea
    detalle_out: list[dict] = []
    venta_es_legacy = False
    cost_real_total = ZERO

    for d in venta.detalles:
        movs = movs_por_variante.get(d.variante_id, [])
        meta = metadata.get(d.variante_id, {})

        # Una línea puede no tener movimientos si el producto era servicio.
        # En ese caso no es legacy, simplemente no aporta a cost_real.
        if not movs:
            es_servicio_o_sin_stock = True
            linea_legacy = False
            cost_real_linea = ZERO
            lotes_consumidos = []
        else:
            es_servicio_o_sin_stock = False
            linea_legacy = _movimientos_son_legacy(movs)

            if linea_legacy:
                venta_es_legacy = True
                cost_real_linea = None
                # Aún mostramos los lote_id que sí tengan datos (los que no, los omitimos)
                lotes_consumidos = []
                for m in movs:
                    if m.lote_id is not None and m.costo_unitario is not None:
                        lotes_consumidos.append({
                            "lote_id": m.lote_id,
                            "qty": abs(m.cantidad),  # mov.cantidad es negativo en salidas
                            "cost_unit": m.costo_unitario,
                        })
            else:
                lotes_consumidos = [
                    {
                        "lote_id": m.lote_id,
                        "qty": abs(m.cantidad),
                        "cost_unit": m.costo_unitario,
                    }
                    for m in movs
                ]
                cost_real_linea = sum(
                    (lc["qty"] * lc["cost_unit"] for lc in lotes_consumidos),
                    ZERO,
                )
                cost_real_total += cost_real_linea

        revenue_bruto = _safe(d.subtotal)
        descuento_prop = descuentos_prop.get(d.id, ZERO)
        revenue_neto = revenue_bruto - descuento_prop

        if cost_real_linea is None:
            profit_linea = None
        else:
            profit_linea = revenue_neto - cost_real_linea

        detalle_out.append({
            "variante_id": d.variante_id,
            "producto_nombre": meta.get("producto_nombre", "(producto eliminado)"),
            "variante_sku": meta.get("variante_sku"),
            "cantidad": _safe(d.cantidad),
            "precio_unitario": _safe(d.precio_unitario),
            "descuento_linea": _safe(d.descuento_linea),
            "revenue_bruto": _round_2(revenue_bruto),
            "descuento_proporcional": _round_2(descuento_prop),
            "revenue_neto": _round_2(revenue_neto),
            "cost_real": _round_2(cost_real_linea) if cost_real_linea is not None else None,
            "profit": _round_2(profit_linea) if profit_linea is not None else None,
            "lotes_consumidos": lotes_consumidos,
        })

    # Cálculos a nivel venta
    if venta_es_legacy:
        cost_real_response = None
        profit = None
        margin = None
    else:
        cost_real_response = _round_2(cost_real_total)
        profit = revenue - cost_real_total
        if revenue > ZERO:
            margin = _round_2((profit / revenue) * HUNDRED)
        else:
            margin = ZERO
        profit = _round_2(profit)

    return {
        "venta_id": venta.id,
        "venta_numero": venta.numero,
        "fecha": venta.created_at,
        "estado": venta.estado,
        "legacy": venta_es_legacy,
        "revenue": _round_2(revenue),
        "subtotal": _round_2(subtotal),
        "descuento": _round_2(descuento),
        "cost_real": cost_real_response,
        "profit": profit,
        "margin": margin,
        "detalle": detalle_out,
    }


# ---------------------------------------------------------------------------
# Cálculo legacy estimado — usa DetalleVenta.costo_unitario congelado
# ---------------------------------------------------------------------------
def calcular_utilidad_legacy_estimada(
    db: Session, negocio_id: str, venta_id: str,
) -> dict | None:
    """
    Calcula utilidad estimada para ventas legacy usando el costo congelado
    en DetalleVenta al momento de la venta.

    Útil cuando los movimientos de inventario no tienen lote_id pero el
    DetalleVenta sí tiene costo_unitario porque era el precio_costo de la
    variante en ese momento.

    Devuelve dict con la estructura UtilidadLegacyEstimadaResponse, o None
    si la venta no existe.
    """
    venta = _cargar_venta_completa(db, negocio_id, venta_id)
    if not venta:
        return None

    descuento = _safe(venta.descuento)
    subtotal = _safe(venta.subtotal)
    revenue = _safe(venta.total)

    # Cargamos movimientos solo para enriquecer lotes_consumidos cuando los hay
    movs_por_variante = _cargar_movimientos_de_venta(
        db, venta.numero, venta.almacen_id,
    )

    var_ids = [d.variante_id for d in venta.detalles]
    metadata = _cargar_metadata_variantes(db, var_ids)

    descuentos_prop = _calcular_descuentos_proporcionales(venta.detalles, descuento)

    detalle_out: list[dict] = []
    cost_estimado_total = ZERO
    todas_lineas_estimables = True

    for d in venta.detalles:
        meta = metadata.get(d.variante_id, {})
        movs = movs_por_variante.get(d.variante_id, [])

        revenue_bruto = _safe(d.subtotal)
        descuento_prop = descuentos_prop.get(d.id, ZERO)
        revenue_neto = revenue_bruto - descuento_prop

        # Construir lotes_consumidos con datos disponibles (puede ser parcial)
        lotes_consumidos = [
            {
                "lote_id": m.lote_id,  # puede ser None
                "qty": abs(m.cantidad),
                "cost_unit": m.costo_unitario,  # puede ser None
            }
            for m in movs
        ]

        # Cost estimado: prefiere DetalleVenta.costo_unitario congelado
        if d.costo_unitario is not None:
            cost_unit_estimado = d.costo_unitario
            cost_estimado_linea = cost_unit_estimado * _safe(d.cantidad)
            profit_estimado_linea = revenue_neto - cost_estimado_linea
            cost_estimado_total += cost_estimado_linea
        else:
            cost_estimado_linea = None
            profit_estimado_linea = None
            todas_lineas_estimables = False

        detalle_out.append({
            "variante_id": d.variante_id,
            "producto_nombre": meta.get("producto_nombre", "(producto eliminado)"),
            "variante_sku": meta.get("variante_sku"),
            "cantidad": _safe(d.cantidad),
            "precio_unitario": _safe(d.precio_unitario),
            "descuento_linea": _safe(d.descuento_linea),
            "revenue_bruto": _round_2(revenue_bruto),
            "descuento_proporcional": _round_2(descuento_prop),
            "revenue_neto": _round_2(revenue_neto),
            "cost_estimado": _round_2(cost_estimado_linea) if cost_estimado_linea is not None else None,
            "profit_estimado": _round_2(profit_estimado_linea) if profit_estimado_linea is not None else None,
            "lotes_consumidos": lotes_consumidos,
        })

    if todas_lineas_estimables:
        cost_estimado_response = _round_2(cost_estimado_total)
        profit_estimado = revenue - cost_estimado_total
        if revenue > ZERO:
            margin_estimado = _round_2((profit_estimado / revenue) * HUNDRED)
        else:
            margin_estimado = ZERO
        profit_estimado = _round_2(profit_estimado)
    else:
        cost_estimado_response = None
        profit_estimado = None
        margin_estimado = None

    return {
        "venta_id": venta.id,
        "venta_numero": venta.numero,
        "fecha": venta.created_at,
        "estado": venta.estado,
        "revenue": _round_2(revenue),
        "subtotal": _round_2(subtotal),
        "descuento": _round_2(descuento),
        "cost_estimado": cost_estimado_response,
        "profit_estimado": profit_estimado,
        "margin_estimado": margin_estimado,
        "detalle": detalle_out,
        "advertencia": (
            "Esta utilidad usa el costo congelado en la venta, no el costo real "
            "del lote consumido. Para utilidad real ver /reportes/utilidad-venta/{id}."
        ),
    }
