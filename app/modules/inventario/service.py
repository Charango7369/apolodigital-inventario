"""
Service layer — módulo inventario

Contiene la lógica de negocio para CRUD y operaciones de stock.
Todos los métodos filtran por negocio_id para multi-tenancy.
"""

from decimal import Decimal
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.modules.inventario.models import (
    Categoria, Proveedor, Almacen, Producto, Variante, Stock, MovimientoStock
)
from app.modules.inventario.schemas import (
    CategoriaCreate, CategoriaUpdate,
    ProveedorCreate, ProveedorUpdate,
    AlmacenCreate, AlmacenUpdate,
    ProductoCreate, ProductoUpdate,
    VarianteCreate, VarianteUpdate,
    MovimientoCreate, StockUpdate,
)


# ---------------------------------------------------------------------------
# Categorías
# ---------------------------------------------------------------------------
def get_categorias(db: Session, negocio_id: str, solo_activas: bool = True) -> list[Categoria]:
    query = db.query(Categoria).filter(Categoria.negocio_id == negocio_id)
    if solo_activas:
        query = query.filter(Categoria.activa == True)
    return query.order_by(Categoria.nombre).all()


def get_categoria(db: Session, negocio_id: str, categoria_id: str) -> Categoria | None:
    return db.query(Categoria).filter(
        Categoria.id == categoria_id,
        Categoria.negocio_id == negocio_id
    ).first()


def create_categoria(db: Session, negocio_id: str, data: CategoriaCreate) -> Categoria:
    categoria = Categoria(negocio_id=negocio_id, **data.model_dump())
    db.add(categoria)
    db.commit()
    db.refresh(categoria)
    return categoria


def update_categoria(db: Session, categoria: Categoria, data: CategoriaUpdate) -> Categoria:
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(categoria, key, value)
    db.commit()
    db.refresh(categoria)
    return categoria


def delete_categoria(db: Session, categoria: Categoria) -> None:
    db.delete(categoria)
    db.commit()


# ---------------------------------------------------------------------------
# Proveedores
# ---------------------------------------------------------------------------
def get_proveedores(db: Session, negocio_id: str, solo_activos: bool = True) -> list[Proveedor]:
    query = db.query(Proveedor).filter(Proveedor.negocio_id == negocio_id)
    if solo_activos:
        query = query.filter(Proveedor.activo == True)
    return query.order_by(Proveedor.nombre).all()


def get_proveedor(db: Session, negocio_id: str, proveedor_id: str) -> Proveedor | None:
    return db.query(Proveedor).filter(
        Proveedor.id == proveedor_id,
        Proveedor.negocio_id == negocio_id
    ).first()


def create_proveedor(db: Session, negocio_id: str, data: ProveedorCreate) -> Proveedor:
    proveedor = Proveedor(negocio_id=negocio_id, **data.model_dump())
    db.add(proveedor)
    db.commit()
    db.refresh(proveedor)
    return proveedor


def update_proveedor(db: Session, proveedor: Proveedor, data: ProveedorUpdate) -> Proveedor:
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(proveedor, key, value)
    db.commit()
    db.refresh(proveedor)
    return proveedor


def delete_proveedor(db: Session, proveedor: Proveedor) -> None:
    db.delete(proveedor)
    db.commit()


# ---------------------------------------------------------------------------
# Almacenes
# ---------------------------------------------------------------------------
def get_almacenes(db: Session, negocio_id: str) -> list[Almacen]:
    return db.query(Almacen).filter(
        Almacen.negocio_id == negocio_id
    ).order_by(Almacen.es_principal.desc(), Almacen.nombre).all()


def get_almacen(db: Session, negocio_id: str, almacen_id: str) -> Almacen | None:
    return db.query(Almacen).filter(
        Almacen.id == almacen_id,
        Almacen.negocio_id == negocio_id
    ).first()


def get_almacen_principal(db: Session, negocio_id: str) -> Almacen | None:
    return db.query(Almacen).filter(
        Almacen.negocio_id == negocio_id,
        Almacen.es_principal == True
    ).first()


def create_almacen(db: Session, negocio_id: str, data: AlmacenCreate) -> Almacen:
    # Si es principal, quitar flag de otros
    if data.es_principal:
        db.query(Almacen).filter(
            Almacen.negocio_id == negocio_id,
            Almacen.es_principal == True
        ).update({"es_principal": False})
    
    almacen = Almacen(negocio_id=negocio_id, **data.model_dump())
    db.add(almacen)
    db.commit()
    db.refresh(almacen)
    return almacen


