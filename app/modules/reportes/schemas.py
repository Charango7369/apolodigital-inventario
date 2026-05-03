"""
Schemas Pydantic — módulo reportes

Estructura cerrada con Edwin:
- Utilidad calculada contra TOTAL de la venta (post-descuento)
- Descuento se prorratea proporcionalmente al subtotal de cada línea
- Costo histórico inmutable: se reconstruye desde MovimientoStock, NO desde Lote actual
- Legacy = ANY(SALIDA_VENTA con lote_id NULL OR costo_unitario NULL)
- Si legacy → cost_real, profit, margin son NULL a nivel venta
- Endpoint principal y endpoint estimado-legacy son DOS endpoints separados
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


# ---------------------------------------------------------------------------
# Detalle de un lote consumido en una línea de venta
# ---------------------------------------------------------------------------
class LoteConsumido(BaseModel):
    """
    Un movimiento SALIDA_VENTA descompuesto por lote.
    Una línea de venta puede tener varios LoteConsumido si FEFO repartió.
    """
    lote_id: str
    qty: Decimal
    cost_unit: Decimal


class LoteConsumidoLegacyEstimado(BaseModel):
    """
    Versión legacy: no hay lote_id ni cost_unit reales.
    El cost_unit se toma del DetalleVenta.costo_unitario congelado al momento de venta.
    """
    lote_id: str | None  # NULL para legacy
    qty: Decimal
    cost_unit: Decimal | None  # del DetalleVenta, no del lote


# ---------------------------------------------------------------------------
# Detalle por línea de venta
# ---------------------------------------------------------------------------
class LineaUtilidad(BaseModel):
    """
    Una línea de la venta con su utilidad descompuesta.
    revenue_bruto = precio_unitario * cantidad - descuento_linea
    descuento_proporcional = parte del descuento global atribuida a esta línea
    revenue_neto = revenue_bruto - descuento_proporcional
    cost_real = SUM(lotes_consumidos[i].qty * lotes_consumidos[i].cost_unit)
    profit = revenue_neto - cost_real
    """
    variante_id: str
    producto_nombre: str
    variante_sku: str | None
    cantidad: Decimal
    precio_unitario: Decimal
    descuento_linea: Decimal       # descuento ya aplicado a la línea (no es el global)
    revenue_bruto: Decimal
    descuento_proporcional: Decimal  # parte del descuento global de la venta
    revenue_neto: Decimal
    cost_real: Decimal | None      # NULL si esta línea tiene movimientos legacy
    profit: Decimal | None
    lotes_consumidos: list[LoteConsumido]


class LineaUtilidadLegacy(BaseModel):
    """Versión para endpoint legacy estimado."""
    variante_id: str
    producto_nombre: str
    variante_sku: str | None
    cantidad: Decimal
    precio_unitario: Decimal
    descuento_linea: Decimal
    revenue_bruto: Decimal
    descuento_proporcional: Decimal
    revenue_neto: Decimal
    cost_estimado: Decimal | None    # del DetalleVenta.costo_unitario
    profit_estimado: Decimal | None
    lotes_consumidos: list[LoteConsumidoLegacyEstimado]


# ---------------------------------------------------------------------------
# Response principal — utilidad real con costo de lote
# ---------------------------------------------------------------------------
class UtilidadVentaResponse(BaseModel):
    """
    Endpoint principal: GET /reportes/utilidad-venta/{venta_id}

    Campos a nivel venta:
      revenue: total de la venta (post-descuento)
      cost_real: SUM(cost_real de cada línea), NULL si la venta es legacy
      profit: revenue - cost_real, NULL si legacy
      margin: (profit / revenue) * 100, NULL si legacy

    legacy=true cuando ANY línea tiene movimientos sin lote_id o sin costo_unitario.
    """
    model_config = ConfigDict(from_attributes=True)

    venta_id: str
    venta_numero: int
    fecha: datetime
    estado: str
    legacy: bool
    revenue: Decimal           # Venta.total
    subtotal: Decimal          # Venta.subtotal
    descuento: Decimal         # Venta.descuento
    cost_real: Decimal | None
    profit: Decimal | None
    margin: Decimal | None     # 0-100, dos decimales
    detalle: list[LineaUtilidad]


class UtilidadLegacyEstimadaResponse(BaseModel):
    """
    Endpoint separado: GET /reportes/utilidad-legacy-estimada/{venta_id}

    Calcula utilidad usando DetalleVenta.costo_unitario (congelado al momento de venta)
    como aproximación cuando no hay lote_id en los movimientos.

    NO usa Variante.precio_costo actual porque ese cambia.
    Si DetalleVenta.costo_unitario también es NULL, no podemos estimar.
    """
    model_config = ConfigDict(from_attributes=True)

    venta_id: str
    venta_numero: int
    fecha: datetime
    estado: str
    revenue: Decimal
    subtotal: Decimal
    descuento: Decimal
    cost_estimado: Decimal | None
    profit_estimado: Decimal | None
    margin_estimado: Decimal | None
    detalle: list[LineaUtilidadLegacy]
    advertencia: str = (
        "Esta utilidad usa el costo congelado en la venta, no el costo real "
        "del lote consumido. Para utilidad real ver /reportes/utilidad-venta/{id}."
    )
