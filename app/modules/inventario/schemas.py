"""
Schemas Pydantic — módulo inventario
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, ConfigDict


# ---------------------------------------------------------------------------
# Categoría
# ---------------------------------------------------------------------------
class CategoriaCreate(BaseModel):
    nombre: str = Field(..., min_length=1, max_length=100)
    icono: str | None = Field(None, max_length=50)


class CategoriaUpdate(BaseModel):
    nombre: str | None = Field(None, min_length=1, max_length=100)
    icono: str | None = None
    activa: bool | None = None


class CategoriaResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    negocio_id: str
    nombre: str
    icono: str | None
    activa: bool


# ---------------------------------------------------------------------------
# Proveedor
# ---------------------------------------------------------------------------
class ProveedorCreate(BaseModel):
    nombre: str = Field(..., min_length=1, max_length=200)
    telefono: str | None = Field(None, max_length=30)
    notas: str | None = None


class ProveedorUpdate(BaseModel):
    nombre: str | None = Field(None, min_length=1, max_length=200)
    telefono: str | None = None
    notas: str | None = None
    activo: bool | None = None


class ProveedorResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    negocio_id: str
    nombre: str
    telefono: str | None
    notas: str | None
    activo: bool


# ---------------------------------------------------------------------------
# Almacén
# ---------------------------------------------------------------------------
class AlmacenCreate(BaseModel):
    nombre: str = Field(..., min_length=1, max_length=100)
    ubicacion: str | None = Field(None, max_length=200)
    es_principal: bool = False


class AlmacenUpdate(BaseModel):
    nombre: str | None = Field(None, min_length=1, max_length=100)
    ubicacion: str | None = None
    es_principal: bool | None = None


class AlmacenResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    negocio_id: str
    nombre: str
    ubicacion: str | None
    es_principal: bool


# ---------------------------------------------------------------------------
# Variante (nested en Producto)
# ---------------------------------------------------------------------------
class VarianteCreate(BaseModel):
    sku: str | None = Field(None, max_length=100)
    atributos: dict | None = Field(default_factory=dict)
    precio_venta: Decimal = Field(..., ge=0, decimal_places=2)
    precio_costo: Decimal | None = Field(None, ge=0, decimal_places=2)
    foto_url: str | None = Field(None, max_length=500)


class VarianteUpdate(BaseModel):
    sku: str | None = None
    atributos: dict | None = None
    precio_venta: Decimal | None = Field(None, ge=0)
    precio_costo: Decimal | None = Field(None, ge=0)
    foto_url: str | None = None
    activa: bool | None = None


class VarianteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    producto_id: str
    sku: str | None
    atributos: dict | None
    precio_venta: Decimal
    precio_costo: Decimal | None
    foto_url: str | None
    activa: bool


# ---------------------------------------------------------------------------
# Producto
# ---------------------------------------------------------------------------
class ProductoCreate(BaseModel):
    nombre: str = Field(..., min_length=1, max_length=200)
    descripcion: str | None = None
    categoria_id: str | None = None
    proveedor_id: str | None = None
    codigo_barras: str | None = Field(None, max_length=100)
    unidad_medida: str = Field("unidad", max_length=30)
    tiene_variantes: bool = False
    es_servicio: bool = False
    # Variante por defecto (requerida si tiene_variantes=False)
    precio_venta: Decimal | None = Field(None, ge=0)
    precio_costo: Decimal | None = Field(None, ge=0)


class ProductoUpdate(BaseModel):
    nombre: str | None = Field(None, min_length=1, max_length=200)
    descripcion: str | None = None
    categoria_id: str | None = None
    proveedor_id: str | None = None
    codigo_barras: str | None = None
    unidad_medida: str | None = None
    activo: bool | None = None


class ProductoResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    negocio_id: str
    categoria_id: str | None
    proveedor_id: str | None
    nombre: str
    descripcion: str | None
    codigo_barras: str | None
    unidad_medida: str
    tiene_variantes: bool
    es_servicio: bool
    activo: bool
    created_at: datetime
    updated_at: datetime
    variantes: list[VarianteResponse] = []


class ProductoListResponse(BaseModel):
    """Versión ligera para listados"""
    model_config = ConfigDict(from_attributes=True)

    id: str
    nombre: str
    categoria_id: str | None
    codigo_barras: str | None
    unidad_medida: str
    tiene_variantes: bool
    es_servicio: bool
    activo: bool
    # Precio de la primera variante activa
    precio_venta: Decimal | None = None


# ---------------------------------------------------------------------------
# Stock
# ---------------------------------------------------------------------------
class StockResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    variante_id: str
    almacen_id: str
    cantidad_actual: Decimal
    cantidad_minima: Decimal
    cantidad_maxima: Decimal | None
    actualizado_at: datetime


class StockConDetalleResponse(BaseModel):
    """Stock con info de producto/variante para alertas y reportes"""
    model_config = ConfigDict(from_attributes=True)

    id: str
    variante_id: str
    almacen_id: str
    cantidad_actual: Decimal
    cantidad_minima: Decimal
    cantidad_maxima: Decimal | None
    # Info adicional
    producto_nombre: str
    variante_sku: str | None
    almacen_nombre: str


class StockUpdate(BaseModel):
    cantidad_minima: Decimal | None = Field(None, ge=0)
    cantidad_maxima: Decimal | None = Field(None, ge=0)


# ---------------------------------------------------------------------------
# Movimiento de Stock
# ---------------------------------------------------------------------------
TipoMovimiento = Literal[
    "ENTRADA_COMPRA",
    "SALIDA_VENTA",
    "AJUSTE_POSITIVO",
    "AJUSTE_NEGATIVO",
    "TRANSFERENCIA_ENTRADA",
    "TRANSFERENCIA_SALIDA",
    "DEVOLUCION_CLIENTE",
    "DEVOLUCION_PROVEEDOR",
]


class MovimientoCreate(BaseModel):
    variante_id: str
    almacen_id: str
    tipo: TipoMovimiento
    cantidad: Decimal = Field(..., gt=0)
    costo_unitario: Decimal | None = Field(None, ge=0)
    referencia_id: str | None = Field(None, max_length=100)
    motivo: str | None = None


class MovimientoResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    variante_id: str
    almacen_id: str
    tipo: str
    cantidad: Decimal
    costo_unitario: Decimal | None
    referencia_id: str | None
    motivo: str | None
    usuario_id: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Respuestas paginadas
# ---------------------------------------------------------------------------
class PaginatedResponse(BaseModel):
    items: list
    total: int
    page: int
    per_page: int
    pages: int
