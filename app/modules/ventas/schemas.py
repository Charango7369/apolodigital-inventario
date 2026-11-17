"""
Schemas Pydantic — módulo ventas
"""

from datetime import datetime, date
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
# Pagos
# ---------------------------------------------------------------------------
MetodoPagoDetalle = Literal["EFECTIVO", "QR", "TARJETA", "TRANSFERENCIA"]


class PagoCreate(BaseModel):
    """
    Solicitud de registro de pago.
    Permite declarar monto, método y referencia. Si el campo 'estado' no se especifica,
    el backend aplica la regla de negocio: EFECTIVO y TARJETA nacen CONFIRMED,
    mientras QR y TRANSFERENCIA nacen PENDING salvo confirmación explícita.
    """
    monto: Decimal = Field(..., gt=Decimal("0"), description="Importe del pago (debe ser estrictamente mayor a 0)")
    metodo_pago: MetodoPagoDetalle = Field(..., description="Método de pago (EFECTIVO, QR, TARJETA, TRANSFERENCIA)")
    estado: Literal["CONFIRMED", "PENDING"] | None = Field(
        default=None,
        description="Estado opcional (CONFIRMED/PENDING). Si se omite, el backend asigna según reglas de negocio"
    )
    referencia: str | None = Field(None, max_length=100, description="Referencia o identificador del comprobante")


class PagoConfirmarRequest(BaseModel):
    """Solicitud explícita para confirmar un pago PENDING (ej. comprobante QR verificado)"""
    referencia_confirmacion: str | None = Field(None, max_length=100, description="Código de autorización o transacción")
    notas: str | None = Field(None, description="Observaciones de la verificación")


class PagoMarcarFallidoRequest(BaseModel):
    """Solicitud para marcar un pago PENDING como fallido o rechazado"""
    motivo: str = Field(..., min_length=3, max_length=255, description="Motivo por el cual el pago se marca como fallido")


class PagoResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    negocio_id: str
    venta_id: str
    monto: Decimal
    metodo_pago: str
    estado: str
    origen: str = "OPERATIVO"
    esta_conciliado: bool = False
    es_cobro_verificado: bool = False
    usuario_id: str
    fecha_pago: datetime
    referencia: str | None
    total_revertido: Decimal = Decimal("0.00")
    saldo_reversible: Decimal = Decimal("0.00")


# ---------------------------------------------------------------------------
# Conciliaciones de Pago Histórico (Fase 2.2.1)
# ---------------------------------------------------------------------------
class ConciliacionPagoCreate(BaseModel):
    """Solicitud de conciliación documental para pagos presuntivos históricos"""
    motivo: str = Field(..., min_length=3, description="Justificación de la conciliación")
    referencia_documental: str = Field(..., min_length=1, max_length=100, description="Comprobante, recibo o extracto bancario físico/digital")
    notas: str | None = Field(None, description="Observaciones adicionales")


class ConciliacionPagoResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    negocio_id: str
    pago_id: str
    venta_id: str
    usuario_id: str
    fecha_conciliacion: datetime
    motivo: str
    referencia_documental: str
    estado: str
    notas: str | None


# ---------------------------------------------------------------------------
# Reversiones de Pago
# ---------------------------------------------------------------------------
class ReversionPagoCreate(BaseModel):
    """Solicitud de reversión contable (total o parcial)"""
    monto: Decimal | None = Field(None, gt=Decimal("0"), description="Monto a revertir (None para reversión total)")
    motivo: str = Field(..., min_length=3, description="Justificación obligatoria de la reversión")
    es_devolucion_efectivo: bool = Field(default=False, description="Indica si hubo devolución física de dinero en caja")


class ReversionPagoResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    negocio_id: str
    pago_id: str
    venta_id: str
    monto_revertido: Decimal
    motivo: str
    usuario_id: str
    fecha_reversion: datetime
    es_devolucion_efectivo: bool


# ---------------------------------------------------------------------------
# Historial de Pagos
# ---------------------------------------------------------------------------
class PagoHistorialItemResponse(BaseModel):
    pago: PagoResponse
    reversiones: list[ReversionPagoResponse] = []
    conciliaciones: list[ConciliacionPagoResponse] = []
    neto_cobrado: Decimal
    es_historico_presuntivo: bool
    es_cobro_verificado: bool


class HistorialPagosVentaResponse(BaseModel):
    venta_id: str
    total_venta: Decimal
    total_cobrado_verificado: Decimal
    total_cobrado_presuntivo: Decimal
    total_revertido: Decimal
    saldo_operativo_pendiente: Decimal
    importe_presuntivo_pendiente: Decimal
    saldo_pendiente: Decimal
    estado_financiero: str
    pagos: list[PagoHistorialItemResponse] = []


# ---------------------------------------------------------------------------
# Cuentas por Cobrar y Cartera (Fase 2.3)
# ---------------------------------------------------------------------------
class CuentaCobrarResumenResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    venta_id: str
    cliente_id: str
    monto_total_venta: Decimal
    monto_inicial_financiado: Decimal
    saldo_pendiente: Decimal
    fecha_vencimiento: date | None
    estado: str
    dias_mora: int = 0
    esta_vencida: bool = False


