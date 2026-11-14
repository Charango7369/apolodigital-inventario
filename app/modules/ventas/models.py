"""
Modelos SQLAlchemy — módulo ventas y gestión financiera

Tablas:
  clientes            → datos del cliente (opcional para ventas rápidas, obligatorio para crédito)
  ventas              → cabecera de la venta/ticket (estado operativo y financiero)
  detalle_ventas      → líneas de productos vendidos
  pagos_venta         → registros inmutables de cobro (Fase 1)
  reversiones_pago    → eventos compensatorios de reversión de cobros (Fase 1)
  cuentas_por_cobrar  → cartera y financiamiento de ventas a crédito/parciales (Fase 1)
"""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from zoneinfo import ZoneInfo

from sqlalchemy import (
    Boolean, CheckConstraint, Date, DateTime, ForeignKey,
    ForeignKeyConstraint, Index, Numeric, String, Text,
    UniqueConstraint, event, text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.modules.auth.models import User


def new_uuid() -> str:
    return str(uuid.uuid4())


ZONA_COMERCIAL = ZoneInfo("America/La_Paz")


def fecha_comercial_hoy() -> date:
    """Devuelve la fecha actual en la zona horaria comercial del negocio (America/La_Paz)."""
    return datetime.now(ZONA_COMERCIAL).date()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class EstadoVenta(str, Enum):
    PENDIENTE = "PENDIENTE"      # Venta iniciada pero no cobrada
    COMPLETADA = "COMPLETADA"    # Pagada/entregada y finalizada operativamente
    CANCELADA = "CANCELADA"      # Anulada (devuelve stock)


class EstadoFinanciero(str, Enum):
    NOT_APPLICABLE = "NOT_APPLICABLE"    # Venta cancelada o sin obligación de cobro
    PAID = "PAID"                        # Liquidada al 100% con cobros verificados
    PAID_PRESUNTIVO = "PAID_PRESUNTIVO"  # Venta histórica de contado con cobros presuntivos pendientes de conciliar
    PARTIAL = "PARTIAL"                  # Con cobros verificados parciales y saldo pendiente
    CREDIT = "CREDIT"                    # Venta financiada a crédito


class EstadoPago(str, Enum):
    PENDING = "PENDING"          # Cobro en proceso (ej. QR generado pero aún no confirmado)
    CONFIRMED = "CONFIRMED"      # Cobro confirmado y acreditado
    FAILED = "FAILED"            # Cobro fallido o rechazado


class OrigenPago(str, Enum):
    OPERATIVO = "OPERATIVO"                          # Cobro generado en el sistema nuevo por cajero o pasarela
    HISTORICO_PRESUNTIVO = "HISTORICO_PRESUNTIVO"    # Cobro presuntivo migrado de ventas completadas previas


class EstadoConciliacion(str, Enum):
    APROBADA = "APROBADA"
    RECHAZADA = "RECHAZADA"


class EstadoCuentaCobrar(str, Enum):
    PENDIENTE = "PENDIENTE"                          # Cuenta activa con saldo por cobrar
    PAGADA = "PAGADA"                                # Totalmente saldada (saldo == 0)
    CANCELADA = "CANCELADA"                          # Venta anulada
    REQUIERE_CONCILIACION = "REQUIERE_CONCILIACION"  # Histórica sin vencimiento pactado


class MetodoPago(str, Enum):
    EFECTIVO = "EFECTIVO"
    QR = "QR"                    # QR bancario (muy usado en Bolivia)
    TARJETA = "TARJETA"
    TRANSFERENCIA = "TRANSFERENCIA"
    CREDITO = "CREDITO"          # Fiado / a crédito
    MIXTO = "MIXTO"              # Combinación de métodos


# ---------------------------------------------------------------------------
# Cliente (opcional para venta rápida, obligatorio para crédito)
# ---------------------------------------------------------------------------
class Cliente(Base):
    __tablename__ = "clientes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    negocio_id: Mapped[str] = mapped_column(String(36), ForeignKey("negocios.id"), nullable=False)
    
    nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    telefono: Mapped[str | None] = mapped_column(String(30))
    email: Mapped[str | None] = mapped_column(String(200))
    nit: Mapped[str | None] = mapped_column(String(20))  # NIT para facturación Bolivia
    direccion: Mapped[str | None] = mapped_column(Text)
    notas: Mapped[str | None] = mapped_column(Text)
    
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        default=lambda: datetime.now(timezone.utc)
    )

    # Relaciones
    ventas: Mapped[list["Venta"]] = relationship(back_populates="cliente")
    cuentas_cobrar: Mapped[list["CuentaPorCobrar"]] = relationship(
        back_populates="cliente",
        foreign_keys="[CuentaPorCobrar.negocio_id, CuentaPorCobrar.cliente_id]",
        overlaps="cuenta_cobrar,venta"
    )

    __table_args__ = (
        UniqueConstraint("negocio_id", "id", name="uq_clientes_negocio_id"),
        Index("ix_clientes_negocio_telefono", "negocio_id", "telefono"),
    )

    def __repr__(self) -> str:
        return f"<Cliente {self.nombre}>"