def update_almacen(db: Session, almacen: Almacen, data: AlmacenUpdate) -> Almacen:
    # Si se marca como principal, quitar flag de otros
    if data.es_principal:
        db.query(Almacen).filter(
            Almacen.negocio_id == almacen.negocio_id,
            Almacen.es_principal == True,
            Almacen.id != almacen.id
        ).update({"es_principal": False})
    
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(almacen, key, value)
    db.commit()
    db.refresh(almacen)
    return almacen


# ---------------------------------------------------------------------------
# Productos
# ---------------------------------------------------------------------------
def get_productos(
    db: Session,
    negocio_id: str,
    solo_activos: bool = True,
    categoria_id: str | None = None,
    busqueda: str | None = None,
    page: int = 1,
    per_page: int = 50
) -> tuple[list[Producto], int]:
    query = db.query(Producto).filter(Producto.negocio_id == negocio_id)
    
    if solo_activos:
        query = query.filter(Producto.activo == True)
    if categoria_id:
        query = query.filter(Producto.categoria_id == categoria_id)
    if busqueda:
        search_term = f"%{busqueda}%"
        query = query.filter(
            (Producto.nombre.ilike(search_term)) |
            (Producto.codigo_barras.ilike(search_term))
        )
    
    total = query.count()
    productos = query.options(
        joinedload(Producto.variantes)
    ).order_by(Producto.nombre).offset((page - 1) * per_page).limit(per_page).all()
    
    return productos, total


def get_producto(db: Session, negocio_id: str, producto_id: str) -> Producto | None:
    return db.query(Producto).options(
        joinedload(Producto.variantes),
        joinedload(Producto.categoria),
        joinedload(Producto.proveedor)
    ).filter(
        Producto.id == producto_id,
        Producto.negocio_id == negocio_id
    ).first()


def get_producto_by_codigo(db: Session, negocio_id: str, codigo_barras: str) -> Producto | None:
    return db.query(Producto).filter(
        Producto.codigo_barras == codigo_barras,
        Producto.negocio_id == negocio_id
    ).first()


def create_producto(db: Session, negocio_id: str, data: ProductoCreate) -> Producto:
    producto_data = data.model_dump(exclude={"precio_venta", "precio_costo"})
    producto = Producto(negocio_id=negocio_id, **producto_data)
    db.add(producto)
    db.flush()  # Obtener ID del producto
    
    # Crear variante por defecto si no tiene variantes
    if not data.tiene_variantes:
        variante = Variante(
            producto_id=producto.id,
            precio_venta=data.precio_venta or Decimal("0"),
            precio_costo=data.precio_costo,
            atributos={}
        )
        db.add(variante)
    
    db.commit()
    db.refresh(producto)
    return producto


def update_producto(db: Session, producto: Producto, data: ProductoUpdate) -> Producto:
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(producto, key, value)
    db.commit()
    db.refresh(producto)
    return producto


def delete_producto(db: Session, producto: Producto) -> None:
    # Soft delete
    producto.activo = False
    db.commit()


# ---------------------------------------------------------------------------
# Variantes
# ---------------------------------------------------------------------------
def get_variante(db: Session, negocio_id: str, variante_id: str) -> Variante | None:
    return db.query(Variante).join(Producto).filter(
        Variante.id == variante_id,
        Producto.negocio_id == negocio_id
    ).first()


def create_variante(db: Session, producto: Producto, data: VarianteCreate) -> Variante:
    variante = Variante(producto_id=producto.id, **data.model_dump())
    db.add(variante)
    db.commit()
    db.refresh(variante)
    return variante


def update_variante(db: Session, variante: Variante, data: VarianteUpdate) -> Variante:
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(variante, key, value)
    db.commit()
    db.refresh(variante)
    return variante


# ---------------------------------------------------------------------------
# Stock
# ---------------------------------------------------------------------------
def get_stock_by_variante(db: Session, variante_id: str, almacen_id: str | None = None) -> list[Stock]:
    query = db.query(Stock).filter(Stock.variante_id == variante_id)
    if almacen_id:
        query = query.filter(Stock.almacen_id == almacen_id)
    return query.all()


def get_or_create_stock(db: Session, variante_id: str, almacen_id: str) -> Stock:
    stock = db.query(Stock).filter(
        Stock.variante_id == variante_id,
        Stock.almacen_id == almacen_id
    ).first()
    
    if not stock:
        stock = Stock(variante_id=variante_id, almacen_id=almacen_id, cantidad_actual=Decimal("0"))
        db.add(stock)
        db.flush()
    
    return stock


