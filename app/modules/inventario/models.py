"""
Modelos SQLAlchemy — módulo inventario

Tablas:
  negocios          → tenant principal
  categorias        → agrupación de productos
  proveedores       → de dónde vienen los productos
  almacenes         → dónde se guarda el stock
  productos         → ficha del producto
  variantes         → SKU concreto (precio_venta, atributos JSONB)
  lotes             → unidad física de inventario (NUEVO)
                      vive aquí: cantidad real, costo real, vencimiento
  stock             → caché denormalizado: SUM(lotes activos) por (variante × almacén)
  movimientos_stock → registro inmutable, con referencia a lote consumido
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, Date, DateTime, ForeignKey, Index, JSON,
    Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# ---------------------------------------------------------------------------
# Helper — UUID como string compatible con SQLite (tests) y PostgreSQL (prod)
# ---------------------------------------------------------------------------
def new_uuid() -> str:
    return str(uuid.uuid4())


# JSONB en Postgres, JSON (que en SQLite es TEXT) en tests locales.
# Esto permite que la suite de tests siga corriendo con SQLite sin conocer JSONB.
JsonAttr = JSONB().with_variant(JSON, "sqlite")


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
    icono: Mapped[str | None] = mapped_column(String(50))
    activa: Mapped[bool] = mapped_column(Boolean, default=True)

    # ── Bloque B ─────────────────────────────────────────────────────────
    # Default heredado por productos al crearse. NO propaga si cambias.
    # Usar POST /categorias/{id}/aplicar-default-a-productos para forzarlo.
    controla_vencimiento_default: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False,
    )

    # Sets de valores válidos por atributo, ej:
    #   {"talla": ["S","M","L","XL"], "color": ["rojo","azul"]}
    # Si vacío {}, se permite cualquier atributo libre.
    # Si tiene contenido, se valida en crear/editar variante.
    # Override admin: query param ?bypass_validation=true.
    atributos_esperados: Mapped[dict] = mapped_column(
        JsonAttr, default=dict, nullable=False,
    )
    # ─────────────────────────────────────────────────────────────────────

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
    lotes: Mapped[list["Lote"]] = relationship(back_populates="almacen")
    movimientos: Mapped[list["MovimientoStock"]] = relationship(back_populates="almacen")

    def __repr__(self) -> str:
        return f"<Almacen {self.nombre}>"


# ---------------------------------------------------------------------------
# Producto — ficha maestra
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

    tiene_variantes: Mapped[bool] = mapped_column(Boolean, default=False)
    es_servicio: Mapped[bool] = mapped_column(Boolean, default=False)
    # NUEVO: indica si el producto maneja vencimiento.
    # Si False, los lotes pueden tener fecha_vencimiento NULL sin alarma.
    # Si True, se exige fecha_vencimiento al crear lotes (validación en service).
    controla_vencimiento: Mapped[bool] = mapped_column(Boolean, default=False)

    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    negocio: Mapped["Negocio"] = relationship(back_populates="productos")
    categoria: Mapped["Categoria | None"] = relationship(back_populates="productos")
    proveedor: Mapped["Proveedor | None"] = relationship(back_populates="productos")
    variantes: Mapped[list["Variante"]] = relationship(back_populates="producto", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Producto {self.nombre}>"


# ---------------------------------------------------------------------------
# Variante — SKU concreto
# ---------------------------------------------------------------------------
class Variante(Base):
    __tablename__ = "variantes"
    __table_args__ = (
        # Índice GIN sobre atributos JSONB. En SQLite se ignora silenciosamente.
        Index(
            "ix_variantes_atributos_gin",
            "atributos",
            postgresql_using="gin",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    producto_id: Mapped[str] = mapped_column(String(36), ForeignKey("productos.id"), nullable=False)

    sku: Mapped[str | None] = mapped_column(String(100))
    codigo_barras: Mapped[str | None] = mapped_column(String(50), index=True)

    # Atributos dinámicos: {"talla": "M", "volumen": "50ml", "color": "rojo", ...}
    # JSONB en Postgres con índice GIN → consultable: atributos @> '{"talla":"M"}'
    atributos: Mapped[dict | None] = mapped_column(JsonAttr, default=dict)

    precio_venta: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    # precio_costo se conserva como "costo de referencia / sugerido" para UI
    # (autocompletar al crear lotes nuevos). NO es la fuente de verdad para
    # cálculo de utilidad — eso vive en Lote.costo_unitario del lote vendido.
    precio_costo: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    foto_url: Mapped[str | None] = mapped_column(String(500))
    activa: Mapped[bool] = mapped_column(Boolean, default=True)

    producto: Mapped["Producto"] = relationship(back_populates="variantes")
    stocks: Mapped[list["Stock"]] = relationship(back_populates="variante", cascade="all, delete-orphan")
    lotes: Mapped[list["Lote"]] = relationship(back_populates="variante", cascade="all, delete-orphan")
    movimientos: Mapped[list["MovimientoStock"]] = relationship(back_populates="variante")

    def __repr__(self) -> str:
        return f"<Variante {self.sku or self.id[:8]}>"


# ---------------------------------------------------------------------------
# Lote — unidad física de inventario (NUEVO)
# ---------------------------------------------------------------------------
class Lote(Base):
    """
    Un Lote representa una entrada física de inventario:
    - viene de una compra (o de un ajuste positivo, transferencia, etc.)
    - tiene una cantidad inicial y una cantidad actual (que se va consumiendo)
    - tiene un costo unitario propio (NO depende de Variante.precio_costo)
    - puede tener fecha_vencimiento (NULL si el producto no controla vencimiento)

    Reglas:
    - Toda variante física (no servicio) consume y entrega stock vía Lotes.
    - El descuento por venta usa FEFO: el lote con vencimiento más próximo primero.
    - cantidad_actual nunca debe quedar negativa.
    - Cuando cantidad_actual llega a 0, el lote sigue activo para histórico
      (se usa activo=False solo para lotes dados de baja completos).
    """
    __tablename__ = "lotes"
    __table_args__ = (
        # Índice clave para FEFO: filtrar por (variante, almacén, activo) y
        # ordenar por fecha_vencimiento en una sola pasada.
        Index(
            "ix_lotes_fefo",
            "variante_id", "almacen_id", "activo", "fecha_vencimiento",
        ),
        # Para reporte "próximos a vencer" cross-negocio:
        Index("ix_lotes_vencimiento", "fecha_vencimiento"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    variante_id: Mapped[str] = mapped_column(String(36), ForeignKey("variantes.id"), nullable=False)
    almacen_id: Mapped[str] = mapped_column(String(36), ForeignKey("almacenes.id"), nullable=False)

    # Identificador del lote según el proveedor / etiqueta del fabricante
    codigo_lote: Mapped[str | None] = mapped_column(String(50))

    # NULL si el producto no controla vencimiento (ropa, ferretería)
    fecha_vencimiento: Mapped[date | None] = mapped_column(Date)

    # Fecha en que entró al inventario (NO confundir con fecha de fabricación)
    fecha_ingreso: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Cantidad con la que entró (no cambia)
    cantidad_inicial: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    # Cantidad disponible hoy (se decrementa con cada salida)
    cantidad_actual: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)

    # Costo unitario al momento de la compra. Fuente de verdad para utilidad.
    costo_unitario: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    # ID externo de la compra/factura que originó el lote (trazabilidad)
    referencia_compra: Mapped[str | None] = mapped_column(String(100))

    # activo=False solo para lotes dados de baja completos (vencimiento, robo...)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)

    notas: Mapped[str | None] = mapped_column(Text)

    variante: Mapped["Variante"] = relationship(back_populates="lotes")
    almacen: Mapped["Almacen"] = relationship(back_populates="lotes")
    movimientos: Mapped[list["MovimientoStock"]] = relationship(back_populates="lote")

    def __repr__(self) -> str:
        venc = self.fecha_vencimiento.isoformat() if self.fecha_vencimiento else "sin venc"
        return f"<Lote {self.codigo_lote or self.id[:8]} cant={self.cantidad_actual} vence={venc}>"


# ---------------------------------------------------------------------------
# Stock — caché denormalizado por (variante × almacén)
# ---------------------------------------------------------------------------
class Stock(Base):
    """
    OJO: Stock NO es la fuente de verdad. La verdad está en Lote.
    Stock.cantidad_actual = SUM(lotes.cantidad_actual WHERE activo=True
                                AND variante=X AND almacen=Y)

    Existe como caché para que las queries comunes (alertas de stock bajo,
    listado rápido de inventario) no tengan que hacer GROUP BY en cada call.

    La sincronización es responsabilidad del service (`_sincronizar_stock`)
    y se ejecuta después de cada operación que toque Lote.
    """
    __tablename__ = "stock"
    __table_args__ = (
        UniqueConstraint("variante_id", "almacen_id", name="uq_stock_variante_almacen"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    variante_id: Mapped[str] = mapped_column(String(36), ForeignKey("variantes.id"), nullable=False)
    almacen_id: Mapped[str] = mapped_column(String(36), ForeignKey("almacenes.id"), nullable=False)

    cantidad_actual: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0)
    cantidad_minima: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0)
    cantidad_maxima: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))

    actualizado_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    variante: Mapped["Variante"] = relationship(back_populates="stocks")
    almacen: Mapped["Almacen"] = relationship(back_populates="stocks")

    def __repr__(self) -> str:
        return f"<Stock variante={self.variante_id[:8]} cant={self.cantidad_actual}>"


# ---------------------------------------------------------------------------
# MovimientoStock — registro inmutable de cada cambio
# ---------------------------------------------------------------------------
TIPOS_MOVIMIENTO = (
    "ENTRADA_COMPRA",        # llegó mercancía del proveedor (crea lote)
    "SALIDA_VENTA",          # se vendió (consume uno o más lotes vía FEFO)
    "AJUSTE_POSITIVO",       # corrección manual al alza (crea lote)
    "AJUSTE_NEGATIVO",       # corrección manual a la baja
    "TRANSFERENCIA_ENTRADA", # llegó de otro almacén (crea lote nuevo)
    "TRANSFERENCIA_SALIDA",  # salió hacia otro almacén
    "DEVOLUCION_CLIENTE",    # cliente devolvió producto (crea lote nuevo)
    "DEVOLUCION_PROVEEDOR",  # se devolvió al proveedor
    "MERMA_VENCIMIENTO",     # NUEVO: baja por lote vencido
    "MERMA_OTROS",           # NUEVO: baja por rotura/robo/error
)

TIPOS_ENTRADA = {"ENTRADA_COMPRA", "AJUSTE_POSITIVO", "TRANSFERENCIA_ENTRADA", "DEVOLUCION_CLIENTE"}
TIPOS_SALIDA = {
    "SALIDA_VENTA", "AJUSTE_NEGATIVO", "TRANSFERENCIA_SALIDA",
    "DEVOLUCION_PROVEEDOR", "MERMA_VENCIMIENTO", "MERMA_OTROS",
}


class MovimientoStock(Base):
    __tablename__ = "movimientos_stock"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    variante_id: Mapped[str] = mapped_column(String(36), ForeignKey("variantes.id"), nullable=False)
    almacen_id: Mapped[str] = mapped_column(String(36), ForeignKey("almacenes.id"), nullable=False)

    # NUEVO: lote afectado por el movimiento.
    # Nullable para preservar movimientos legacy y casos de servicios.
    # Para movimientos nuevos sobre productos físicos, es obligatorio (validado en service).
    lote_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("lotes.id"))

    tipo: Mapped[str] = mapped_column(String(30), nullable=False)
    # Positivo si entra, negativo si sale.
    cantidad: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    costo_unitario: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))

    referencia_id: Mapped[str | None] = mapped_column(String(100))
    motivo: Mapped[str | None] = mapped_column(Text)
    usuario_id: Mapped[str | None] = mapped_column(String(36))

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    variante: Mapped["Variante"] = relationship(back_populates="movimientos")
    almacen: Mapped["Almacen"] = relationship(back_populates="movimientos")
    lote: Mapped["Lote | None"] = relationship(back_populates="movimientos")

    def __repr__(self) -> str:
        return f"<Movimiento {self.tipo} cant={self.cantidad} lote={self.lote_id}>"