# ---------------------------------------------------------------------------
# Venta (cabecera)
# ---------------------------------------------------------------------------
class Venta(Base):
    __tablename__ = "ventas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    negocio_id: Mapped[str] = mapped_column(String(36), ForeignKey("negocios.id"), nullable=False)
    almacen_id: Mapped[str] = mapped_column(String(36), ForeignKey("almacenes.id"), nullable=False)
    
    # Número de ticket/factura (autogenerado por negocio)
    numero: Mapped[int] = mapped_column(nullable=False)
    
    # Cliente (opcional para ventas rápidas, obligatorio para crédito)
    cliente_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("clientes.id"))
    cliente_nombre: Mapped[str | None] = mapped_column(String(200))  # Para ventas sin cliente registrado
    cliente_nit: Mapped[str | None] = mapped_column(String(30))      # NIT/CI estructurado tributario (H-08)
    
    # Totales
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    descuento: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    
    # Pago histórico (preservado para compatibilidad)
    metodo_pago: Mapped[str] = mapped_column(String(20), default=MetodoPago.EFECTIVO.value)
    monto_recibido: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    cambio: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    
    # Estado operativo (PENDIENTE, COMPLETADA, CANCELADA)
    estado: Mapped[str] = mapped_column(String(20), default=EstadoVenta.PENDIENTE.value)
    # Estado financiero independiente (D02.1-C01)
    estado_financiero: Mapped[str] = mapped_column(
        String(30), 
        default=EstadoFinanciero.NOT_APPLICABLE.value, 
        nullable=False
    )
    
    # Quién atendió
    usuario_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))
    
    # Notas
    notas: Mapped[str | None] = mapped_column(Text)
    
    # Timestamps con zona horaria UTC
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        default=lambda: datetime.now(timezone.utc)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Relaciones operativas
    cliente: Mapped["Cliente | None"] = relationship(back_populates="ventas")
    detalles: Mapped[list["DetalleVenta"]] = relationship(
        back_populates="venta", 
        cascade="all, delete-orphan"
    )

    # Relaciones financieras (CONDICIÓN 1: SIN delete-orphan; inmutabilidad financiera)
    pagos: Mapped[list["PagoVenta"]] = relationship(
        back_populates="venta",
        foreign_keys="[PagoVenta.negocio_id, PagoVenta.venta_id]",
    )
    cuenta_cobrar: Mapped["CuentaPorCobrar | None"] = relationship(
        back_populates="venta",
        uselist=False,
        foreign_keys="[CuentaPorCobrar.negocio_id, CuentaPorCobrar.venta_id]",
        overlaps="cuentas_cobrar",
    )

    __table_args__ = (
        UniqueConstraint("negocio_id", "id", name="uq_ventas_negocio_id"),
        CheckConstraint(
            "estado_financiero IN ('NOT_APPLICABLE', 'PAID', 'PAID_PRESUNTIVO', 'PARTIAL', 'CREDIT')",
            name="chk_ventas_estado_financiero",
        ),
        Index("ix_ventas_negocio_numero", "negocio_id", "numero", unique=True),
        Index("ix_ventas_negocio_fecha", "negocio_id", "created_at"),
        Index("ix_ventas_estado", "negocio_id", "estado"),
        Index("ix_ventas_estado_financiero", "negocio_id", "estado_financiero"),
    )

    def __repr__(self) -> str:
        return f"<Venta #{self.numero} total={self.total} est={self.estado} fin={self.estado_financiero}>"


