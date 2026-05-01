"""
Service layer — módulo inventario

Contiene la lógica de negocio para CRUD y operaciones de stock.
Todos los métodos filtran por negocio_id para multi-tenancy.

Cambios v2 (lotes):
- Lote es la fuente de verdad de cantidades. Stock es caché denormalizado.
- Toda entrada física crea o suma a un Lote, vía crear_lote() o registrar_entrada().
- Toda salida selecciona lote(s) por FEFO automáticamente, salvo override manual.
- _sincronizar_stock() es el único punto que actualiza Stock.cantidad_actual.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, func
from sqlalchemy.orm import Session, joinedload

from app.modules.inventario.models import (
    Categoria, Proveedor, Almacen, Producto, Variante,
    Stock, Lote, MovimientoStock,
    TIPOS_ENTRADA, TIPOS_SALIDA,
)
from app.modules.inventario.schemas import (
    CategoriaCreate, CategoriaUpdate,
    ProveedorCreate, ProveedorUpdate,
    AlmacenCreate, AlmacenUpdate,
    ProductoCreate, ProductoUpdate,
    VarianteCreate, VarianteUpdate,
    LoteCreate, LoteUpdate,
    MovimientoCreate, StockUpdate,
)


# ===========================================================================
# CATEGORÍAS, PROVEEDORES, ALMACENES — sin cambios respecto a v1
# (Se incluyen aquí para que el archivo sea drop-in replacement.)
# ===========================================================================

# --- Categorías ------------------------------------------------------------
def get_categorias(db: Session, negocio_id: str, solo_activas: bool = True) -> list[Categoria]:
    query = db.query(Categoria).filter(Categoria.negocio_id == negocio_id)
    if solo_activas:
        query = query.filter(Categoria.activa == True)
    return query.order_by(Categoria.nombre).all()


def get_categoria(db: Session, negocio_id: str, categoria_id: str) -> Categoria | None:
    return db.query(Categoria).filter(
        Categoria.id == categoria_id, Categoria.negocio_id == negocio_id
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


# --- Proveedores -----------------------------------------------------------
def get_proveedores(db: Session, negocio_id: str, solo_activos: bool = True) -> list[Proveedor]:
    query = db.query(Proveedor).filter(Proveedor.negocio_id == negocio_id)
    if solo_activos:
        query = query.filter(Proveedor.activo == True)
    return query.order_by(Proveedor.nombre).all()


def get_proveedor(db: Session, negocio_id: str, proveedor_id: str) -> Proveedor | None:
    return db.query(Proveedor).filter(
        Proveedor.id == proveedor_id, Proveedor.negocio_id == negocio_id
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


# --- Almacenes -------------------------------------------------------------
def get_almacenes(db: Session, negocio_id: str) -> list[Almacen]:
    return db.query(Almacen).filter(Almacen.negocio_id == negocio_id).order_by(
        Almacen.es_principal.desc(), Almacen.nombre
    ).all()


def get_almacen(db: Session, negocio_id: str, almacen_id: str) -> Almacen | None:
    return db.query(Almacen).filter(
        Almacen.id == almacen_id, Almacen.negocio_id == negocio_id
    ).first()


def get_almacen_principal(db: Session, negocio_id: str) -> Almacen | None:
    return db.query(Almacen).filter(
        Almacen.negocio_id == negocio_id, Almacen.es_principal == True
    ).first()


def create_almacen(db: Session, negocio_id: str, data: AlmacenCreate) -> Almacen:
    if data.es_principal:
        db.query(Almacen).filter(
            Almacen.negocio_id == negocio_id, Almacen.es_principal == True
        ).update({"es_principal": False})
    almacen = Almacen(negocio_id=negocio_id, **data.model_dump())
    db.add(almacen)
    db.commit()
    db.refresh(almacen)
    return almacen


def update_almacen(db: Session, almacen: Almacen, data: AlmacenUpdate) -> Almacen:
    if data.es_principal:
        db.query(Almacen).filter(
            Almacen.negocio_id == almacen.negocio_id,
            Almacen.es_principal == True,
            Almacen.id != almacen.id,
        ).update({"es_principal": False})
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(almacen, key, value)
    db.commit()
    db.refresh(almacen)
    return almacen


# ===========================================================================
# PRODUCTOS Y VARIANTES — sin cambios estructurales, sólo se preserva
# ===========================================================================
def get_productos(
    db: Session, negocio_id: str, solo_activos: bool = True,
    categoria_id: str | None = None, busqueda: str | None = None,
    page: int = 1, per_page: int = 50,
) -> tuple[list[Producto], int]:
    query = db.query(Producto).options(joinedload(Producto.variantes)).filter(
        Producto.negocio_id == negocio_id
    )
    if solo_activos:
        query = query.filter(Producto.activo == True)
    if categoria_id:
        query = query.filter(Producto.categoria_id == categoria_id)
    if busqueda:
        term = f"%{busqueda}%"
        query = query.filter(
            (Producto.nombre.ilike(term)) | (Producto.codigo_barras.ilike(term))
        )
    total = query.count()
    productos = query.order_by(Producto.nombre).offset((page - 1) * per_page).limit(per_page).all()
    return productos, total


def get_producto(db: Session, negocio_id: str, producto_id: str) -> Producto | None:
    return db.query(Producto).options(
        joinedload(Producto.variantes), joinedload(Producto.categoria), joinedload(Producto.proveedor)
    ).filter(
        Producto.id == producto_id, Producto.negocio_id == negocio_id
    ).first()


def get_producto_by_codigo(db: Session, negocio_id: str, codigo_barras: str) -> Producto | None:
    return db.query(Producto).filter(
        Producto.codigo_barras == codigo_barras, Producto.negocio_id == negocio_id
    ).first()


def create_producto(db: Session, negocio_id: str, data: ProductoCreate) -> Producto:
    producto_data = data.model_dump(exclude={"precio_venta", "precio_costo"})
    producto = Producto(negocio_id=negocio_id, **producto_data)
    db.add(producto)
    db.flush()
    if not data.tiene_variantes:
        variante = Variante(
            producto_id=producto.id,
            precio_venta=data.precio_venta or Decimal("0"),
            precio_costo=data.precio_costo,
            atributos={},
        )
        db.add(variante)
    db.commit()
    db.refresh(producto)
    return producto


def update_producto(db: Session, producto: Producto, data: ProductoUpdate) -> Producto:
    update_data = data.model_dump(exclude_unset=True)
    precio_venta = update_data.pop("precio_venta", None)
    precio_costo = update_data.pop("precio_costo", None)
    for key, value in update_data.items():
        setattr(producto, key, value)
    if (precio_venta is not None or precio_costo is not None) and not producto.tiene_variantes:
        if producto.variantes:
            v = producto.variantes[0]
            if precio_venta is not None:
                v.precio_venta = precio_venta
            if precio_costo is not None:
                v.precio_costo = precio_costo
    db.commit()
    db.refresh(producto)
    return producto




def delete_producto(db: Session, producto: Producto) -> None:
    """Soft delete: marca el producto como inactivo."""
    producto.activo = False
    db.commit()


def get_variante(db: Session, negocio_id: str, variante_id: str) -> Variante | None:
    return (
        db.query(Variante)
        .join(Producto, Variante.producto_id == Producto.id)
        .filter(Variante.id == variante_id, Producto.negocio_id == negocio_id)
        .first()
    )


def _validar_barcode_unico(db: Session, negocio_id: str, codigo: str | None,
                            variante_id: str | None = None) -> None:
    if not codigo:
        return
    q = (
        db.query(Variante).join(Producto)
        .filter(Producto.negocio_id == negocio_id, Variante.codigo_barras == codigo)
    )
    if variante_id:
        q = q.filter(Variante.id != variante_id)
    if q.first():
        raise ValueError(f"El código de barras '{codigo}' ya está asignado a otro producto")


def create_variante(db: Session, producto: Producto, data: VarianteCreate) -> Variante:
    _validar_barcode_unico(db, producto.negocio_id, data.codigo_barras)
    variante = Variante(producto_id=producto.id, **data.model_dump())
    db.add(variante)
    db.commit()
    db.refresh(variante)
    return variante


def update_variante(db: Session, variante: Variante, data: VarianteUpdate) -> Variante:
    _validar_barcode_unico(
        db, variante.producto.negocio_id, data.codigo_barras, variante.id,
    )
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(variante, field, value)
    db.commit()
    db.refresh(variante)
    return variante


# ===========================================================================
# LOTES — núcleo del nuevo modelo
# ===========================================================================
def _sincronizar_stock(db: Session, variante_id: str, almacen_id: str) -> Stock:
    """
    Recalcula Stock.cantidad_actual desde la suma de lotes activos.
    Llamar después de cualquier mutación a Lote.cantidad_actual.

    Devuelve la fila Stock (creada si no existía) ya actualizada.
    No hace commit — el caller controla la transacción.
    """
    db.flush()  # ← LÍNEA NUEVA. Asegura que cambios pendientes a Lote estén
                #    visibles en la query SUM siguiente, incluso con autoflush=False.
    suma = (
        db.query(func.coalesce(func.sum(Lote.cantidad_actual), 0))
        .filter(
            Lote.variante_id == variante_id,
            Lote.almacen_id == almacen_id,
            Lote.activo == True,
        )
        .scalar()
    )
    stock = (
        db.query(Stock)
        .filter(Stock.variante_id == variante_id, Stock.almacen_id == almacen_id)
        .first()
    )
    if not stock:
        stock = Stock(
            variante_id=variante_id, almacen_id=almacen_id,
            cantidad_actual=Decimal(suma),
        )
        db.add(stock)
    else:
        stock.cantidad_actual = Decimal(suma)
    db.flush()
    return stock


def _seleccionar_lotes_fefo(
    db: Session, variante_id: str, almacen_id: str, cantidad: Decimal,
) -> list[tuple[Lote, Decimal]]:
    """
    Devuelve lista [(lote, cantidad_a_descontar), ...] cubriendo `cantidad` total.

    Orden FEFO:
    1. fecha_vencimiento ASC (NULL al final → no vencen, se consumen últimos)
    2. fecha_ingreso ASC (FIFO como tiebreaker)

    Lanza ValueError si los lotes activos no alcanzan a cubrir la cantidad.
    """
    if cantidad <= 0:
        raise ValueError("La cantidad debe ser positiva")

    lotes = (
        db.query(Lote)
        .filter(
            Lote.variante_id == variante_id,
            Lote.almacen_id == almacen_id,
            Lote.activo == True,
            Lote.cantidad_actual > 0,
        )
        .order_by(
            # nullslast es Postgres-específico; en SQLite usamos un CASE.
            # SQLAlchemy traduce nullslast() correctamente en ambos dialectos
            # desde 1.4+, pero por seguridad usamos el patrón portable:
            Lote.fecha_vencimiento.is_(None).asc(),  # False(0) primero, True(1) al final
            Lote.fecha_vencimiento.asc(),
            Lote.fecha_ingreso.asc(),
        )
        .all()
    )

    asignacion: list[tuple[Lote, Decimal]] = []
    pendiente = Decimal(cantidad)
    for lote in lotes:
        if pendiente <= 0:
            break
        toma = min(pendiente, lote.cantidad_actual)
        asignacion.append((lote, toma))
        pendiente -= toma

    if pendiente > 0:
        disponible = sum(l.cantidad_actual for l in lotes)
        raise ValueError(
            f"Stock insuficiente. Solicitado: {cantidad}, Disponible: {disponible}"
        )
    return asignacion


def get_lote(db: Session, negocio_id: str, lote_id: str) -> Lote | None:
    """Obtiene un lote validando que pertenezca al negocio."""
    return (
        db.query(Lote)
        .join(Variante, Lote.variante_id == Variante.id)
        .join(Producto, Variante.producto_id == Producto.id)
        .filter(Lote.id == lote_id, Producto.negocio_id == negocio_id)
        .first()
    )


def get_lotes(
    db: Session, negocio_id: str,
    variante_id: str | None = None, almacen_id: str | None = None,
    solo_activos: bool = True, solo_con_stock: bool = False,
    vencen_antes_de: date | None = None,
) -> list[Lote]:
    """
    Listado de lotes con filtros. Útil para:
    - vista de lotes de un producto: variante_id
    - reporte de lotes de un almacén: almacen_id
    - reporte de próximos a vencer: vencen_antes_de
    """
    q = (
        db.query(Lote)
        .join(Variante, Lote.variante_id == Variante.id)
        .join(Producto, Variante.producto_id == Producto.id)
        .filter(Producto.negocio_id == negocio_id)
    )
    if variante_id:
        q = q.filter(Lote.variante_id == variante_id)
    if almacen_id:
        q = q.filter(Lote.almacen_id == almacen_id)
    if solo_activos:
        q = q.filter(Lote.activo == True)
    if solo_con_stock:
        q = q.filter(Lote.cantidad_actual > 0)
    if vencen_antes_de:
        q = q.filter(
            Lote.fecha_vencimiento.is_not(None),
            Lote.fecha_vencimiento <= vencen_antes_de,
        )
    return q.order_by(
        Lote.fecha_vencimiento.is_(None).asc(),
        Lote.fecha_vencimiento.asc(),
        Lote.fecha_ingreso.desc(),
    ).all()


def get_lotes_proximos_vencer(
    db: Session, negocio_id: str, dias: int = 30,
) -> list[Lote]:
    """Lotes con stock que vencen en los próximos N días (incluye ya vencidos)."""
    limite = date.today() + timedelta(days=dias)
    return get_lotes(
        db, negocio_id,
        solo_activos=True, solo_con_stock=True,
        vencen_antes_de=limite,
    )


def crear_lote(
    db: Session, negocio_id: str, data: LoteCreate,
    usuario_id: str | None = None,
    tipo_movimiento: str = "ENTRADA_COMPRA",
) -> Lote:
    """
    Crea un Lote y registra el MovimientoStock correspondiente.
    Sincroniza el stock agregado.

    `tipo_movimiento` permite reusar este flujo para AJUSTE_POSITIVO,
    TRANSFERENCIA_ENTRADA o DEVOLUCION_CLIENTE.
    """
    if tipo_movimiento not in TIPOS_ENTRADA:
        raise ValueError(f"tipo_movimiento debe ser de entrada, recibí: {tipo_movimiento}")

    variante = get_variante(db, negocio_id, data.variante_id)
    if not variante:
        raise ValueError("Variante no encontrada o no pertenece al negocio")

    almacen = get_almacen(db, negocio_id, data.almacen_id)
    if not almacen:
        raise ValueError("Almacén no encontrado o no pertenece al negocio")

    # Validación: si el producto controla vencimiento, fecha es obligatoria
    if variante.producto.controla_vencimiento and data.fecha_vencimiento is None:
        raise ValueError(
            f"El producto '{variante.producto.nombre}' controla vencimiento "
            f"y requiere fecha_vencimiento al crear el lote"
        )

    lote = Lote(
        variante_id=data.variante_id,
        almacen_id=data.almacen_id,
        codigo_lote=data.codigo_lote,
        fecha_vencimiento=data.fecha_vencimiento,
        cantidad_inicial=data.cantidad,
        cantidad_actual=data.cantidad,
        costo_unitario=data.costo_unitario,
        referencia_compra=data.referencia_compra,
        notas=data.notas,
    )
    db.add(lote)
    db.flush()  # obtener id

    movimiento = MovimientoStock(
        variante_id=data.variante_id,
        almacen_id=data.almacen_id,
        lote_id=lote.id,
        tipo=tipo_movimiento,
        cantidad=data.cantidad,
        costo_unitario=data.costo_unitario,
        referencia_id=data.referencia_compra,
        motivo=data.notas,
        usuario_id=usuario_id,
    )
    db.add(movimiento)

    _sincronizar_stock(db, data.variante_id, data.almacen_id)
    db.commit()
    db.refresh(lote)
    return lote


def update_lote(db: Session, lote: Lote, data: LoteUpdate) -> Lote:
    """
    Edita campos descriptivos del lote: codigo_lote, fecha_vencimiento, costo,
    notas. NO permite editar cantidad_actual (eso solo cambia vía movimientos).
    """
    update_data = data.model_dump(exclude_unset=True)
    # Defensa: bloquear edición de cantidades por aquí
    update_data.pop("cantidad_actual", None)
    update_data.pop("cantidad_inicial", None)
    for key, value in update_data.items():
        setattr(lote, key, value)
    db.commit()
    db.refresh(lote)
    return lote


def dar_baja_lote_vencido(
    db: Session, negocio_id: str, lote_id: str,
    motivo: str | None = None, usuario_id: str | None = None,
) -> MovimientoStock:
    """
    Da de baja todo el stock remanente de un lote (típicamente vencido).
    Crea un MovimientoStock tipo MERMA_VENCIMIENTO y desactiva el lote.
    """
    lote = get_lote(db, negocio_id, lote_id)
    if not lote:
        raise ValueError("Lote no encontrado")
    if lote.cantidad_actual <= 0:
        raise ValueError("El lote no tiene stock para dar de baja")

    cantidad_baja = lote.cantidad_actual

    movimiento = MovimientoStock(
        variante_id=lote.variante_id,
        almacen_id=lote.almacen_id,
        lote_id=lote.id,
        tipo="MERMA_VENCIMIENTO",
        cantidad=-cantidad_baja,
        costo_unitario=lote.costo_unitario,
        motivo=motivo or f"Baja por vencimiento (lote {lote.codigo_lote or lote.id[:8]})",
        usuario_id=usuario_id,
    )
    db.add(movimiento)
    lote.cantidad_actual = Decimal("0")
    lote.activo = False

    _sincronizar_stock(db, lote.variante_id, lote.almacen_id)
    db.commit()
    db.refresh(movimiento)
    return movimiento


# ===========================================================================
# STOCK — caché derivado, queries de consulta
# ===========================================================================
def get_stock_by_variante(
    db: Session, variante_id: str, almacen_id: str | None = None,
) -> list[Stock]:
    query = db.query(Stock).filter(Stock.variante_id == variante_id)
    if almacen_id:
        query = query.filter(Stock.almacen_id == almacen_id)
    return query.all()


def get_or_create_stock(db: Session, variante_id: str, almacen_id: str) -> Stock:
    stock = (
        db.query(Stock)
        .filter(Stock.variante_id == variante_id, Stock.almacen_id == almacen_id)
        .first()
    )
    if not stock:
        stock = Stock(
            variante_id=variante_id, almacen_id=almacen_id,
            cantidad_actual=Decimal("0"),
        )
        db.add(stock)
        db.flush()
    return stock


def get_alertas_stock(db: Session, negocio_id: str) -> list[dict]:
    """Productos con stock por debajo del mínimo configurado."""
    results = db.query(Stock, Variante, Producto, Almacen).join(
        Variante, Stock.variante_id == Variante.id
    ).join(
        Producto, Variante.producto_id == Producto.id
    ).join(
        Almacen, Stock.almacen_id == Almacen.id
    ).filter(
        Producto.negocio_id == negocio_id,
        Stock.cantidad_actual <= Stock.cantidad_minima,
        Producto.activo == True,
        Producto.es_servicio == False,
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
    """Actualiza solo cantidad_minima / cantidad_maxima (configuración de alertas)."""
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(stock, key, value)
    db.commit()
    db.refresh(stock)
    return stock


# ===========================================================================
# MOVIMIENTOS — entrada y salida con FEFO
# ===========================================================================
def crear_movimiento(
    db: Session, negocio_id: str, data: MovimientoCreate,
    usuario_id: str | None = None,
) -> list[MovimientoStock]:
    """
    Crea uno o más movimientos según el tipo:

    ENTRADAS (TIPOS_ENTRADA):
      - Si data.lote_id está presente, suma a ese lote existente.
      - Si no, exige los datos para crear un Lote nuevo (cantidad, costo,
        fecha_vencimiento opcional). Para esto preferí llamar a crear_lote().

    SALIDAS (TIPOS_SALIDA):
      - Si data.lote_id está presente: override manual, descuenta sólo de ese lote.
      - Si no: FEFO automático. Puede generar N movimientos (uno por lote consumido).

    Devuelve siempre una lista de movimientos (1 o más).
    """
    variante = get_variante(db, negocio_id, data.variante_id)
    if not variante:
        raise ValueError("Variante no encontrada o no pertenece al negocio")

    almacen = get_almacen(db, negocio_id, data.almacen_id)
    if not almacen:
        raise ValueError("Almacén no encontrado o no pertenece al negocio")

    # Servicios: no tocan stock
    if variante.producto.es_servicio:
        raise ValueError("Los servicios no generan movimientos de stock")

    if data.tipo in TIPOS_ENTRADA:
        return _crear_movimiento_entrada(db, data, usuario_id)
    elif data.tipo in TIPOS_SALIDA:
        return _crear_movimientos_salida(db, data, usuario_id)
    else:
        raise ValueError(f"Tipo de movimiento inválido: {data.tipo}")


def _crear_movimiento_entrada(
    db: Session, data: MovimientoCreate, usuario_id: str | None,
) -> list[MovimientoStock]:
    if not data.lote_id:
        raise ValueError(
            "Las entradas requieren lote_id. "
            "Para crear un lote nuevo usa crear_lote() en su lugar."
        )

    lote = db.get(Lote, data.lote_id)
    if not lote or lote.variante_id != data.variante_id or lote.almacen_id != data.almacen_id:
        raise ValueError("Lote no coincide con variante/almacen del movimiento")

    movimiento = MovimientoStock(
        variante_id=data.variante_id,
        almacen_id=data.almacen_id,
        lote_id=lote.id,
        tipo=data.tipo,
        cantidad=data.cantidad,
        costo_unitario=data.costo_unitario,
        referencia_id=data.referencia_id,
        motivo=data.motivo,
        usuario_id=usuario_id,
    )
    db.add(movimiento)
    lote.cantidad_actual += data.cantidad

    _sincronizar_stock(db, data.variante_id, data.almacen_id)
    db.commit()
    db.refresh(movimiento)
    return [movimiento]


def _crear_movimientos_salida(
    db: Session, data: MovimientoCreate, usuario_id: str | None,
) -> list[MovimientoStock]:
    # Determinar asignación: override manual o FEFO automático
    if data.lote_id:
        lote = db.get(Lote, data.lote_id)
        if not lote or lote.variante_id != data.variante_id or lote.almacen_id != data.almacen_id:
            raise ValueError("Lote no coincide con variante/almacen del movimiento")
        if lote.cantidad_actual < data.cantidad:
            raise ValueError(
                f"Stock insuficiente en lote. "
                f"Disponible: {lote.cantidad_actual}, Solicitado: {data.cantidad}"
            )
        asignacion = [(lote, data.cantidad)]
    else:
        asignacion = _seleccionar_lotes_fefo(
            db, data.variante_id, data.almacen_id, data.cantidad,
        )

    movimientos: list[MovimientoStock] = []
    for lote, cantidad in asignacion:
        mov = MovimientoStock(
            variante_id=data.variante_id,
            almacen_id=data.almacen_id,
            lote_id=lote.id,
            tipo=data.tipo,
            cantidad=-cantidad,  # negativo = salida
            costo_unitario=lote.costo_unitario,  # costo del lote vendido
            referencia_id=data.referencia_id,
            motivo=data.motivo,
            usuario_id=usuario_id,
        )
        db.add(mov)
        lote.cantidad_actual -= cantidad
        movimientos.append(mov)

    _sincronizar_stock(db, data.variante_id, data.almacen_id)
    db.commit()
    for mov in movimientos:
        db.refresh(mov)
    return movimientos


def get_movimientos(
    db: Session, negocio_id: str,
    variante_id: str | None = None, almacen_id: str | None = None,
    tipo: str | None = None, lote_id: str | None = None,
    page: int = 1, per_page: int = 50,
) -> tuple[list[MovimientoStock], int]:
    query = (
        db.query(MovimientoStock)
        .join(Variante, MovimientoStock.variante_id == Variante.id)
        .join(Producto, Variante.producto_id == Producto.id)
        .filter(Producto.negocio_id == negocio_id)
    )
    if variante_id:
        query = query.filter(MovimientoStock.variante_id == variante_id)
    if almacen_id:
        query = query.filter(MovimientoStock.almacen_id == almacen_id)
    if tipo:
        query = query.filter(MovimientoStock.tipo == tipo)
    if lote_id:
        query = query.filter(MovimientoStock.lote_id == lote_id)
    total = query.count()
    movimientos = query.order_by(
        MovimientoStock.created_at.desc()
    ).offset((page - 1) * per_page).limit(per_page).all()
    return movimientos, total


def get_variante_por_barcode(
    db: Session, negocio_id: str, codigo: str,
) -> Variante | None:
    return (
        db.query(Variante)
        .join(Producto, Variante.producto_id == Producto.id)
        .options(joinedload(Variante.producto))
        .filter(
            Producto.negocio_id == negocio_id,
            Variante.codigo_barras == codigo,
            Variante.activa == True,
        )
        .first()
    )


# ===========================================================================
# Búsqueda por atributo (aprovecha el índice GIN de JSONB en Postgres)
# ===========================================================================
def buscar_variantes_por_atributo(
    db: Session, negocio_id: str, atributos: dict,
) -> list[Variante]:
    """
    Busca variantes que matcheen TODOS los atributos pasados.
    Ejemplo: buscar_variantes_por_atributo(db, neg, {"talla": "M", "color": "rojo"})

    En Postgres usa el operador @> contra el índice GIN, query muy rápido.
    En SQLite hace fallback a comparación python-side (sólo para tests).
    """
    q = (
        db.query(Variante)
        .join(Producto, Variante.producto_id == Producto.id)
        .filter(Producto.negocio_id == negocio_id, Variante.activa == True)
    )

    if db.bind.dialect.name == "postgresql":
        # @> es contains: matchea si atributos del registro contienen los pedidos
        q = q.filter(Variante.atributos.contains(atributos))
        return q.all()

    # Fallback SQLite (tests): traer todo y filtrar en Python
    candidatos = q.all()
    return [
        v for v in candidatos
        if v.atributos and all(v.atributos.get(k) == val for k, val in atributos.items())
    ]
