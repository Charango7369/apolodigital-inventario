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


# ===========================================================================
# A.2 — Reportes agregados (utilidad-periodo y utilidad-por-producto)
# ===========================================================================

from datetime import date, datetime, timedelta

from sqlalchemy import text


# ---------------------------------------------------------------------------
# Constantes y validaciones
# ---------------------------------------------------------------------------
RANGO_MAXIMO_DIAS = 365
UMBRAL_GRANULARIDAD_DIA = 90  # > este valor, agrupamos por mes


def _validar_rango(desde: date, hasta: date) -> None:
    """Lanza ValueError si el rango es inválido."""
    if desde > hasta:
        raise ValueError("desde debe ser <= hasta")
    dias = (hasta - desde).days
    if dias > RANGO_MAXIMO_DIAS:
        raise ValueError(
            f"Rango maximo {RANGO_MAXIMO_DIAS} dias. Pediste {dias} dias. "
            f"Para analisis multi-anual hay que usar otro endpoint (futuro)."
        )


def _granularidad_para(desde: date, hasta: date) -> str:
    """Devuelve 'dia' si rango <= 90, 'mes' si > 90."""
    return "dia" if (hasta - desde).days <= UMBRAL_GRANULARIDAD_DIA else "mes"


# ---------------------------------------------------------------------------
# Identificar ventas legacy en bloque (vs llamar al endpoint individual)
# ---------------------------------------------------------------------------
def _ids_ventas_legacy(
    db: Session, negocio_id: str, desde: datetime, hasta: datetime,
) -> set[str]:
    """
    Devuelve los IDs de ventas COMPLETADA en el periodo cuyos movimientos
    tienen al menos un lote_id NULL o costo_unitario NULL.
    Usa SQL puro por eficiencia.

    Nota: una venta puede tener movimientos perfectos Y movimientos legacy.
    Si CUALQUIERA es legacy, toda la venta queda marcada como legacy
    (Opcion A confirmada en A.1).
    """
    sql = text("""
        SELECT DISTINCT v.id
        FROM ventas v
        JOIN movimientos_stock m
          ON m.referencia_id = CAST(v.numero AS TEXT)
         AND m.almacen_id = v.almacen_id
         AND m.tipo = 'SALIDA_VENTA'
        WHERE v.negocio_id = :negocio_id
          AND v.estado = 'COMPLETADA'
          AND v.completed_at >= :desde
          AND v.completed_at <= :hasta
          AND (m.lote_id IS NULL OR m.costo_unitario IS NULL)
    """)
    rows = db.execute(sql, {
        "negocio_id": negocio_id,
        "desde": desde,
        "hasta": hasta,
    }).fetchall()
    return {row[0] for row in rows}


# ---------------------------------------------------------------------------
# Bloque de canceladas (informativo, Opcion B)
# ---------------------------------------------------------------------------
def _info_canceladas(
    db: Session, negocio_id: str, desde: datetime, hasta: datetime,
) -> dict:
    """
    Cuenta y suma de ventas CANCELADAS por cancelled_at en el periodo.
    """
    sql = text("""
        SELECT
            COUNT(*) AS cnt,
            COALESCE(SUM(total), 0) AS monto
        FROM ventas
        WHERE negocio_id = :negocio_id
          AND estado = 'CANCELADA'
          AND cancelled_at >= :desde
          AND cancelled_at <= :hasta
    """)
    row = db.execute(sql, {
        "negocio_id": negocio_id,
        "desde": desde,
        "hasta": hasta,
    }).fetchone()
    return {
        "count": row[0],
        "monto_cancelado": _round_2(_safe(row[1])),
    }