# ---------------------------------------------------------------------------
# Detalle de Venta (líneas de producto)
# ---------------------------------------------------------------------------
class DetalleVenta(Base):
    __tablename__ = "detalle_ventas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    venta_id: Mapped[str] = mapped_column(String(36), ForeignKey("ventas.id"), nullable=False)
    variante_id: Mapped[str] = mapped_column(String(36), ForeignKey("variantes.id"), nullable=False)
    
    # Snapshot del producto al momento de la venta
    producto_nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    variante_sku: Mapped[str | None] = mapped_column(String(100))
    
    # Cantidades y precios
    cantidad: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    precio_unitario: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    descuento_linea: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    
    # Costo al momento de venta (para reportes de ganancia)
    costo_unitario: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    # Relaciones
    venta: Mapped["Venta"] = relationship(back_populates="detalles")

    def __repr__(self) -> str:
        return f"<DetalleVenta {self.producto_nombre} x{self.cantidad}>"


# ---------------------------------------------------------------------------
# PagoVenta (registro inmutable del evento de cobro)
# ---------------------------------------------------------------------------
class PagoVenta(Base):
    __tablename__ = "pagos_venta"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    negocio_id: Mapped[str] = mapped_column(String(36), ForeignKey("negocios.id"), nullable=False)
    venta_id: Mapped[str] = mapped_column(String(36), nullable=False)
    
    monto: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    metodo_pago: Mapped[str] = mapped_column(String(20), nullable=False)
    
    # CONDICIÓN 2: NO CONFIRMAR POR DEFECTO. Debe ser explícito (PENDING, CONFIRMED, FAILED).
    estado: Mapped[str] = mapped_column(String(20), nullable=False)
    
    # PROCEDENCIA/EVIDENCIA DEL REGISTRO: OPERATIVO (nuevo sistema) vs HISTORICO_PRESUNTIVO (migrado)
    origen: Mapped[str] = mapped_column(
        String(30), 
        default=OrigenPago.OPERATIVO.value, 
        nullable=False
    )
    
    usuario_id: Mapped[str] = mapped_column(String(36), nullable=False)
    fecha_pago: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        default=lambda: datetime.now(timezone.utc), 
        nullable=False
    )
    referencia: Mapped[str | None] = mapped_column(String(100))

    # Relaciones (CONDICIÓN 1: SIN delete-orphan)
    venta: Mapped["Venta"] = relationship(
        back_populates="pagos",
        foreign_keys="[PagoVenta.negocio_id, PagoVenta.venta_id]",
    )
    usuario: Mapped["User"] = relationship(
        foreign_keys="[PagoVenta.negocio_id, PagoVenta.usuario_id]",
        overlaps="pagos,venta",
    )
    reversiones: Mapped[list["ReversionPago"]] = relationship(
        back_populates="pago",
        foreign_keys="[ReversionPago.negocio_id, ReversionPago.pago_id, ReversionPago.venta_id]",
    )
    conciliaciones: Mapped[list["ConciliacionPago"]] = relationship(
        back_populates="pago",
        foreign_keys="[ConciliacionPago.negocio_id, ConciliacionPago.pago_id, ConciliacionPago.venta_id]",
    )

    __table_args__ = (
        UniqueConstraint("negocio_id", "id", "venta_id", name="uq_pagos_venta_negocio_id_venta"),
        UniqueConstraint("negocio_id", "id", name="uq_pagos_venta_negocio_id"),
        ForeignKeyConstraint(
            ["negocio_id", "venta_id"],
            ["ventas.negocio_id", "ventas.id"],
            name="fk_pagos_venta_negocio_venta",
            ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["negocio_id", "usuario_id"],
            ["users.negocio_id", "users.id"],
            name="fk_pagos_venta_negocio_user",
            ondelete="RESTRICT"
        ),
        # CONDICIÓN 3: Restricciones CHECK
        CheckConstraint("monto > 0", name="chk_pagos_venta_monto_positivo"),
        CheckConstraint("estado IN ('PENDING', 'CONFIRMED', 'FAILED')", name="chk_pagos_venta_estado_valido"),
        CheckConstraint(
            "origen IN ('OPERATIVO', 'HISTORICO_PRESUNTIVO')",
            name="chk_pagos_venta_origen"
        ),
        Index("ix_pagos_venta_negocio_fecha", "negocio_id", "fecha_pago"),
        Index("ix_pagos_venta_negocio_metodo", "negocio_id", "metodo_pago"),
        Index("ix_pagos_venta_negocio_estado", "negocio_id", "estado"),
        Index("ix_pagos_venta_negocio_origen", "negocio_id", "origen"),
    )

    @property
    def total_revertido(self) -> Decimal:
        """Calcula la sumatoria de todas las reversiones aplicadas a este pago."""
        if not self.reversiones:
            return Decimal("0.00")
        return sum((Decimal(str(r.monto_revertido)) for r in self.reversiones), Decimal("0.00"))

    @property
    def saldo_reversible(self) -> Decimal:
        """Calcula el importe neto restante que aún puede ser revertido."""
        disponible = Decimal(str(self.monto)) - self.total_revertido
        return max(Decimal("0.00"), disponible)

    @property
    def es_historico_presuntivo(self) -> bool:
        return self.origen == OrigenPago.HISTORICO_PRESUNTIVO.value

    @property
    def esta_conciliado(self) -> bool:
        """Verifica si este pago cuenta con un evento formal de conciliación aprobado."""
        if not self.conciliaciones:
            return False
        return any(c.estado == EstadoConciliacion.APROBADA.value for c in self.conciliaciones)

    @property
    def es_cobro_verificado(self) -> bool:
        """
        Determina si este pago constituye cobro verificado real:
        - Debe estar CONFIRMED.
        - Si es OPERATIVO, es verificado directamente.
        - Si es HISTORICO_PRESUNTIVO, es verificado ÚNICAMENTE si cuenta con conciliación aprobada.
        """
        if self.estado != EstadoPago.CONFIRMED.value:
            return False
        if self.origen == OrigenPago.OPERATIVO.value:
            return True
        if self.origen == OrigenPago.HISTORICO_PRESUNTIVO.value:
            return self.esta_conciliado
        return False

    def __repr__(self) -> str:
        return f"<PagoVenta {self.id[:8]} monto={self.monto} metodo={self.metodo_pago} est={self.estado} orig={self.origen}>"


