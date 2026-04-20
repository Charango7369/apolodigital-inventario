"""
Router FastAPI — módulo inventario

Endpoints para gestión de productos, categorías, proveedores,
almacenes, stock y movimientos.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, get_current_admin
from app.modules.auth.models import User

from app.modules.inventario import service
from app.modules.inventario.schemas import (
    # Categoría
    CategoriaCreate,
    CategoriaUpdate,
    CategoriaResponse,
    # Proveedor
    ProveedorCreate,
    ProveedorUpdate,
    ProveedorResponse,
    # Almacén
    AlmacenCreate,
    AlmacenUpdate,
    AlmacenResponse,
    # Producto
    ProductoCreate,
    ProductoUpdate,
    ProductoResponse,
    ProductoListResponse,
    # Variante
    VarianteCreate,
    VarianteUpdate,
    VarianteResponse,
    # Stock
    StockResponse,
    StockConDetalleResponse,
    StockUpdate,
    # Movimiento
    MovimientoCreate,
    MovimientoResponse,
)

router = APIRouter()


# ===========================================================================
# CATEGORÍAS
# ===========================================================================
@router.get("/categorias", response_model=list[CategoriaResponse])
def listar_categorias(
    solo_activas: bool = True,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lista todas las categorías del negocio"""
    return service.get_categorias(db, user.negocio_id, solo_activas)


