# models/inventario.py  (fragmento de los modelos clave)

class Producto(Base):
    __tablename__ = "productos"
    id           = Column(UUID, primary_key=True, default=uuid4)
    negocio_id   = Column(UUID, ForeignKey("negocios.id"), nullable=False)
    categoria_id = Column(UUID, ForeignKey("categorias.id"))
    nombre       = Column(String(200), nullable=False)
    tiene_variantes = Column(Boolean, default=False)
    es_servicio  = Column(Boolean, default=False)
    activo       = Column(Boolean, default=True)
    created_at   = Column(DateTime, default=datetime.utcnow)
    variantes    = relationship("Variante", back_populates="producto")

class Variante(Base):
    __tablename__ = "variantes"
    id           = Column(UUID, primary_key=True, default=uuid4)
    producto_id  = Column(UUID, ForeignKey("productos.id"), nullable=False)
    sku          = Column(String(100), unique=True)
    atributos    = Column(JSON)          # {"talla":"M","color":"rojo"}
    precio_venta = Column(Numeric(12,2), nullable=False)
    precio_costo = Column(Numeric(12,2))
    foto_url     = Column(String(500))
    stocks       = relationship("Stock", back_populates="variante")

class Stock(Base):
    __tablename__ = "stock"
    __table_args__ = (UniqueConstraint("variante_id","almacen_id"),)
    id               = Column(UUID, primary_key=True, default=uuid4)
    variante_id      = Column(UUID, ForeignKey("variantes.id"), nullable=False)
    almacen_id       = Column(UUID, ForeignKey("almacenes.id"), nullable=False)
    cantidad_actual  = Column(Numeric(12,3), default=0)
    cantidad_minima  = Column(Numeric(12,3), default=0)   # alerta stock bajo

class MovimientoStock(Base):
    __tablename__ = "movimientos_stock"
    id              = Column(UUID, primary_key=True, default=uuid4)
    variante_id     = Column(UUID, ForeignKey("variantes.id"), nullable=False)
    almacen_id      = Column(UUID, ForeignKey("almacenes.id"), nullable=False)
    tipo            = Column(String(30), nullable=False)  # ENTRADA_COMPRA, etc.
    cantidad        = Column(Numeric(12,3), nullable=False)
    costo_unitario  = Column(Numeric(12,2))
    referencia_id   = Column(String(100))  # ID de venta, factura, etc.
    motivo          = Column(Text)
    usuario_id      = Column(UUID, ForeignKey("usuarios.id"))
    created_at      = Column(DateTime, default=datetime.utcnow)