# ---------------------------------------------------------------------------
# ConciliacionPago (evento independiente e inmutable de conciliación histórica)
# ---------------------------------------------------------------------------
class ConciliacionPago(Base):
    __tablename__ = "conciliaciones_pago"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    negocio_id: Mapped[str] = mapped_column(String(36), ForeignKey("negocios.id"), nullable=False)
    pago_id: Mapped[str] = mapped_column(String(36), nullable=False)
    venta_id: Mapped[str] = mapped_column(String(36), nullable=False)
    usuario_id: Mapped[str] = mapped_column(String(36), nullable=False)
    
    fecha_conciliacion: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        default=lambda: datetime.now(timezone.utc), 
        nullable=False
    )
    motivo: Mapped[str] = mapped_column(Text, nullable=False)
    referencia_documental: Mapped[str] = mapped_column(String(100), nullable=False)
    estado: Mapped[str] = mapped_column(
        String(20), 
        default=EstadoConciliacion.APROBADA.value, 
        nullable=False
    )
    notas: Mapped[str | None] = mapped_column(Text)

    # Relaciones
    pago: Mapped["PagoVenta"] = relationship(
        back_populates="conciliaciones",
        foreign_keys="[ConciliacionPago.negocio_id, ConciliacionPago.pago_id, ConciliacionPago.venta_id]",
    )
    usuario: Mapped["User"] = relationship(
        foreign_keys="[ConciliacionPago.negocio_id, ConciliacionPago.usuario_id]",
        overlaps="pago,conciliaciones",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["negocio_id", "pago_id", "venta_id"],
            ["pagos_venta.negocio_id", "pagos_venta.id", "pagos_venta.venta_id"],
            name="fk_conciliaciones_pago_venta",
            ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["negocio_id", "usuario_id"],
            ["users.negocio_id", "users.id"],
            name="fk_conciliaciones_negocio_user",
            ondelete="RESTRICT"
        ),
        CheckConstraint("estado IN ('APROBADA', 'RECHAZADA')", name="chk_conciliaciones_estado_valido"),
        Index("ix_conciliaciones_negocio_fecha", "negocio_id", "fecha_conciliacion"),
        Index(
            "uq_conciliaciones_negocio_pago_aprobada",
            "negocio_id", "pago_id",
            unique=True,
            postgresql_where=text("estado = 'APROBADA'"),
            sqlite_where=text("estado = 'APROBADA'"),
        ),
    )

    def __repr__(self) -> str:
        return f"<ConciliacionPago {self.id[:8]} pago={self.pago_id[:8]} est={self.estado}>"