# ---------------------------------------------------------------------------
# Endpoint principal: utilidad-periodo
# ---------------------------------------------------------------------------
def calcular_utilidad_periodo(
    db: Session, negocio_id: str, desde: date, hasta: date,
) -> dict:
    """
    Calcula utilidad agregada del periodo con breakdown temporal.

    REGLAS (cerradas con stakeholder en A.2):
    - Solo COMPLETADA, filtrado por completed_at
    - Excluye ventas legacy del calculo (las cuenta aparte)
    - Bloque informativo de canceladas (cancelled_at en periodo)
    - Granularidad: dia si <=90 dias, mes si > 90 dias
    - Rango max: 365 dias

    Devuelve dict con estructura UtilidadPeriodoResponse.
    """
    _validar_rango(desde, hasta)
    granularidad = _granularidad_para(desde, hasta)

    # Convertir a datetime con bordes [00:00:00, 23:59:59.999999]
    desde_dt = datetime.combine(desde, datetime.min.time())
    hasta_dt = datetime.combine(hasta, datetime.max.time())

    legacy_ids = _ids_ventas_legacy(db, negocio_id, desde_dt, hasta_dt)

    # ---- Totales del periodo (excluyendo legacy) ----
    # Revenue: SUM(ventas.total)
    # Cost: SUM(|movimiento.cantidad| * movimiento.costo_unitario) por las salidas
    #
    # IMPORTANTE: ventas.total ya incluye el descuento aplicado.
    # cost_real lo calculamos desde movimientos sin tocar.
    #
    # Filtramos legacy con NOT IN para no contaminar el cálculo.

    legacy_filter_sql = ""
    legacy_params = {}
    if legacy_ids:
        # Postgres acepta tuple expansion con SQLAlchemy text(), pero hay que
        # usar parametros nominales para list.
        legacy_list = list(legacy_ids)
        legacy_filter_sql = "AND v.id != ALL(:legacy_ids)"
        legacy_params["legacy_ids"] = legacy_list

    # Revenue: agregamos por venta para no duplicar por joins múltiples
    sql_revenue = text(f"""
        SELECT
            COUNT(*) AS ventas_count,
            COALESCE(SUM(v.total), 0) AS revenue
        FROM ventas v
        WHERE v.negocio_id = :negocio_id
          AND v.estado = 'COMPLETADA'
          AND v.completed_at >= :desde
          AND v.completed_at <= :hasta
          {legacy_filter_sql}
    """)
    row_rev = db.execute(sql_revenue, {
        "negocio_id": negocio_id,
        "desde": desde_dt,
        "hasta": hasta_dt,
        **legacy_params,
    }).fetchone()
    ventas_count = row_rev[0]
    revenue = _safe(row_rev[1])

    # Cost real: SUM sobre movimientos de las ventas no-legacy
    sql_cost = text(f"""
        SELECT COALESCE(SUM(ABS(m.cantidad) * m.costo_unitario), 0) AS cost
        FROM ventas v
        JOIN movimientos_stock m
          ON m.referencia_id = CAST(v.numero AS TEXT)
         AND m.almacen_id = v.almacen_id
         AND m.tipo = 'SALIDA_VENTA'
        WHERE v.negocio_id = :negocio_id
          AND v.estado = 'COMPLETADA'
          AND v.completed_at >= :desde
          AND v.completed_at <= :hasta
          {legacy_filter_sql}
    """)
    row_cost = db.execute(sql_cost, {
        "negocio_id": negocio_id,
        "desde": desde_dt,
        "hasta": hasta_dt,
        **legacy_params,
    }).fetchone()
    cost_real = _safe(row_cost[0])

    profit = revenue - cost_real
    if revenue > ZERO:
        margin = (profit / revenue) * HUNDRED
    else:
        margin = ZERO

    # ---- Bloque informativo legacy ----
    sql_legacy_info = text("""
        SELECT
            COUNT(*) AS cnt,
            COALESCE(SUM(total), 0) AS rev_excluido
        FROM ventas
        WHERE id = ANY(:ids)
    """)
    if legacy_ids:
        row_leg = db.execute(sql_legacy_info, {"ids": list(legacy_ids)}).fetchone()
        legacy_info = {
            "count": row_leg[0],
            "revenue_excluido": _round_2(_safe(row_leg[1])),
        }
    else:
        legacy_info = {"count": 0, "revenue_excluido": _round_2(ZERO)}

    # ---- Bloque informativo canceladas ----
    canceladas_info = _info_canceladas(db, negocio_id, desde_dt, hasta_dt)

    # ---- Breakdown por periodo (dia o mes) ----
    if granularidad == "dia":
        trunc_expr = "DATE(v.completed_at)"
    else:
        trunc_expr = "DATE(DATE_TRUNC('month', v.completed_at))"

    sql_buckets = text(f"""
        SELECT
            {trunc_expr} AS fecha,
            COUNT(*) AS ventas_count,
            COALESCE(SUM(v.total), 0) AS revenue,
            COALESCE(SUM(
                (SELECT COALESCE(SUM(ABS(m.cantidad) * m.costo_unitario), 0)
                 FROM movimientos_stock m
                 WHERE m.referencia_id = CAST(v.numero AS TEXT)
                   AND m.almacen_id = v.almacen_id
                   AND m.tipo = 'SALIDA_VENTA')
            ), 0) AS cost_real
        FROM ventas v
        WHERE v.negocio_id = :negocio_id
          AND v.estado = 'COMPLETADA'
          AND v.completed_at >= :desde
          AND v.completed_at <= :hasta
          {legacy_filter_sql}
        GROUP BY {trunc_expr}
        ORDER BY {trunc_expr} ASC
    """)
    rows_buckets = db.execute(sql_buckets, {
        "negocio_id": negocio_id,
        "desde": desde_dt,
        "hasta": hasta_dt,
        **legacy_params,
    }).fetchall()

    por_periodo = []
    for r in rows_buckets:
        b_revenue = _safe(r[2])
        b_cost = _safe(r[3])
        b_profit = b_revenue - b_cost
        b_margin = (b_profit / b_revenue) * HUNDRED if b_revenue > ZERO else ZERO
        por_periodo.append({
            "fecha": r[0],
            "ventas_count": r[1],
            "revenue": _round_2(b_revenue),
            "cost_real": _round_2(b_cost),
            "profit": _round_2(b_profit),
            "margin": _round_2(b_margin),
        })

    return {
        "desde": desde,
        "hasta": hasta,
        "granularidad": granularidad,
        "ventas_count": ventas_count,
        "revenue": _round_2(revenue),
        "cost_real": _round_2(cost_real),
        "profit": _round_2(profit),
        "margin": _round_2(margin),
        "ventas_legacy_excluidas": legacy_info,
        "ventas_canceladas": canceladas_info,
        "por_periodo": por_periodo,
    }


