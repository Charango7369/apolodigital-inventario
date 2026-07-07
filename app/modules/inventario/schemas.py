"""
Schemas Pydantic — módulo inventario

Cambios v2 (lotes):
- Nuevo: LoteCreate, LoteUpdate, LoteResponse, LoteProximoVencerResponse
- ProductoCreate/Update: + controla_vencimiento
- MovimientoCreate: + lote_id opcional (override manual de FEFO)
- MovimientoResponse: + lote_id
- Nuevos tipos: MERMA_VENCIMIENTO, MERMA_OTROS
- VarianteAtributoFilter: schema para búsqueda por atributos
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, ConfigDict, model_validator


# ---------------------------------------------------------------------------
# Categoría
# ---------------------------------------------------------------------------
class CategoriaCreate(BaseModel):
    nombre: str = Field(..., min_length=1, max_length=100)
    icono: str | None = Field(None, max_length=50)
    # Bloque B: configuracion de la categoria
    controla_vencimiento_default: bool = False
    atributos_esperados: dict = Field(default_factory=dict)


class CategoriaUpdate(BaseModel):
    nombre: str | None = Field(None, min_length=1, max_length=100)
    icono: str | None = None
    activa: bool | None = None
    # Bloque B
    controla_vencimiento_default: bool | None = None
    atributos_esperados: dict | None = None


class CategoriaResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    negocio_id: str
    nombre: str
    icono: str | None
    activa: bool
    # Bloque B
    controla_vencimiento_default: bool
    atributos_esperados: dict


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
# Variante
# ---------------------------------------------------------------------------
class VarianteCreate(BaseModel):
    sku: str | None = Field(None, max_length=100)
    codigo_barras: str | None = Field(None, max_length=50)
    atributos: dict | None = Field(default_factory=dict)
    precio_venta: Decimal = Field(..., ge=0, decimal_places=2)
    precio_costo: Decimal | None = Field(None, ge=0, decimal_places=2)
    foto_url: str | None = Field(None, max_length=500)


class VarianteUpdate(BaseModel):
    sku: str | None = None
    codigo_barras: str | None = None
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
    codigo_barras: str | None
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
    controla_vencimiento: bool = False  # NUEVO
    precio_venta: Decimal | None = Field(None, ge=0)
    precio_costo: Decimal | None = Field(None, ge=0)
    sku: str | None = Field(
        None, max_length=100,
        description="SKU de la variante única para productos sin variantes. "
                    "Se ignora si tiene_variantes=True (el SKU se define por variante).",
    )

class ProductoUpdate(BaseModel):
    nombre: str | None = Field(None, min_length=1, max_length=200)
    descripcion: str | None = None
    categoria_id: str | None = None
    proveedor_id: str | None = None
    codigo_barras: str | None = None
    unidad_medida: str | None = None
    controla_vencimiento: bool | None = None  # NUEVO
    activo: bool | None = None
    precio_venta: Decimal | None = Field(None, ge=0)
    precio_costo: Decimal | None = Field(None, ge=0)
    sku: str | None = None


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
    controla_vencimiento: bool  # NUEVO
    activo: bool
    created_at: datetime
    updated_at: datetime
    variantes: list[VarianteResponse] = []


class ProductoListResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    nombre: str
    categoria_id: str | None
    codigo_barras: str | None
    unidad_medida: str
    tiene_variantes: bool
    es_servicio: bool
    controla_vencimiento: bool  # NUEVO
    activo: bool
    precio_venta: Decimal | None = None


# ---------------------------------------------------------------------------
# Lote (NUEVO)
# ---------------------------------------------------------------------------
class LoteCreate(BaseModel):
    """Crear un lote nuevo. Genera automáticamente un movimiento ENTRADA_COMPRA."""
    variante_id: str
    almacen_id: str
    codigo_lote: str | None = Field(None, max_length=50)
    fecha_vencimiento: date | None = None
    cantidad: Decimal = Field(..., gt=0, decimal_places=3)
    costo_unitario: Decimal | None = Field(None, ge=0, decimal_places=2)
    referencia_compra: str | None = Field(None, max_length=100)
    notas: str | None = None


class LoteUpdate(BaseModel):
    """Edita campos descriptivos. Para cambiar cantidad usá movimientos."""
    codigo_lote: str | None = Field(None, max_length=50)
    fecha_vencimiento: date | None = None
    costo_unitario: Decimal | None = Field(None, ge=0, decimal_places=2)
    referencia_compra: str | None = Field(None, max_length=100)
    notas: str | None = None
    activo: bool | None = None


class LoteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    variante_id: str
    almacen_id: str
    codigo_lote: str | None
    fecha_vencimiento: date | None
    fecha_ingreso: datetime
    cantidad_inicial: Decimal
    cantidad_actual: Decimal
    costo_unitario: Decimal | None
    referencia_compra: str | None
    activo: bool
    notas: str | None


class LoteProximoVencerResponse(BaseModel):
    """Reporte: lotes con stock que vencen pronto."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    variante_id: str
    almacen_id: str
    codigo_lote: str | None
    fecha_vencimiento: date | None
    cantidad_actual: Decimal
    # Info enriquecida para UI
    producto_nombre: str
    variante_sku: str | None
    almacen_nombre: str
    dias_para_vencer: int  # negativo = ya vencido