# ---------------------------------------------------------------------------
# Inmutabilidad estricta de Conciliaciones de Pago (Fase 2.2.2)
# ---------------------------------------------------------------------------
@event.listens_for(ConciliacionPago, "before_update")
def _prevent_conciliacion_update(mapper, connection, target):
    raise RuntimeError("Las conciliaciones de pago aprobadas son inmutables y no admiten modificaciones.")


@event.listens_for(ConciliacionPago, "before_delete")
def _prevent_conciliacion_delete(mapper, connection, target):
    raise RuntimeError("Las conciliaciones de pago son registros de auditoría permanentes y no admiten eliminación.")


# ---------------------------------------------------------------------------
# ReversionPago (registro compensatorio de reversión de cobros)
# ---------------------------------------------------------------------------
class ReversionPago(Base):
    __tablename__ = "reversiones_pago"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    negocio_id: Mapped[str] = mapped_column(String(36), ForeignKey("negocios.id"), nullable=False)
    pago_id: Mapped[str] = mapped_column(String(36), nullable=False)
    venta_id: Mapped[str] = mapped_column(String(36), nullable=False)
    
    monto_revertido: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    motivo: Mapped[str] = mapped_column(Text, nullable=False)
    usuario_id: Mapped[str] = mapped_column(String(36), nullable=False)
    
    fecha_reversion: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        default=lambda: datetime.now(timezone.utc), 
        nullable=False
    )
    
    # REGLA H-15: default=False (reversión contable no implica devolución física de efectivo)
    es_devolucion_efectivo: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Relaciones
    pago: Mapped["PagoVenta"] = relationship(
        back_populates="reversiones",
        foreign_keys="[ReversionPago.negocio_id, ReversionPago.pago_id, ReversionPago.venta_id]",
    )
    usuario: Mapped["User"] = relationship(
        foreign_keys="[ReversionPago.negocio_id, ReversionPago.usuario_id]",
        overlaps="pago,reversiones",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["negocio_id", "pago_id", "venta_id"],
            ["pagos_venta.negocio_id", "pagos_venta.id", "pagos_venta.venta_id"],
            name="fk_reversiones_pago_venta",
            ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["negocio_id", "usuario_id"],
            ["users.negocio_id", "users.id"],
            name="fk_reversiones_negocio_user",
            ondelete="RESTRICT"
        ),
        # CONDICIÓN 3: Restricciones CHECK
        CheckConstraint("monto_revertido > 0", name="chk_reversiones_pago_monto_positivo"),
        Index("ix_reversiones_negocio_fecha", "negocio_id", "fecha_reversion"),
    )

    def __repr__(self) -> str:
        return f"<ReversionPago {self.id[:8]} monto={self.monto_revertido} efectivo={self.es_devolucion_efectivo}>"


