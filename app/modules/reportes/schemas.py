"""
Schemas Pydantic — módulo reportes
Estructura unificada: Inventario + Utilidad de Ventas.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, ConfigDict




# ===========================================================================
# 1. SCHEMAS DE INVENTARIO (Stock y Alertas)
# ===========================================================================

class StockBaseDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    variante_id: str
    producto_nombre: str
    sku: Optional[str]
    almacen_nombre: str
    cantidad_actual: Decimal


class AlertaStockDTO(StockBaseDTO):
    cantidad_minima: Decimal
    estado_alerta: str  # "AGOTADO" o "STOCK_BAJO"


# ===========================================================================
# 2. SCHEMAS DE UTILIDAD Y VENTAS
# ===========================================================================

class LoteConsumido(BaseModel):
    lote_id: str
    qty: Decimal
    cost_unit: Decimal


class LoteConsumidoLegacyEstimado(BaseModel):
    lote_id: Optional[str]
    qty: Decimal
    cost_unit: Optional[Decimal]


class LineaUtilidad(BaseModel):
    variante_id: str
    producto_nombre: str
    variante_sku: Optional[str]
    cantidad: Decimal
    precio_unitario: Decimal
    descuento_linea: Decimal
    revenue_bruto: Decimal
    descuento_proporcional: Decimal
    revenue_neto: Decimal
    cost_real: Optional[Decimal]
    profit: Optional[Decimal]
    lotes_consumidos: List[LoteConsumido]


class LineaUtilidadLegacy(BaseModel):
    variante_id: str
    producto_nombre: str
    variante_sku: Optional[str]
    cantidad: Decimal
    precio_unitario: Decimal
    descuento_linea: Decimal
    revenue_bruto: Decimal
    descuento_proporcional: Decimal
    revenue_neto: Decimal
    cost_estimado: Optional[Decimal]
    profit_estimado: Optional[Decimal]
    lotes_consumidos: List[LoteConsumidoLegacyEstimado]


class UtilidadVentaResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    venta_id: str
    venta_numero: int
    fecha: datetime
    estado: str
    legacy: bool
    revenue: Decimal
    subtotal: Decimal
    descuento: Decimal
    cost_real: Optional[Decimal]
    profit: Optional[Decimal]
    margin: Optional[Decimal]
    detalle: List[LineaUtilidad]


class UtilidadLegacyEstimadaResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    venta_id: str
    venta_numero: int
    fecha: datetime
    estado: str
    revenue: Decimal
    subtotal: Decimal
    descuento: Decimal
    cost_estimado: Optional[Decimal]
    profit_estimado: Optional[Decimal]
    margin_estimado: Optional[Decimal]
    detalle: List[LineaUtilidadLegacy]
    advertencia: str = (
        "Esta utilidad usa el costo congelado en la venta, no el costo real "
        "del lote consumido. Para utilidad real ver /reportes/utilidad-venta/{id}."
    )


class VentasCanceladasInfo(BaseModel):
    count: int
    monto_cancelado: Decimal


class VentasLegacyInfo(BaseModel):
    count: int
    revenue_excluido: Decimal


class UtilidadPeriodoBucket(BaseModel):
    fecha: date
    ventas_count: int
    revenue: Decimal
    cost_real: Decimal
    profit: Decimal
    margin: Decimal


class UtilidadPeriodoResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    desde: date
    hasta: date
    granularidad: str
    ventas_count: int
    revenue: Decimal
    cost_real: Decimal
    profit: Decimal
    margin: Decimal
    ventas_legacy_excluidas: VentasLegacyInfo
    ventas_canceladas: VentasCanceladasInfo
    por_periodo: List[UtilidadPeriodoBucket]


class UtilidadProductoItem(BaseModel):
    variante_id: str
    producto_nombre: str
    variante_sku: Optional[str]
    cantidad_vendida: Decimal
    ventas_count: int
    revenue: Decimal
    cost_real: Decimal
    profit: Decimal
    margin: Decimal


# 1. El molde exacto para cada fila de producto
class ProductoUtilidadDTO(BaseModel):
    producto_id: str
    producto_nombre: str
    cantidad_vendida: int
    ventas_count: int  # Añadido para que coincida con nuestro service.py
    revenue: Decimal
    cost_real: Decimal
    profit: Decimal
    margin: Decimal

# 2. El contrato unificado que nuestro router y React esperan
class UtilidadPorProductoResponse(BaseModel):
    desde: date
    hasta: date
    top_rentables: List[ProductoUtilidadDTO]
    top_perdidas: List[ProductoUtilidadDTO]

class TopUtilidadProductosResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    desde: date
    hasta: date
    top_rentables: List[ProductoUtilidadDTO]
    top_perdidas: List[ProductoUtilidadDTO]
    
class StockBaseDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    variante_id: str
    producto_nombre: str
    sku: Optional[str]
    almacen_nombre: str
    cantidad_actual: Decimal
    costo_unitario: Decimal  # <-- La variable que faltaba