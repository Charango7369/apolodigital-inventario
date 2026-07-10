"""
Router FastAPI — módulo ventas

Endpoints para clientes, ventas (POS) y reportes.
"""

from datetime import date, datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, get_current_admin
from app.modules.auth.models import User

from app.modules.ventas import service
from app.modules.ventas.schemas import (
    # Cliente
    ClienteCreate, ClienteUpdate, ClienteResponse,
    # Venta
    VentaCreate, VentaUpdate, VentaCompletarRequest, VentaResponse, VentaListResponse,
    # Reportes
    ResumenVentasDia, ResumenCaja,
)

router = APIRouter()


# ===========================================================================
# CLIENTES
# ===========================================================================
@router.get("/clientes", response_model=list[ClienteResponse])
def listar_clientes(
    solo_activos: bool = True,
    busqueda: str | None = Query(None, min_length=1),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Lista clientes del negocio"""
    return service.get_clientes(db, user.negocio_id, solo_activos, busqueda)


@router.post("/clientes", response_model=ClienteResponse, status_code=201)
def crear_cliente(
    data: ClienteCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Crea un nuevo cliente"""
    return service.create_cliente(db, user.negocio_id, data)


@router.get("/clientes/{cliente_id}", response_model=ClienteResponse)
def obtener_cliente(
    cliente_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Obtiene un cliente por ID"""
    cliente = service.get_cliente(db, user.negocio_id, cliente_id)
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    return cliente


@router.get("/clientes/telefono/{telefono}", response_model=ClienteResponse)
def buscar_cliente_por_telefono(
    telefono: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Busca cliente por teléfono (útil para POS)"""
    cliente = service.get_cliente_by_telefono(db, user.negocio_id, telefono)
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    return cliente


@router.put("/clientes/{cliente_id}", response_model=ClienteResponse)
def actualizar_cliente(
    cliente_id: str,
    data: ClienteUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Actualiza un cliente"""
    cliente = service.get_cliente(db, user.negocio_id, cliente_id)
    if not cliente:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    return service.update_cliente(db, cliente, data)


# ===========================================================================
# VENTAS
# ===========================================================================
@router.get("/ventas")
def listar_ventas(
    estado: str | None = None,
    fecha_desde: date | None = None,
    fecha_hasta: date | None = None,
    cliente_id: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Lista ventas con filtros"""
    ventas, total = service.get_ventas(
        db, user.negocio_id, estado, fecha_desde, fecha_hasta, cliente_id, page, per_page
    )
    
    items = []
    for v in ventas:
        items.append({
            "id": v.id,
            "numero": v.numero,
            "cliente_nombre": v.cliente_nombre or (v.cliente.nombre if v.cliente else None),
            "total": v.total,
            "metodo_pago": v.metodo_pago,
            "estado": v.estado,
            "created_at": v.created_at,
            "items_count": len(v.detalles)
        })
    
    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page
    }


@router.post("/ventas", response_model=VentaResponse, status_code=201)
def crear_venta(
    data: VentaCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Crea una nueva venta.
    
    - Si `completar=True` (default), la venta se completa y descuenta stock.
    - Si `completar=False`, queda como PENDIENTE para completarla después.
    """
    try:
        return service.crear_venta(db, user.negocio_id, data, user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/ventas/{venta_id}", response_model=VentaResponse)
def obtener_venta(
    venta_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Obtiene una venta con todos sus detalles"""
    venta = service.get_venta(db, user.negocio_id, venta_id)
    if not venta:
        raise HTTPException(status_code=404, detail="Venta no encontrada")
    return venta


@router.get("/ventas/numero/{numero}", response_model=VentaResponse)
def obtener_venta_por_numero(
    numero: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Busca venta por número de ticket"""
    venta = service.get_venta_by_numero(db, user.negocio_id, numero)
    if not venta:
        raise HTTPException(status_code=404, detail="Venta no encontrada")
    return venta


@router.post("/ventas/{venta_id}/completar", response_model=VentaResponse)
def completar_venta(
    venta_id: str,
    data: VentaCompletarRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Completa una venta pendiente (descuenta stock)"""
    venta = service.get_venta(db, user.negocio_id, venta_id)
    if not venta:
        raise HTTPException(status_code=404, detail="Venta no encontrada")
    try:
        return service.completar_venta(db, user.negocio_id, venta, data, user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/ventas/{venta_id}/cancelar", response_model=VentaResponse)
def cancelar_venta(
    venta_id: str,
    motivo: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Cancela una venta.
    Si estaba completada, devuelve el stock automáticamente.
    """
    venta = service.get_venta(db, user.negocio_id, venta_id)
    if not venta:
        raise HTTPException(status_code=404, detail="Venta no encontrada")
    try:
        return service.cancelar_venta(db, user.negocio_id, venta, user.id, motivo)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ===========================================================================
# REPORTES
# ===========================================================================
@router.get("/reportes/ventas-dia", response_model=ResumenVentasDia)
def reporte_ventas_dia(
    fecha: date = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Resumen de ventas de un día (default: hoy)"""
    if fecha is None:
        fecha = date.today()
    return service.get_resumen_ventas_dia(db, user.negocio_id, fecha)


@router.get("/reportes/caja", response_model=ResumenCaja)
def reporte_caja(
    fecha: date = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Resumen de caja del día (default: hoy)"""
    if fecha is None:
        fecha = date.today()
    return service.get_resumen_caja(db, user.negocio_id, fecha)