class DarBajaLoteRequest(BaseModel):
    motivo: str | None = Field(None, max_length=500)


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
    model_config = ConfigDict(from_attributes=True)

    id: str
    variante_id: str
    almacen_id: str
    cantidad_actual: Decimal
    cantidad_minima: Decimal
    cantidad_maxima: Decimal | None
    producto_nombre: str
    variante_sku: str | None
    almacen_nombre: str


class StockUpdate(BaseModel):
    cantidad_minima: Decimal | None = Field(None, ge=0)
    cantidad_maxima: Decimal | None = Field(None, ge=0)


# ---------------------------------------------------------------------------
# Movimiento
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
    "MERMA_VENCIMIENTO",  # NUEVO
    "MERMA_OTROS",        # NUEVO
]


class MovimientoCreate(BaseModel):
    """
    Crear un movimiento de stock.

    Reglas según el tipo:
    - ENTRADAS sobre lote existente: pasar `lote_id`. Para crear lote nuevo, usar
      el endpoint POST /lotes en lugar de este.
    - SALIDAS: si pasás `lote_id` el descuento es de ese lote (override manual).
      Si no, el sistema aplica FEFO automático.
    """
    variante_id: str
    almacen_id: str
    tipo: TipoMovimiento
    cantidad: Decimal = Field(..., gt=0)
    lote_id: str | None = None  # NUEVO: override manual o entrada a lote existente
    costo_unitario: Decimal | None = Field(None, ge=0)
    referencia_id: str | None = Field(None, max_length=100)
    motivo: str | None = None


class MovimientoResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    variante_id: str
    almacen_id: str
    lote_id: str | None  # NUEVO
    tipo: str
    cantidad: Decimal
    costo_unitario: Decimal | None
    referencia_id: str | None
    motivo: str | None
    usuario_id: str | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Búsqueda por atributos (JSONB)
# ---------------------------------------------------------------------------
class BusquedaAtributosRequest(BaseModel):
    """
    Buscar variantes que matcheen TODOS los atributos especificados.
    Ejemplo body: {"atributos": {"talla": "M", "color": "rojo"}}
    """
    atributos: dict = Field(..., min_length=1)


# ---------------------------------------------------------------------------
# Respuestas paginadas
# ---------------------------------------------------------------------------
class PaginatedResponse(BaseModel):
    items: list
    total: int
    page: int
    per_page: int
    pages: int


# ===========================================================================
# Bloque B — endpoints nuevos
# ===========================================================================

class GenerarVariantesRequest(BaseModel):
    """
    Body para POST /productos/{id}/generar-variantes

    Ejemplo:
        {
            "atributos": {
                "talla": ["S", "M", "L"],
                "color": ["rojo", "azul"]
            },
            "precio_venta": 150.00,
            "precio_costo": 80.00,
            "sku_prefix": "REM-NIKE"
        }

    Genera todas las combinaciones (3 × 2 = 6 variantes en el ejemplo).
    SKU autogenerado: f"{sku_prefix}-{valor1}-{valor2}" → "REM-NIKE-S-rojo".
    """
    atributos: dict[str, list[str]] = Field(..., min_length=1)
    precio_venta: Decimal = Field(..., ge=0, decimal_places=2)
    precio_costo: Decimal | None = Field(None, ge=0, decimal_places=2)
    sku_prefix: str | None = Field(None, max_length=50)
    bypass_validation: bool = False  # solo admin


class GenerarVariantesResponse(BaseModel):
    """Resumen de la generacion."""
    producto_id: str
    creadas: int
    omitidas_por_duplicado: int
    variantes: list[VarianteResponse]


class AplicarDefaultRequest(BaseModel):
    """
    Body para POST /categorias/{id}/aplicar-default-a-productos

    Aplica el `controla_vencimiento_default` actual de la categoria a TODOS
    los productos activos de esa categoria. Pisar valores existentes.
    Solo admin puede ejecutarlo.
    """
    confirmar: bool = Field(..., description="Debe ser true para ejecutar")


class AplicarDefaultResponse(BaseModel):
    productos_afectados: int
    valor_aplicado: bool
