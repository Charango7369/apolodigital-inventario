"""
Modelos SQLAlchemy — módulo ventas

Tablas:
  clientes       → datos del cliente (opcional para ventas)
  ventas         → cabecera de la venta/ticket
  detalle_ventas → líneas de productos vendidos
"""

import uuid
from datetime import datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index,
    Numeric, String, Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def new_uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class EstadoVenta(str, Enum):
    PENDIENTE = "PENDIENTE"      # Venta iniciada pero no cobrada
    COMPLETADA = "COMPLETADA"    # Pagada y finalizada
    CANCELADA = "CANCELADA"      # Anulada (devuelve stock)


class MetodoPago(str, Enum):
    EFECTIVO = "EFECTIVO"
    QR = "QR"                    # QR bancario (muy usado en Bolivia)
    TARJETA = "TARJETA"
    TRANSFERENCIA = "TRANSFERENCIA"
    CREDITO = "CREDITO"          # Fiado / a crédito
    MIXTO = "MIXTO"              # Combinación de métodos


# ---------------------------------------------------------------------------
# Cliente (opcional)
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relaciones
    ventas: Mapped[list["Venta"]] = relationship(back_populates="cliente")

    __table_args__ = (
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
    
    # Cliente (opcional para ventas rápidas)
    cliente_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("clientes.id"))
    cliente_nombre: Mapped[str | None] = mapped_column(String(200))  # Para ventas sin cliente registrado
    
    # Totales
    subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    descuento: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    
    # Pago
    metodo_pago: Mapped[str] = mapped_column(String(20), default=MetodoPago.EFECTIVO.value)
    monto_recibido: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    cambio: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    
    # Estado
    estado: Mapped[str] = mapped_column(String(20), default=EstadoVenta.PENDIENTE.value)
    
    # Quién atendió
    usuario_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"))
    
    # Notas
    notas: Mapped[str | None] = mapped_column(Text)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)

    # Relaciones
    cliente: Mapped["Cliente | None"] = relationship(back_populates="ventas")
    detalles: Mapped[list["DetalleVenta"]] = relationship(
        back_populates="venta", 
        cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_ventas_negocio_numero", "negocio_id", "numero", unique=True),
        Index("ix_ventas_negocio_fecha", "negocio_id", "created_at"),
        Index("ix_ventas_estado", "negocio_id", "estado"),
    )

    def __repr__(self) -> str:
        return f"<Venta #{self.numero} total={self.total}>"


# ---------------------------------------------------------------------------
# Detalle de Venta (líneas)
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