def get_alertas_stock(db: Session, negocio_id: str) -> list[dict]:
    """Retorna productos con stock bajo el mínimo"""
    results = db.query(
        Stock,
        Variante,
        Producto,
        Almacen
    ).join(
        Variante, Stock.variante_id == Variante.id
    ).join(
        Producto, Variante.producto_id == Producto.id
    ).join(
        Almacen, Stock.almacen_id == Almacen.id
    ).filter(
        Producto.negocio_id == negocio_id,
        Stock.cantidad_actual <= Stock.cantidad_minima,
        Producto.activo == True,
        Producto.es_servicio == False
    ).all()
    
    return [
        {
            "id": stock.id,
            "variante_id": stock.variante_id,
            "almacen_id": stock.almacen_id,
            "cantidad_actual": stock.cantidad_actual,
            "cantidad_minima": stock.cantidad_minima,
            "cantidad_maxima": stock.cantidad_maxima,
            "producto_nombre": producto.nombre,
            "variante_sku": variante.sku,
            "almacen_nombre": almacen.nombre,
        }
        for stock, variante, producto, almacen in results
    ]


def update_stock_config(db: Session, stock: Stock, data: StockUpdate) -> Stock:
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(stock, key, value)
    db.commit()
    db.refresh(stock)
    return stock


# ---------------------------------------------------------------------------
# Movimientos de Stock
# ---------------------------------------------------------------------------
TIPOS_ENTRADA = {"ENTRADA_COMPRA", "AJUSTE_POSITIVO", "TRANSFERENCIA_ENTRADA", "DEVOLUCION_CLIENTE"}
TIPOS_SALIDA = {"SALIDA_VENTA", "AJUSTE_NEGATIVO", "TRANSFERENCIA_SALIDA", "DEVOLUCION_PROVEEDOR"}


def crear_movimiento(
    db: Session,
    negocio_id: str,
    data: MovimientoCreate,
    usuario_id: str | None = None
) -> MovimientoStock:
    """
    Crea un movimiento y actualiza el stock correspondiente.
    Valida que la variante y almacén pertenezcan al negocio.
    """
    # Validar variante pertenece al negocio
    variante = get_variante(db, negocio_id, data.variante_id)
    if not variante:
        raise ValueError("Variante no encontrada o no pertenece al negocio")
    
    # Validar almacén pertenece al negocio
    almacen = get_almacen(db, negocio_id, data.almacen_id)
    if not almacen:
        raise ValueError("Almacén no encontrado o no pertenece al negocio")
    
    # Obtener o crear registro de stock
    stock = get_or_create_stock(db, data.variante_id, data.almacen_id)
    
    # Calcular nueva cantidad
    if data.tipo in TIPOS_ENTRADA:
        nueva_cantidad = stock.cantidad_actual + data.cantidad
    elif data.tipo in TIPOS_SALIDA:
        nueva_cantidad = stock.cantidad_actual - data.cantidad
        if nueva_cantidad < 0:
            raise ValueError(f"Stock insuficiente. Disponible: {stock.cantidad_actual}")
    else:
        raise ValueError(f"Tipo de movimiento inválido: {data.tipo}")
    
    # Crear movimiento
    movimiento = MovimientoStock(
        variante_id=data.variante_id,
        almacen_id=data.almacen_id,
        tipo=data.tipo,
        cantidad=data.cantidad if data.tipo in TIPOS_ENTRADA else -data.cantidad,
        costo_unitario=data.costo_unitario,
        referencia_id=data.referencia_id,
        motivo=data.motivo,
        usuario_id=usuario_id
    )
    db.add(movimiento)
    
    # Actualizar stock
    stock.cantidad_actual = nueva_cantidad
    
    db.commit()
    db.refresh(movimiento)
    return movimiento


def get_movimientos(
    db: Session,
    negocio_id: str,
    variante_id: str | None = None,
    almacen_id: str | None = None,
    tipo: str | None = None,
    page: int = 1,
    per_page: int = 50
) -> tuple[list[MovimientoStock], int]:
    query = db.query(MovimientoStock).join(
        Variante, MovimientoStock.variante_id == Variante.id
    ).join(
        Producto, Variante.producto_id == Producto.id
    ).filter(Producto.negocio_id == negocio_id)
    
    if variante_id:
        query = query.filter(MovimientoStock.variante_id == variante_id)
    if almacen_id:
        query = query.filter(MovimientoStock.almacen_id == almacen_id)
    if tipo:
        query = query.filter(MovimientoStock.tipo == tipo)
    
    total = query.count()
    movimientos = query.order_by(
        MovimientoStock.created_at.desc()
    ).offset((page - 1) * per_page).limit(per_page).all()
    
    return movimientos, total
