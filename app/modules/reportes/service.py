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

# 1. Librerías Estándar
from collections import defaultdict
from datetime import datetime, time, timezone
from decimal import ROUND_HALF_UP, Decimal

# 2. Librerías de Terceros
from sqlalchemy import case, select, text
from sqlalchemy.orm import Session, joinedload

# 3. Módulos Locales de la Aplicación
from app.modules.inventario.models import Almacen, MovimientoStock, Producto, Stock, Variante
from app.modules.ventas.models import DetalleVenta, Venta

# Constantes globales de cálculo
ZERO = Decimal("0")
HUNDRED = Decimal("100")
TWO_PLACES = Decimal("0.01")

def obtener_reporte_stock_actual(
    db: Session, 
    negocio_id: str, 
    almacen_id: str | None = None, 
    categoria_id: str | None = None
) -> list[dict]:
    """
    Calcula el stock actual agrupado. 
    Alineado estrictamente con StockBaseDTO heredado.
    """
    stmt = (
        select(
            Variante.id.label("variante_id"),
            Producto.nombre.label("producto_nombre"),
            Variante.sku.label("sku"),
            Almacen.nombre.label("almacen_nombre"),
            Stock.cantidad_actual.label("cantidad_actual"),
            Variante.precio_costo.label("costo_unitario")  # ⬅️ CORREGIDO: precio_costo
        )
        .join(Variante, Stock.variante_id == Variante.id)
        .join(Producto, Variante.producto_id == Producto.id)
        .join(Almacen, Stock.almacen_id == Almacen.id)
        .where(
            Producto.negocio_id == negocio_id,
            Stock.cantidad_actual > 0  # Solo traer lo que realmente ocupa espacio
        )
    )

    # Filtros dinámicos
    if almacen_id:
        stmt = stmt.where(Stock.almacen_id == almacen_id)
    if categoria_id:
        stmt = stmt.where(Producto.categoria_id == categoria_id)

    # Ordenamiento nativo descendente por cantidad actual
    stmt = stmt.order_by(Stock.cantidad_actual.desc())
    
    resultados = db.execute(stmt).mappings().all()
    return [dict(row) for row in resultados]

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
    db: Session, venta_id: int, almacen_id: str,
) -> dict[str, list[MovimientoStock]]:
    """
    Trae todos los SALIDA_VENTA de la venta agrupados por variante_id.

    Devuelve: {variante_id: [movimiento1, movimiento2, ...]}

    Una línea de venta puede generar N movimientos por FEFO repartido.
    """
    movs = (
        db.query(MovimientoStock)
        .filter(
            MovimientoStock.referencia_id == venta_id,
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
        db, venta.id, venta.almacen_id,
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
        db, venta.id, venta.almacen_id,
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
def _ids_ventas_legacy(db: Session, negocio_id: str, desde: datetime, hasta: datetime) -> set[str]:
    """
    Identifica dinámicamente IDs de ventas corruptas o antiguas (Legacy).
    Una venta es Legacy si está COMPLETADA, contiene detalles de productos físicos,
    pero NO generó registros de salida en la tabla de movimientos de stock.
    """
    # Usamos NOT EXISTS para máxima velocidad en PostgreSQL
    # Ajusta los nombres de las tablas de detalles ('detalles_ventas') si difieren en tu esquema
    # Usamos NOT EXISTS para máxima velocidad en PostgreSQL
    query = text("""
        SELECT DISTINCT v.id
        FROM ventas v
        JOIN detalle_ventas dv ON dv.venta_id = v.id  -- CORRECCIÓN: detalle_ventas en lugar de detalles_ventas
        JOIN variantes var ON var.id = dv.variante_id
        JOIN productos p ON p.id = var.producto_id
        WHERE v.negocio_id = :negocio_id
          AND v.estado = 'COMPLETADA'
          AND v.completed_at >= :desde
          AND v.completed_at <= :hasta
          AND p.es_servicio = FALSE
          AND NOT EXISTS (
              SELECT 1 
              FROM movimientos_stock m 
              WHERE m.referencia_id = v.id::text
                AND m.tipo = 'SALIDA_VENTA'
          )
    """)
    result = db.execute(query, {
        "negocio_id": negocio_id,
        "desde": desde,
        "hasta": hasta
    }).fetchall()

    # Retornamos un set de strings para que la comparación 'v.id != ALL(:legacy_ids)' funcione velozmente
    return {str(row[0]) for row in result}

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

# Asegúrate de importar tu constante ZERO, HUNDRED, _safe, _round_2, etc.
from zoneinfo import ZoneInfo

ZONA_LOCAL = ZoneInfo("America/La_Paz")

def calcular_utilidad_periodo(
    db: Session, negocio_id: str, desde: date, hasta: date,
) -> dict:
    """
    Calcula utilidad agregada del periodo con breakdown temporal.
    Optimizada con casting de UUID, corrección de zona horaria (GMT-4) y normalización DTO.
    """
    _validar_rango(desde, hasta)
    granularidad = _granularidad_para(desde, hasta)

    # Convertir el día calendario BOLIVIANO a sus límites UTC reales — antes esto
    # comparaba las fechas "a lo ingenuo", como si 00:00 del día elegido ya fuera
    # 00:00 UTC. Eso desalinea el total del período respecto del desglose diario
    # de más abajo (que sí convierte a hora local), contando ventas de la noche
    # anterior (hora Bolivia) como si fueran del día siguiente.
    desde_local = datetime.combine(desde, time.min, tzinfo=ZONA_LOCAL)
    hasta_local = datetime.combine(hasta, time.max, tzinfo=ZONA_LOCAL)
    desde_dt = desde_local.astimezone(timezone.utc).replace(tzinfo=None)
    hasta_dt = hasta_local.astimezone(timezone.utc).replace(tzinfo=None)
    legacy_ids = _ids_ventas_legacy(db, negocio_id, desde_dt, hasta_dt)

    legacy_filter_sql = ""
    legacy_params = {}
    if legacy_ids:
        legacy_list = list(legacy_ids)
        legacy_filter_sql = "AND v.id != ALL(:legacy_ids)"
        legacy_params["legacy_ids"] = legacy_list

    # Revenue
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

    # PARCHE 2: Cast explícito de v.id a texto (v.id::text)
    sql_cost = text(f"""
        SELECT COALESCE(SUM(ABS(m.cantidad) * m.costo_unitario), 0) AS cost
        FROM ventas v
        JOIN movimientos_stock m
          ON m.referencia_id = v.id::text 
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
    margin = (profit / revenue) * HUNDRED if revenue > ZERO else ZERO

    # Información legacy y canceladas
    sql_legacy_info = text("""
        SELECT COUNT(*) AS cnt, COALESCE(SUM(total), 0) AS rev_excluido
        FROM ventas WHERE id = ANY(:ids)
    """)
    if legacy_ids:
        row_leg = db.execute(sql_legacy_info, {"ids": list(legacy_ids)}).fetchone()
        legacy_info = {"count": row_leg[0], "revenue_excluido": _round_2(_safe(row_leg[1]))}
    else:
        legacy_info = {"count": 0, "revenue_excluido": _round_2(ZERO)}

    # PARCHE DTO: Extracción y normalización cruda de canceladas
    raw_canceladas = _info_canceladas(db, negocio_id, desde_dt, hasta_dt)
    monto_anulado_bruto = raw_canceladas.get("monto_cancelado")
    if monto_anulado_bruto is None:
        monto_anulado_bruto = raw_canceladas.get("revenue_perdido", ZERO)
    
    conteo_canceladas = raw_canceladas.get("count", raw_canceladas.get("cnt", 0))

    ventas_canceladas_dto = {
        "count": conteo_canceladas,
        "monto_cancelado": _round_2(_safe(monto_anulado_bruto))
    }

    # PARCHE 1: Corrección de Zona Horaria directamente en SQL antes de truncar la fecha
    if granularidad == "dia":
        trunc_expr = "DATE(v.completed_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/La_Paz')"
    else:
        trunc_expr = "DATE(DATE_TRUNC('month', v.completed_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/La_Paz'))"

    # PARCHE 3: Subconsulta casted a texto (v.id::text)
    sql_buckets = text(f"""
        SELECT
            {trunc_expr} AS fecha,
            COUNT(*) AS ventas_count,
            COALESCE(SUM(v.total), 0) AS revenue,
            COALESCE(SUM(
                (SELECT COALESCE(SUM(ABS(m.cantidad) * m.costo_unitario), 0)
                 FROM movimientos_stock m
                 WHERE m.referencia_id = v.id::text
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
        "ventas_canceladas": ventas_canceladas_dto, # Bloque estrictamente normalizado
        "por_periodo": por_periodo,
    }
    
def calcular_utilidad_por_producto(
    db: Session, negocio_id: str, desde: date, hasta: date, orden: str = "profit", limit: int = 10
) -> dict:
    """
    Calcula utilidad agregada por producto en el periodo.
    Consolida las métricas de las variantes bajo el producto raíz.
    """
    # Validación autónoma: Sin dependencias de variables globales
    if orden not in ("profit", "margin", "revenue"):
        raise ValueError(
            f"El parámetro 'orden' debe ser uno de: profit, margin, revenue. Recibí: {orden}"
        )
    _validar_rango(desde, hasta)

    # Mismo fix que calcular_utilidad_periodo: convertir el día calendario
    # BOLIVIANO a sus límites UTC reales. El código anterior le pegaba la
    # etiqueta UTC directamente a medianoche boliviana (.replace(tzinfo=...)
    # no corre horas, solo cambia la etiqueta), sin correr las 4 horas de
    # diferencia real — mismo bug que en calcular_utilidad_periodo.
    desde_local = datetime.combine(desde, time.min, tzinfo=ZONA_LOCAL)
    hasta_local = datetime.combine(hasta, time.max, tzinfo=ZONA_LOCAL)
    desde_dt = desde_local.astimezone(timezone.utc).replace(tzinfo=None)
    hasta_dt = hasta_local.astimezone(timezone.utc).replace(tzinfo=None)

    legacy_ids = _ids_ventas_legacy(db, negocio_id, desde_dt, hasta_dt)

    legacy_filter_sql = ""
    legacy_params = {}
    if legacy_ids:
        legacy_filter_sql = "AND v.id != ALL(:legacy_ids)"
        legacy_params["legacy_ids"] = list(legacy_ids)

    # Mantenemos tus CTEs intactas, pero reparamos la selección final
    sql = text(f"""
        WITH ventas_periodo AS (
            SELECT v.id, v.numero, v.almacen_id, v.subtotal, v.total
            FROM ventas v
            WHERE v.negocio_id = :negocio_id
              AND v.estado = 'COMPLETADA'
              AND v.completed_at >= :desde
              AND v.completed_at <= :hasta
              {legacy_filter_sql}
        ),
        ventas_count_total AS (
            SELECT COUNT(*) AS total FROM ventas_periodo
        ),
        revenue_por_variante AS (
            SELECT
                d.variante_id,
                MAX(d.producto_nombre) AS producto_nombre_historico,
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
              ON m.referencia_id = vp.id
             AND m.almacen_id = vp.almacen_id
            WHERE m.tipo = 'SALIDA_VENTA'
            GROUP BY m.variante_id
        )
        SELECT
            r.variante_id,                                      -- [0]
            COALESCE(p.nombre, r.producto_nombre_historico) AS producto_nombre, -- [1]
            v.sku AS variante_sku,                              -- [2]
            r.qty_total,                                        -- [3]
            r.ventas_count,                                     -- [4]
            r.revenue_neto,                                     -- [5]
            COALESCE(c.cost_total, 0) AS cost_total,            -- [6]
            (SELECT total FROM ventas_count_total) AS total_ventas_periodo, -- [7]
            p.id AS producto_id                                 -- [8] ⬅️ LA CURA AL PUNTO CIEGO 1
        FROM revenue_por_variante r
        LEFT JOIN variantes v ON v.id = r.variante_id
        LEFT JOIN productos p ON p.id = v.producto_id
        LEFT JOIN cost_por_variante c ON c.variante_id = r.variante_id
        ORDER BY r.variante_id
    """)

    rows = db.execute(sql, {
        "negocio_id": negocio_id,
        "desde": desde_dt,
        "hasta": hasta_dt,
        **legacy_params,
    }).fetchall()

    # Diccionario intermedio para consolidar variantes en el producto raíz
    consolidado_productos = {}

    for r in rows:
        # p.id está ahora de forma segura en el índice 8 de la fila
        prod_id = str(r[8]) if r[8] else f"LGC-{r[0]}" 
        prod_nombre = r[1]
        cantidad_vendida = _safe(r[3])
        v_count = int(r[4]) if r[4] else 0  # Rescate del Punto Ciego 3
        revenue_neto = _safe(r[5])
        cost_total = _safe(r[6])

        if prod_id not in consolidado_productos:
            consolidado_productos[prod_id] = {
                "producto_id": prod_id,
                "producto_nombre": prod_nombre,
                "cantidad_vendida": 0,
                "ventas_count": 0,
                "revenue": ZERO,
                "cost_real": ZERO,
            }

        consolidado_productos[prod_id]["cantidad_vendida"] += cantidad_vendida
        consolidado_productos[prod_id]["ventas_count"] += v_count
        consolidado_productos[prod_id]["revenue"] += revenue_neto
        consolidado_productos[prod_id]["cost_real"] += cost_total

    # Formateo y cálculo de márgenes finales por producto unificado
    lista_productos = []
    for p_id, p_data in consolidado_productos.items():
        rev = p_data["revenue"]
        cost = p_data["cost_real"]
        profit = rev - cost
        margin = (profit / rev) * HUNDRED if rev > ZERO else ZERO

        lista_productos.append({
            "producto_id": p_id,
            "producto_nombre": p_data["producto_nombre"],
            "cantidad_vendida": int(p_data["cantidad_vendida"]),
            "ventas_count": p_data["ventas_count"],
            "revenue": _round_2(rev),
            "cost_real": _round_2(cost),
            "profit": _round_2(profit),
            "margin": _round_2(margin),
        })

    # Aplicamos las reglas de ordenamiento sobre la lista consolidada
    if orden == "profit":
        lista_productos.sort(key=lambda x: x["profit"], reverse=True)
    elif orden == "margin":
        lista_productos.sort(key=lambda x: x["margin"], reverse=True)
    elif orden == "revenue":
        lista_productos.sort(key=lambda x: x["revenue"], reverse=True)

    # Segmentación estricta para alimentar los dos gráficos del frontend
    top_rentables = lista_productos[:limit]
    
    # Las pérdidas se calculan filtrando los profits negativos y ordenando del peor al mejor
    productos_perdida = [p for p in lista_productos if p["profit"] < ZERO]
    top_perdidas = sorted(productos_perdida, key=lambda x: x["profit"])[:limit]

    return {
        "desde": desde,
        "hasta": hasta,
        "top_rentables": top_rentables,
        "top_perdidas": top_perdidas
    }

def obtener_reporte_alertas_stock(
    db: Session, 
    negocio_id: str, 
    almacen_id: str | None = None
) -> list[dict]:
    """
    Identifica inventario crítico. 
    La lógica de criticidad se resuelve a nivel de base de datos.
    """
    # Expresión CASE nativa de SQL para evitar bucles for en Python
    estado_alerta_expr = case(
        (Stock.cantidad_actual <= 0, "AGOTADO"),
        else_="STOCK_BAJO"
    ).label("estado_alerta")

    stmt = (
        select(
            Variante.id.label("variante_id"),
            Producto.nombre.label("producto_nombre"),
            Variante.sku.label("sku"),                  # Etiquetado para Pydantic
            Almacen.nombre.label("almacen_nombre"),
            Stock.cantidad_actual.label("cantidad_actual"),
            Stock.cantidad_minima.label("cantidad_minima"),
            estado_alerta_expr                          # Columna calculada inyectada
        )
        .join(Variante, Stock.variante_id == Variante.id)
        .join(Producto, Variante.producto_id == Producto.id)
        .join(Almacen, Stock.almacen_id == Almacen.id)
        .where(
            Producto.negocio_id == negocio_id,
            Stock.cantidad_actual <= Stock.cantidad_minima
        )
    )

    if almacen_id:
        stmt = stmt.where(Stock.almacen_id == almacen_id)

    # Ordenamos primero por los agotados, luego por orden alfabético
    stmt = stmt.order_by(Stock.cantidad_actual.asc(), Producto.nombre.asc())

    resultados = db.execute(stmt).mappings().all()
    return [dict(row) for row in resultados]