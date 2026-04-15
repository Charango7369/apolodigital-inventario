"""
Schemas Pydantic — módulo ventas
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, ConfigDict


# ---------------------------------------------------------------------------
# Tipos
# ---------------------------------------------------------------------------
EstadoVenta = Literal["PENDIENTE", "COMPLETADA", "CANCELADA"]
MetodoPago = Literal["EFECTIVO", "QR", "TARJETA", "TRANSFERENCIA", "CREDITO", "MIXTO"]


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------
class ClienteCreate(BaseModel):
    nombre: str = Field(..., min_length=1, max_length=200)
    telefono: str | None = Field(None, max_length=30)
    email: str | None = Field(None, max_length=200)
    nit: str | None = Field(None, max_length=20)
    direccion: str | None = None
    notas: str | None = None


class ClienteUpdate(BaseModel):
    nombre: str | None = Field(None, min_length=1, max_length=200)
    telefono: str | None = None
    email: str | None = None
    nit: str | None = None
    direccion: str | None = None
    notas: str | None = None
    activo: bool | None = None


class ClienteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    negocio_id: str
    nombre: str
    telefono: str | None
    email: str | None
    nit: str | None
    direccion: str | None
    notas: str | None
    activo: bool
    created_at: datetime


# ---------------------------------------------------------------------------
# Detalle de Venta (línea)
# ---------------------------------------------------------------------------
class DetalleVentaCreate(BaseModel):
    variante_id: str
    cantidad: Decimal = Field(..., gt=0)
    precio_unitario: Decimal | None = None  # Si no se envía, usa precio de variante
    descuento_linea: Decimal = Field(default=Decimal("0"), ge=0)


class DetalleVentaResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    venta_id: str
    variante_id: str
    producto_nombre: str
    variante_sku: str | None
    cantidad: Decimal
    precio_unitario: Decimal
    descuento_linea: Decimal
    subtotal: Decimal
    costo_unitario: Decimal | None


# ---------------------------------------------------------------------------
# Venta
# ---------------------------------------------------------------------------
class VentaCreate(BaseModel):
    """Crear venta completa en una sola llamada"""
    almacen_id: str | None = None  # Si no se envía, usa almacén principal
    cliente_id: str | None = None
    cliente_nombre: str | None = Field(None, max_length=200)  # Para venta rápida
    
    detalles: list[DetalleVentaCreate] = Field(..., min_length=1)
    
    descuento: Decimal = Field(default=Decimal("0"), ge=0)
    metodo_pago: MetodoPago = "EFECTIVO"
    monto_recibido: Decimal | None = None
    notas: str | None = None
    
    # Si True, completa la venta inmediatamente (descuenta stock)
    completar: bool = True


class VentaUpdate(BaseModel):
    """Solo para ventas PENDIENTES"""
    cliente_id: str | None = None
    cliente_nombre: str | None = None
    descuento: Decimal | None = None
    metodo_pago: MetodoPago | None = None
    notas: str | None = None


class VentaCompletarRequest(BaseModel):
    """Completar una venta pendiente"""
    metodo_pago: MetodoPago = "EFECTIVO"
    monto_recibido: Decimal | None = None


class VentaResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    negocio_id: str
    almacen_id: str
    numero: int
    cliente_id: str | None
    cliente_nombre: str | None
    subtotal: Decimal
    descuento: Decimal
    total: Decimal
    metodo_pago: str
    monto_recibido: Decimal | None
    cambio: Decimal | None
    estado: str
    usuario_id: str | None
    notas: str | None
    created_at: datetime
    completed_at: datetime | None
    cancelled_at: datetime | None
    detalles: list[DetalleVentaResponse] = []


class VentaListResponse(BaseModel):
    """Versión ligera para listados"""
    model_config = ConfigDict(from_attributes=True)

    id: str
    numero: int
    cliente_nombre: str | None
    total: Decimal
    metodo_pago: str
    estado: str
    created_at: datetime
    items_count: int = 0


# ---------------------------------------------------------------------------
# Reportes
# ---------------------------------------------------------------------------
class ResumenVentasDia(BaseModel):
    fecha: str
    total_ventas: int
    monto_total: Decimal
    ticket_promedio: Decimal
    por_metodo_pago: dict[str, Decimal]


class ResumenCaja(BaseModel):
    """Resumen de caja del día"""
    fecha: str
    ventas_completadas: int
    ventas_canceladas: int
    total_efectivo: Decimal
    total_qr: Decimal
    total_tarjeta: Decimal
    total_transferencia: Decimal
    total_credito: Decimal
    total_general: Decimal