@router.post("/categorias", response_model=CategoriaResponse, status_code=201)
def crear_categoria(
    data: CategoriaCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Crea una nueva categoría"""
    return service.create_categoria(db, user.negocio_id, data)


@router.get("/categorias/{categoria_id}", response_model=CategoriaResponse)
def obtener_categoria(
    categoria_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Obtiene una categoría por ID"""
    categoria = service.get_categoria(db, user.negocio_id, categoria_id)
    if not categoria:
        raise HTTPException(status_code=404, detail="Categoría no encontrada")
    return categoria


@router.put("/categorias/{categoria_id}", response_model=CategoriaResponse)
def actualizar_categoria(
    categoria_id: str,
    data: CategoriaUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Actualiza una categoría"""
    categoria = service.get_categoria(db, user.negocio_id, categoria_id)
    if not categoria:
        raise HTTPException(status_code=404, detail="Categoría no encontrada")
    return service.update_categoria(db, categoria, data)


@router.delete("/categorias/{categoria_id}", status_code=204)
def eliminar_categoria(
    categoria_id: str,
    user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Elimina una categoría (solo admin)"""
    categoria = service.get_categoria(db, user.negocio_id, categoria_id)
    if not categoria:
        raise HTTPException(status_code=404, detail="Categoría no encontrada")
    service.delete_categoria(db, categoria)


# ===========================================================================
# PROVEEDORES
# ===========================================================================
@router.get("/proveedores", response_model=list[ProveedorResponse])
def listar_proveedores(
    solo_activos: bool = True,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lista todos los proveedores del negocio"""
    return service.get_proveedores(db, user.negocio_id, solo_activos)


@router.post("/proveedores", response_model=ProveedorResponse, status_code=201)
def crear_proveedor(
    data: ProveedorCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Crea un nuevo proveedor"""
    return service.create_proveedor(db, user.negocio_id, data)


@router.get("/proveedores/{proveedor_id}", response_model=ProveedorResponse)
def obtener_proveedor(
    proveedor_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Obtiene un proveedor por ID"""
    proveedor = service.get_proveedor(db, user.negocio_id, proveedor_id)
    if not proveedor:
        raise HTTPException(status_code=404, detail="Proveedor no encontrado")
    return proveedor


@router.put("/proveedores/{proveedor_id}", response_model=ProveedorResponse)
def actualizar_proveedor(
    proveedor_id: str,
    data: ProveedorUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Actualiza un proveedor"""
    proveedor = service.get_proveedor(db, user.negocio_id, proveedor_id)
    if not proveedor:
        raise HTTPException(status_code=404, detail="Proveedor no encontrado")
    return service.update_proveedor(db, proveedor, data)


@router.delete("/proveedores/{proveedor_id}", status_code=204)
def eliminar_proveedor(
    proveedor_id: str,
    user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Elimina un proveedor (solo admin)"""
    proveedor = service.get_proveedor(db, user.negocio_id, proveedor_id)
    if not proveedor:
        raise HTTPException(status_code=404, detail="Proveedor no encontrado")
    service.delete_proveedor(db, proveedor)


# ===========================================================================
# ALMACENES
# ===========================================================================
@router.get("/almacenes", response_model=list[AlmacenResponse])
def listar_almacenes(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Lista todos los almacenes del negocio"""
    return service.get_almacenes(db, user.negocio_id)


@router.post("/almacenes", response_model=AlmacenResponse, status_code=201)
def crear_almacen(
    data: AlmacenCreate,
    user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Crea un nuevo almacén (solo admin)"""
    return service.create_almacen(db, user.negocio_id, data)


@router.get("/almacenes/{almacen_id}", response_model=AlmacenResponse)
def obtener_almacen(
    almacen_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Obtiene un almacén por ID"""
    almacen = service.get_almacen(db, user.negocio_id, almacen_id)
    if not almacen:
        raise HTTPException(status_code=404, detail="Almacén no encontrado")
    return almacen


@router.put("/almacenes/{almacen_id}", response_model=AlmacenResponse)
def actualizar_almacen(
    almacen_id: str,
    data: AlmacenUpdate,
    user: User = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Actualiza un almacén (solo admin)"""
    almacen = service.get_almacen(db, user.negocio_id, almacen_id)
    if not almacen:
        raise HTTPException(status_code=404, detail="Almacén no encontrado")
    return service.update_almacen(db, almacen, data)


# ===========================================================================
# PRODUCTOS
# ===========================================================================
@router.get("/productos")
def listar_productos(
    solo_activos: bool = True,
    categoria_id: str | None = None,
    busqueda: str | None = Query(
        None, min_length=1, description="Buscar por nombre o código"
    ),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lista productos con paginación y filtros"""
    productos, total = service.get_productos(
        db, user.negocio_id, solo_activos, categoria_id, busqueda, page, per_page
    )

    # Convertir a respuesta con precio
    items = []
    for p in productos:
        precio = None
        if p.variantes:
            activas = [v for v in p.variantes if v.activa]
            if activas:
                precio = activas[0].precio_venta
            items.append(
                {
                    "id": p.id,
                    "nombre": p.nombre,
                    "categoria_id": p.categoria_id,
                    "codigo_barras": p.codigo_barras,
                    "unidad_medida": p.unidad_medida,
                    "tiene_variantes": p.tiene_variantes,
                    "es_servicio": p.es_servicio,
                    "activo": p.activo,
                    "precio_venta": precio,
                    "variantes": [
                        {
                            "id": v.id,
                            "sku": v.sku,
                            "codigo_barras": v.codigo_barras,
                            "precio_venta": v.precio_venta,
                            "precio_costo": v.precio_costo,
                            "activa": v.activa,
                        }
                        for v in p.variantes
                        if v.activa
                    ],
                }
            )

    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    }


@router.post("/productos", response_model=ProductoResponse, status_code=201)
def crear_producto(
    data: ProductoCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Crea un nuevo producto con su variante por defecto"""
    # Validar que tenga precio si no tiene variantes
    if not data.tiene_variantes and data.precio_venta is None:
        raise HTTPException(
            status_code=400,
            detail="Debe especificar precio_venta para productos sin variantes",
        )
    return service.create_producto(db, user.negocio_id, data)


@router.get("/productos/{producto_id}", response_model=ProductoResponse)
def obtener_producto(
    producto_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Obtiene un producto con sus variantes"""
    producto = service.get_producto(db, user.negocio_id, producto_id)
    if not producto:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    return producto


@router.get("/productos/codigo/{codigo_barras}", response_model=ProductoResponse)
def buscar_por_codigo(
    codigo_barras: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Busca un producto por código de barras"""
    producto = service.get_producto_by_codigo(db, user.negocio_id, codigo_barras)
    if not producto:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    return producto


@router.put("/productos/{producto_id}", response_model=ProductoResponse)
def actualizar_producto(
    producto_id: str,
    data: ProductoUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Actualiza un producto"""
    producto = service.get_producto(db, user.negocio_id, producto_id)
    if not producto:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    return service.update_producto(db, producto, data)


@router.delete("/productos/{producto_id}", status_code=204)
def eliminar_producto(
    producto_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Desactiva un producto (soft delete)"""
    producto = service.get_producto(db, user.negocio_id, producto_id)
    if not producto:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    service.delete_producto(db, producto)


# ===========================================================================
# VARIANTES
# ===========================================================================
@router.post(
    "/productos/{producto_id}/variantes",
    response_model=VarianteResponse,
    status_code=201,
)
def crear_variante(
    producto_id: str,
    data: VarianteCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Crea una variante para un producto"""
    producto = service.get_producto(db, user.negocio_id, producto_id)
    if not producto:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    if not producto.tiene_variantes:
        raise HTTPException(
            status_code=400,
            detail="El producto no admite variantes. Edite el producto para habilitar variantes.",
        )

    try:
        return service.create_variante(db, producto, data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))



@router.put("/variantes/{variante_id}", response_model=VarianteResponse)
def actualizar_variante(
    variante_id: str,
    data: VarianteUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Actualiza una variante"""
    variante = service.get_variante(db, user.negocio_id, variante_id)
    if not variante:
        raise HTTPException(status_code=404, detail="Variante no encontrada")
    return service.update_variante(db, variante, data)

@router.get("/variantes/barcode/{codigo}")
def buscar_variante_por_barcode(
    codigo: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Busca una variante por su código de barras.

    Usado desde el POS para agregar al carrito escaneando.
    Devuelve la variante con su producto anidado.

    Retorna 404 si no se encuentra en el negocio del usuario.
    """
    variante = service.get_variante_por_barcode(db, user.negocio_id, codigo)
    if not variante:
        raise HTTPException(
            status_code=404,
            detail=f"Código '{codigo}' no registrado",
        )
    return {
        "id": variante.id,
        "producto_id": variante.producto_id,
        "producto_nombre": variante.producto.nombre,
        "sku": variante.sku,
        "codigo_barras": variante.codigo_barras,
        "precio_venta": variante.precio_venta,
        "atributos": variante.atributos,
        "activa": variante.activa,
    }


# ===========================================================================
# STOCK
# ===========================================================================
@router.get("/stock/alertas", response_model=list[StockConDetalleResponse])
def obtener_alertas_stock(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Lista productos con stock bajo el mínimo"""
    return service.get_alertas_stock(db, user.negocio_id)


@router.get("/stock/variante/{variante_id}", response_model=list[StockResponse])
def obtener_stock_variante(
    variante_id: str,
    almacen_id: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Obtiene el stock de una variante en todos los almacenes o uno específico"""
    # Validar que la variante pertenece al negocio
    variante = service.get_variante(db, user.negocio_id, variante_id)
    if not variante:
        raise HTTPException(status_code=404, detail="Variante no encontrada")
    return service.get_stock_by_variante(db, variante_id, almacen_id)


@router.put("/stock/{stock_id}", response_model=StockResponse)
def configurar_stock(
    stock_id: str,
    data: StockUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Configura mínimos y máximos de stock"""
    # TODO: Validar que el stock pertenece al negocio
    stock = db.query(service.Stock).filter(service.Stock.id == stock_id).first()
    if not stock:
        raise HTTPException(status_code=404, detail="Stock no encontrado")
    return service.update_stock_config(db, stock, data)


# ===========================================================================
# MOVIMIENTOS
# ===========================================================================
@router.post("/movimientos", response_model=MovimientoResponse, status_code=201)
def registrar_movimiento(
    data: MovimientoCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Registra un movimiento de stock.

    Tipos de entrada (aumentan stock):
    - ENTRADA_COMPRA: mercancía del proveedor
    - AJUSTE_POSITIVO: corrección manual al alza
    - TRANSFERENCIA_ENTRADA: desde otro almacén
    - DEVOLUCION_CLIENTE: cliente devolvió producto

    Tipos de salida (reducen stock):
    - SALIDA_VENTA: se vendió
    - AJUSTE_NEGATIVO: corrección manual a la baja
    - TRANSFERENCIA_SALIDA: hacia otro almacén
    - DEVOLUCION_PROVEEDOR: se devolvió al proveedor
    """
    try:
        return service.crear_movimiento(db, user.negocio_id, data, user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/movimientos")
def listar_movimientos(
    variante_id: str | None = None,
    almacen_id: str | None = None,
    tipo: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lista movimientos de stock con filtros"""
    movimientos, total = service.get_movimientos(
        db, user.negocio_id, variante_id, almacen_id, tipo, page, per_page
    )
    return {
        "items": [MovimientoResponse.model_validate(m) for m in movimientos],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    }