# ---------------------------------------------------------------------------
# CuentaPorCobrar (gestión de cartera y créditos)
# ---------------------------------------------------------------------------
class CuentaPorCobrar(Base):
    __tablename__ = "cuentas_por_cobrar"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    negocio_id: Mapped[str] = mapped_column(String(36), ForeignKey("negocios.id"), nullable=False)
    venta_id: Mapped[str] = mapped_column(String(36), nullable=False)
    cliente_id: Mapped[str] = mapped_column(String(36), nullable=False)
    
    # REGLA H-13: Definición contable unívoca
    monto_total_venta: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    monto_inicial_financiado: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    saldo_pendiente: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    
    # REGLA H-14: Nullable para históricos (sin inventar 30 días)
    fecha_vencimiento: Mapped[date | None] = mapped_column(Date, nullable=True)
    
    # REGLA H-16: Estados base persistidos
    estado: Mapped[str] = mapped_column(
        String(30), 
        default=EstadoCuentaCobrar.PENDIENTE.value, 
        nullable=False
    )
    notas: Mapped[str | None] = mapped_column(Text)
    
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        default=lambda: datetime.now(timezone.utc), 
        nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        default=lambda: datetime.now(timezone.utc), 
        onupdate=lambda: datetime.now(timezone.utc), 
        nullable=False
    )

    # Relaciones
    venta: Mapped["Venta"] = relationship(
        back_populates="cuenta_cobrar",
        foreign_keys="[CuentaPorCobrar.negocio_id, CuentaPorCobrar.venta_id]",
        overlaps="cuentas_cobrar",
    )
    cliente: Mapped["Cliente"] = relationship(
        back_populates="cuentas_cobrar",
        foreign_keys="[CuentaPorCobrar.negocio_id, CuentaPorCobrar.cliente_id]",
        overlaps="cuenta_cobrar,venta",
    )

    __table_args__ = (
        UniqueConstraint("negocio_id", "venta_id", name="uq_cxc_negocio_venta"),
        ForeignKeyConstraint(
            ["negocio_id", "venta_id"],
            ["ventas.negocio_id", "ventas.id"],
            name="fk_cxc_negocio_venta",
            ondelete="RESTRICT"
        ),
        ForeignKeyConstraint(
            ["negocio_id", "cliente_id"],
            ["clientes.negocio_id", "clientes.id"],
            name="fk_cxc_negocio_cliente",
            ondelete="RESTRICT"
        ),
        # CONDICIÓN 3: Restricciones CHECK
        CheckConstraint("monto_total_venta > 0", name="chk_cxc_monto_total_positivo"),
        CheckConstraint("monto_inicial_financiado >= 0 AND monto_inicial_financiado <= monto_total_venta", name="chk_cxc_monto_inicial_rango"),
        CheckConstraint("saldo_pendiente >= 0 AND saldo_pendiente <= monto_total_venta", name="chk_cxc_saldo_pendiente_rango"),
        CheckConstraint("estado IN ('PENDIENTE', 'PAGADA', 'CANCELADA', 'REQUIERE_CONCILIACION')", name="chk_cxc_estado_valido"),
        Index("ix_cxc_negocio_vencimiento", "negocio_id", "fecha_vencimiento"),
        Index("ix_cxc_negocio_estado", "negocio_id", "estado"),
    )

    # CONDICIÓN 5: Fecha comercial en zona horaria America/La_Paz (H-16 dinámico)
    @property
    def esta_vencida(self) -> bool:
        """
        Determina dinámicamente si la cuenta está vencida según la fecha comercial de Bolivia.
        Cuentas sin fecha de vencimiento pactada o con estado REQUIERE_CONCILIACION
        NO se consideran vencidas artificialmente.
        """
        if self.saldo_pendiente <= Decimal("0") or self.fecha_vencimiento is None:
            return False
        if self.estado == EstadoCuentaCobrar.REQUIERE_CONCILIACION.value:
            return False
        return self.fecha_vencimiento < fecha_comercial_hoy()

    @property
    def dias_mora(self) -> int:
        """Calcula dinámicamente los días de mora transcurridos desde la fecha de vencimiento."""
        if not self.esta_vencida or self.fecha_vencimiento is None:
            return 0
        delta = (fecha_comercial_hoy() - self.fecha_vencimiento).days
        return max(0, delta)

    def __repr__(self) -> str:
        return f"<CuentaPorCobrar {self.id[:8]} venta={self.venta_id[:8]} saldo={self.saldo_pendiente} est={self.estado}>"


# ---------------------------------------------------------------------------
# RegistroIdempotencia (persistencia de llaves y respuestas — Fase 2.5.1)
# ---------------------------------------------------------------------------
class RegistroIdempotencia(Base):
    __tablename__ = "idempotencia_claves"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    negocio_id: Mapped[str] = mapped_column(String(36), ForeignKey("negocios.id"), nullable=False)
    metodo: Mapped[str] = mapped_column(String(10), default="POST", nullable=False)
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    usuario_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="SUCCESS", nullable=False)  # SUCCESS, FAILED, PROCESSING
    response_code: Mapped[int | None] = mapped_column(nullable=True)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True
    )

    # Relación multi-tenant con User
    usuario: Mapped["User"] = relationship(
        foreign_keys="[RegistroIdempotencia.negocio_id, RegistroIdempotencia.usuario_id]",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["negocio_id", "usuario_id"],
            ["users.negocio_id", "users.id"],
            name="fk_idempotencia_negocio_usuario",
            ondelete="CASCADE"
        ),
        UniqueConstraint(
            "negocio_id", "metodo", "endpoint", "idempotency_key",
            name="uq_idempotencia_negocio_metodo_endpoint_key"
        ),
        Index("ix_idempotencia_lookup", "negocio_id", "metodo", "endpoint", "idempotency_key"),
        Index("ix_idempotencia_expires_at", "expires_at"),
    )

    def __repr__(self) -> str:
        return f"<RegistroIdempotencia key={self.idempotency_key[:8]} met={self.metodo} ep={self.endpoint} status={self.status}>"