# ---------------------------------------------------------------------------
# Endpoint: utilidad-por-producto
# ---------------------------------------------------------------------------
ORDENES_VALIDOS = {"profit", "margin", "revenue"}


def calcular_utilidad_por_producto(
    db: Session, negocio_id: str, desde: date, hasta: date, orden: str = "profit",
) -> dict:
    """
    Calcula utilidad agregada por producto en el periodo.

    REGLAS (mismas que utilidad-periodo):
    - Solo COMPLETADA, completed_at en rango
    - Excluye legacy
    - Orden por profit | margin | revenue (default profit DESC)

    El revenue por producto se calcula sumando los detalle_venta.subtotal
    MENOS el descuento prorrateado correspondiente. Esto es coherente con
    el endpoint individual y con el agregado de periodo.
    """
    if orden not in ORDENES_VALIDOS:
        raise ValueError(
            f"orden debe ser uno de: {', '.join(ORDENES_VALIDOS)}. Recibí: {orden}"
        )
    _validar_rango(desde, hasta)

    desde_dt = datetime.combine(desde, datetime.min.time())
    hasta_dt = datetime.combine(hasta, datetime.max.time())

    legacy_ids = _ids_ventas_legacy(db, negocio_id, desde_dt, hasta_dt)

    legacy_filter_sql = ""
    legacy_params = {}
    if legacy_ids:
        legacy_filter_sql = "AND v.id != ALL(:legacy_ids)"
        legacy_params["legacy_ids"] = list(legacy_ids)

    # Revenue por producto: subtotal de la linea menos su descuento prorrateado.
    # El descuento prorrateado por linea = (linea.subtotal / venta.subtotal) * venta.descuento
    # Algebraicamente: revenue_neto_linea = linea.subtotal * (1 - venta.descuento / venta.subtotal)
    # Que es lo mismo que: linea.subtotal * (venta.total / venta.subtotal)
    # Esa segunda forma es más estable cuando subtotal=0 (caso defensivo).

    # Cost por producto: SUM movimientos por variante en las ventas del periodo
    # ABS(cantidad) porque salidas son negativas.

    sql = text(f"""
        WITH ventas_periodo AS (
            SELECT v.id, v.numero, v.almacen_id, v.subtotal, v.total
            FROM ventas v
            WHERE v.negocio_id = :negocio_id
              AND v.estado = 'COMPLETADA'
              AND v.completed_at >= :desde
              AND v.completed_at <= :hasta
              {legacy_filter_sql.replace('v.id', 'v.id')}
        ),
        ventas_count_total AS (
            SELECT COUNT(*) AS total FROM ventas_periodo
        ),
        revenue_por_variante AS (
            SELECT
                d.variante_id,
                SUM(d.cantidad) AS qty_total,
                COUNT(DISTINCT d.venta_id) AS ventas_count,
                SUM(
                    CASE
                        WHEN vp.subtotal > 0
                        THEN d.subtotal * (vp.total / vp.subtotal)
                        ELSE d.subtotal
                    END
                ) AS revenue_neto
            FROM detalle_ventas d
            JOIN ventas_periodo vp ON vp.id = d.venta_id
            GROUP BY d.variante_id
        ),
        cost_por_variante AS (
            SELECT
                m.variante_id,
                SUM(ABS(m.cantidad) * m.costo_unitario) AS cost_total
            FROM movimientos_stock m
            JOIN ventas_periodo vp
              ON m.referencia_id = CAST(vp.numero AS TEXT)
             AND m.almacen_id = vp.almacen_id
            WHERE m.tipo = 'SALIDA_VENTA'
            GROUP BY m.variante_id
        )
        SELECT
            r.variante_id,
            p.nombre AS producto_nombre,
            v.sku AS variante_sku,
            r.qty_total,
            r.ventas_count,
            r.revenue_neto,
            COALESCE(c.cost_total, 0) AS cost_total,
            (SELECT total FROM ventas_count_total) AS total_ventas_periodo
        FROM revenue_por_variante r
        JOIN variantes v ON v.id = r.variante_id
        JOIN productos p ON p.id = v.producto_id
        LEFT JOIN cost_por_variante c ON c.variante_id = r.variante_id
        ORDER BY r.variante_id
    """)

    rows = db.execute(sql, {
        "negocio_id": negocio_id,
        "desde": desde_dt,
        "hasta": hasta_dt,
        **legacy_params,
    }).fetchall()

    productos = []
    revenue_total = ZERO
    cost_total_global = ZERO
    ventas_count_periodo = 0

    for r in rows:
        revenue_neto = _safe(r[5])
        cost_total = _safe(r[6])
        profit = revenue_neto - cost_total
        margin = (profit / revenue_neto) * HUNDRED if revenue_neto > ZERO else ZERO

        productos.append({
            "variante_id": r[0],
            "producto_nombre": r[1],
            "variante_sku": r[2],
            "cantidad_vendida": _safe(r[3]),
            "ventas_count": r[4],
            "revenue": _round_2(revenue_neto),
            "cost_real": _round_2(cost_total),
            "profit": _round_2(profit),
            "margin": _round_2(margin),
        })

        revenue_total += revenue_neto
        cost_total_global += cost_total
        ventas_count_periodo = r[7]  # mismo en todas las filas

    # Ordenar
    if orden == "profit":
        productos.sort(key=lambda x: x["profit"], reverse=True)
    elif orden == "margin":
        productos.sort(key=lambda x: x["margin"], reverse=True)
    elif orden == "revenue":
        productos.sort(key=lambda x: x["revenue"], reverse=True)

    profit_total = revenue_total - cost_total_global
    margin_total = (profit_total / revenue_total) * HUNDRED if revenue_total > ZERO else ZERO

    # Bloque informativo legacy
    if legacy_ids:
        sql_leg = text("""
            SELECT COUNT(*), COALESCE(SUM(total), 0)
            FROM ventas WHERE id = ANY(:ids)
        """)
        row_leg = db.execute(sql_leg, {"ids": list(legacy_ids)}).fetchone()
        legacy_info = {
            "count": row_leg[0],
            "revenue_excluido": _round_2(_safe(row_leg[1])),
        }
    else:
        legacy_info = {"count": 0, "revenue_excluido": _round_2(ZERO)}

    return {
        "desde": desde,
        "hasta": hasta,
        "orden": orden,
        "ventas_count": ventas_count_periodo,
        "revenue": _round_2(revenue_total),
        "cost_real": _round_2(cost_total_global),
        "profit": _round_2(profit_total),
        "margin": _round_2(margin_total),
        "ventas_legacy_excluidas": legacy_info,
        "productos": productos,
    }