class AbonoCreate(BaseModel):
    """Solicitud de abono / amortización para una cuenta por cobrar"""
    monto: Decimal = Field(..., gt=Decimal("0"), description="Importe del abono (debe ser estrictamente mayor a 0)")
    metodo_pago: MetodoPagoDetalle = Field(..., description="Método de pago (EFECTIVO, QR, TARJETA, TRANSFERENCIA)")
    referencia: str | None = Field(None, max_length=100, description="Referencia de pago o comprobante bancario")
    confirmar_inmediato: bool | None = Field(
        None, 
        description="Si se omite, EFECTIVO/TARJETA nacen CONFIRMED y QR/TRANSFERENCIA nacen PENDING"
    )


class CuentaPorCobrarDetalleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    negocio_id: str
    venta_id: str
    numero_ticket: int | None = None
    cliente_id: str
    cliente_nombre: str | None = None
    cliente_nit: str | None = None
    cliente_telefono: str | None = None
    monto_total_venta: Decimal
    monto_inicial_financiado: Decimal
    cobros_verificados_netos: Decimal
    cobros_presuntivos_netos: Decimal
    saldo_operativo: Decimal
    saldo_pendiente: Decimal
    fecha_vencimiento: date | None
    dias_mora: int = 0
    esta_vencida: bool = False
    estado: str
    notas: str | None = None
    created_at: datetime
    updated_at: datetime


class AbonoResponse(BaseModel):
    cuenta_id: str
    venta_id: str
    cliente_id: str
    monto_abonado: Decimal
    saldo_operativo_anterior: Decimal
    saldo_operativo_nuevo: Decimal
    estado_cuenta: str
    estado_financiero_venta: str
    pago: PagoResponse


class CarteraClienteItemResponse(BaseModel):
    cliente_id: str
    cliente_nombre: str
    cliente_nit: str | None = None
    cliente_telefono: str | None = None
    total_cuentas: int
    cuentas_activas: int
    cuentas_vencidas: int
    monto_total_financiado: Decimal
    total_cobrado_verificado: Decimal
    saldo_operativo_total: Decimal
    importe_presuntivo_total: Decimal
    max_dias_mora: int = 0


class EstadoCuentaClienteResponse(BaseModel):
    cliente_id: str
    cliente_nombre: str
    cliente_nit: str | None = None
    cliente_telefono: str | None = None
    resumen: CarteraClienteItemResponse
    cuentas: list[CuentaPorCobrarDetalleResponse] = []


# ---------------------------------------------------------------------------
# Venta
# ---------------------------------------------------------------------------
class VentaCreate(BaseModel):
    """Crear venta completa en una sola llamada (soporta modo tradicional y financiero multi-pago)"""
    almacen_id: str | None = None  # Si no se envía, usa almacén principal
    cliente_id: str | None = None
    cliente_nombre: str | None = Field(None, max_length=200)  # Para venta rápida
    cliente_nit: str | None = Field(None, max_length=30)      # NIT/CI tributario (H-08)
    
    detalles: list[DetalleVentaCreate] = Field(..., min_length=1)
    
    descuento: Decimal = Field(default=Decimal("0"), ge=0)
    metodo_pago: MetodoPago = "EFECTIVO"
    monto_recibido: Decimal | None = None
    notas: str | None = None
    
    # Si True, completa la venta inmediatamente (descuenta stock)
    completar: bool = True

    # Extensiones financieras Fase 2
    pagos: list[PagoCreate] = Field(default_factory=list, description="Lista opcional de pagos divididos/mixtos")
    es_credito: bool = Field(default=False, description="Indica si la venta es a crédito o tiene saldo financiado")
    fecha_vencimiento: date | None = Field(None, description="Fecha de vencimiento para ventas a crédito")
    idempotency_key: str | None = Field(None, max_length=64, description="Clave de idempotencia para evitar reintentos duplicados")


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
    cliente_nit: str | None = None
    subtotal: Decimal
    descuento: Decimal
    total: Decimal
    metodo_pago: str
    monto_recibido: Decimal | None
    cambio: Decimal | None
    estado: str
    estado_financiero: str = "NOT_APPLICABLE"
    usuario_id: str | None
    notas: str | None
    created_at: datetime
    completed_at: datetime | None
    cancelled_at: datetime | None
    detalles: list[DetalleVentaResponse] = []
    pagos: list[PagoResponse] = []
    cuenta_cobrar: CuentaCobrarResumenResponse | None = None


VentaFinancieraResponse = VentaResponse


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
    total_credito_otorgado: Decimal = Field(default=Decimal("0"))
    total_cobrado: Decimal = Field(default=Decimal("0"))
    monto_total_vendido: Decimal = Field(default=Decimal("0"))


# ---------------------------------------------------------------------------
# Cancelación de Ventas (Bloque 2.4)
# ---------------------------------------------------------------------------
class VentaCancelacionRequest(BaseModel):
    motivo: str = Field(..., min_length=3, description="Motivo descriptivo de la anulación")
    devolver_efectivo: bool = Field(
        default=False, 
        description="Si es True y existen cobros en EFECTIVO, se registra la salida física de caja en la reversión compensatoria"
    )


class VentaCancelacionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    venta_id: str
    numero_ticket: int
    estado_operativo: str
    estado_financiero: str
    total_venta: Decimal
    total_revertido: Decimal
    es_devolucion_efectivo: bool
    reversiones_generadas: int
    pagos_pendientes_cancelados: int
    movimientos_inventario_reingresados: int
    cuenta_cobrar_cancelada: bool
    cancelled_at: datetime
    motivo: str
