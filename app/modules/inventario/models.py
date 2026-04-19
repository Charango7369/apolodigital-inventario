"""
Modelos SQLAlchemy — módulo inventario

Tablas:
  negocios          → tenant principal, dueño de todo
  categorias        → agrupación de productos
  proveedores       → de dónde vienen los productos
  almacenes         → dónde se guarda el stock (uno por defecto)
  productos         → ficha del producto (sin precio ni stock directo)
  variantes         → SKU concreto: precio, atributos, foto
  stock             → cantidad actual por variante × almacén
  movimientos_stock → registro inmutable de cada cambio de stock
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, JSON,
    Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# ---------------------------------------------------------------------------
# Helper — UUID como string compatible con SQLite (tests) y PostgreSQL (prod)
# ---------------------------------------------------------------------------
def new_uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Negocio — tenant raíz
# ---------------------------------------------------------------------------
class Negocio(Base):
    __tablename__ = "negocios"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    propietario: Mapped[str] = mapped_column(String(200), nullable=False)
    telefono: Mapped[str | None] = mapped_column(String(30))
    moneda: Mapped[str] = mapped_column(String(10), default="BOB")
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relaciones
    categorias: Mapped[list["Categoria"]] = relationship(back_populates="negocio", cascade="all, delete-orphan")
    proveedores: Mapped[list["Proveedor"]] = relationship(back_populates="negocio", cascade="all, delete-orphan")
    almacenes: Mapped[list["Almacen"]] = relationship(back_populates="negocio", cascade="all, delete-orphan")
    productos: Mapped[list["Producto"]] = relationship(back_populates="negocio", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Negocio {self.nombre}>"


# ---------------------------------------------------------------------------
# Categoría
# ---------------------------------------------------------------------------
class Categoria(Base):
    __tablename__ = "categorias"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    negocio_id: Mapped[str] = mapped_column(String(36), ForeignKey("negocios.id"), nullable=False)
    nombre: Mapped[str] = mapped_column(String(100), nullable=False)
    icono: Mapped[str | None] = mapped_column(String(50))   # emoji o nombre de ícono
    activa: Mapped[bool] = mapped_column(Boolean, default=True)

    negocio: Mapped["Negocio"] = relationship(back_populates="categorias")
    productos: Mapped[list["Producto"]] = relationship(back_populates="categoria")

    def __repr__(self) -> str:
        return f"<Categoria {self.nombre}>"


# ---------------------------------------------------------------------------
# Proveedor
# ---------------------------------------------------------------------------
class Proveedor(Base):
    __tablename__ = "proveedores"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    negocio_id: Mapped[str] = mapped_column(String(36), ForeignKey("negocios.id"), nullable=False)
    nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    telefono: Mapped[str | None] = mapped_column(String(30))
    notas: Mapped[str | None] = mapped_column(Text)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)

    negocio: Mapped["Negocio"] = relationship(back_populates="proveedores")
    productos: Mapped[list["Producto"]] = relationship(back_populates="proveedor")

    def __repr__(self) -> str:
        return f"<Proveedor {self.nombre}>"


# ---------------------------------------------------------------------------
# Almacén
# ---------------------------------------------------------------------------
class Almacen(Base):
    __tablename__ = "almacenes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    negocio_id: Mapped[str] = mapped_column(String(36), ForeignKey("negocios.id"), nullable=False)
    nombre: Mapped[str] = mapped_column(String(100), nullable=False)
    ubicacion: Mapped[str | None] = mapped_column(String(200))
    es_principal: Mapped[bool] = mapped_column(Boolean, default=False)

    negocio: Mapped["Negocio"] = relationship(back_populates="almacenes")
    stocks: Mapped[list["Stock"]] = relationship(back_populates="almacen")
    movimientos: Mapped[list["MovimientoStock"]] = relationship(back_populates="almacen")

    def __repr__(self) -> str:
        return f"<Almacen {self.nombre}>"


# ---------------------------------------------------------------------------
# Producto — ficha maestra, sin precio ni stock directo
# ---------------------------------------------------------------------------
class Producto(Base):
    __tablename__ = "productos"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    negocio_id: Mapped[str] = mapped_column(String(36), ForeignKey("negocios.id"), nullable=False)
    categoria_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("categorias.id"))
    proveedor_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("proveedores.id"))

    nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    descripcion: Mapped[str | None] = mapped_column(Text)
    codigo_barras: Mapped[str | None] = mapped_column(String(100))
    unidad_medida: Mapped[str] = mapped_column(String(30), default="unidad")
    # unidad_medida: "unidad", "kg", "litro", "metro", etc.

    tiene_variantes: Mapped[bool] = mapped_column(Boolean, default=False)
    # False → el producto tiene exactamente una variante (la "por defecto")
    # True  → el usuario puede crear múltiples variantes (talla, color, etc.)

    es_servicio: Mapped[bool] = mapped_column(Boolean, default=False)
    # Los servicios no descuentan stock; se facturan pero no tienen movimientos

    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relaciones
    negocio: Mapped["Negocio"] = relationship(back_populates="productos")
    categoria: Mapped["Categoria | None"] = relationship(back_populates="productos")
    proveedor: Mapped["Proveedor | None"] = relationship(back_populates="productos")
    variantes: Mapped[list["Variante"]] = relationship(back_populates="producto", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Producto {self.nombre}>"


# ---------------------------------------------------------------------------
# Variante — SKU concreto con precio, foto y atributos
# ---------------------------------------------------------------------------
class Variante(Base):
    __tablename__ = "variantes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    producto_id: Mapped[str] = mapped_column(String(36), ForeignKey("productos.id"), nullable=False)

    sku: Mapped[str | None] = mapped_column(String(100))
    codigo_barras: Mapped[str | None] = mapped_column(String(50), index=True)
    # atributos: {"talla": "M", "color": "rojo"} — vacío {} para producto simple
    atributos: Mapped[dict | None] = mapped_column(JSON, default=dict)
    
    precio_venta: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    precio_costo: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    foto_url: Mapped[str | None] = mapped_column(String(500))
    activa: Mapped[bool] = mapped_column(Boolean, default=True)

    # Relaciones
    producto: Mapped["Producto"] = relationship(back_populates="variantes")
    stocks: Mapped[list["Stock"]] = relationship(back_populates="variante", cascade="all, delete-orphan")
    movimientos: Mapped[list["MovimientoStock"]] = relationship(back_populates="variante")

    def __repr__(self) -> str:
        return f"<Variante {self.sku or self.id[:8]}>"


# ---------------------------------------------------------------------------
# Stock — cantidad actual por variante × almacén
# ---------------------------------------------------------------------------
class Stock(Base):
    __tablename__ = "stock"
    __table_args__ = (
        UniqueConstraint("variante_id", "almacen_id", name="uq_stock_variante_almacen"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    variante_id: Mapped[str] = mapped_column(String(36), ForeignKey("variantes.id"), nullable=False)
    almacen_id: Mapped[str] = mapped_column(String(36), ForeignKey("almacenes.id"), nullable=False)

    cantidad_actual: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0)
    cantidad_minima: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0)
    # Cuando cantidad_actual <= cantidad_minima → aparece en /stock/alertas
    cantidad_maxima: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))

    actualizado_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relaciones
    variante: Mapped["Variante"] = relationship(back_populates="stocks")
    almacen: Mapped["Almacen"] = relationship(back_populates="stocks")

    def __repr__(self) -> str:
        return f"<Stock variante={self.variante_id[:8]} cant={self.cantidad_actual}>"


# ---------------------------------------------------------------------------
# MovimientoStock — registro inmutable de cada cambio de stock
# ---------------------------------------------------------------------------
TIPOS_MOVIMIENTO = (
    "ENTRADA_COMPRA",       # llegó mercancía del proveedor
    "SALIDA_VENTA",         # se vendió (lo dispara el módulo ventas)
    "AJUSTE_POSITIVO",      # corrección manual al alza (conteo físico)
    "AJUSTE_NEGATIVO",      # corrección manual a la baja (pérdida, rotura)
    "TRANSFERENCIA_ENTRADA",# llegó de otro almacén
    "TRANSFERENCIA_SALIDA", # salió hacia otro almacén
    "DEVOLUCION_CLIENTE",   # cliente devolvió producto
    "DEVOLUCION_PROVEEDOR", # se devolvió al proveedor
)


class MovimientoStock(Base):
    __tablename__ = "movimientos_stock"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    variante_id: Mapped[str] = mapped_column(String(36), ForeignKey("variantes.id"), nullable=False)
    almacen_id: Mapped[str] = mapped_column(String(36), ForeignKey("almacenes.id"), nullable=False)

    tipo: Mapped[str] = mapped_column(String(30), nullable=False)
    # Positivo = entra stock, negativo = sale stock
    cantidad: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    costo_unitario: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    # ID externo: número de factura, ID de venta, ID de orden de compra, etc.
    referencia_id: Mapped[str | None] = mapped_column(String(100))
    motivo: Mapped[str | None] = mapped_column(Text)

    # Quién lo registró (FK a usuarios — se activa cuando exista el módulo auth)
    usuario_id: Mapped[str | None] = mapped_column(String(36))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relaciones
    variante: Mapped["Variante"] = relationship(back_populates="movimientos")
    almacen: Mapped["Almacen"] = relationship(back_populates="movimientos")

    def __repr__(self) -> str:
        return f"<Movimiento {self.tipo} cant={self.cantidad}>"
