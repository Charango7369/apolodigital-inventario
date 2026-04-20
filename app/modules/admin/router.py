"""Router del panel SUPERADMIN: gestión de negocios."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_superadmin
from app.modules.auth.models import User

from . import service
from .schemas import (
    NegocioCreate,
    NegocioUpdate,
    NegocioResponse,
    NegocioConAdminResponse,
    NegocioListItem,
)

router = APIRouter(prefix="/admin", tags=["admin"])


# ===========================================================================
# NEGOCIOS
# ===========================================================================
@router.get("/negocios", response_model=list[NegocioListItem])
def listar_negocios(
    solo_activos: bool = False,
    superadmin: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    """Lista todos los negocios registrados en la plataforma."""
    return service.list_negocios(db, solo_activos)


@router.post(
    "/negocios",
    response_model=NegocioConAdminResponse,
    status_code=status.HTTP_201_CREATED,
)
def crear_negocio(
    data: NegocioCreate,
    superadmin: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    """
    Crea un nuevo negocio con su primer administrador.
    Ambos se crean en una sola transacción atómica.
    """
    try:
        resultado = service.create_negocio_con_admin(db, data)
        return resultado
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/negocios/{negocio_id}", response_model=NegocioResponse)
def obtener_negocio(
    negocio_id: str,
    superadmin: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    """Obtiene un negocio por ID."""
    negocio = service.get_negocio(db, negocio_id)
    if not negocio:
        raise HTTPException(status_code=404, detail="Negocio no encontrado")
    return negocio


@router.put("/negocios/{negocio_id}", response_model=NegocioResponse)
def actualizar_negocio(
    negocio_id: str,
    data: NegocioUpdate,
    superadmin: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    """Actualiza datos del negocio (incluyendo suspensión con activo=False)."""
    negocio = service.get_negocio(db, negocio_id)
    if not negocio:
        raise HTTPException(status_code=404, detail="Negocio no encontrado")
    return service.update_negocio(db, negocio, data)


@router.post("/negocios/{negocio_id}/toggle", response_model=NegocioResponse)
def suspender_reactivar_negocio(
    negocio_id: str,
    superadmin: User = Depends(get_current_superadmin),
    db: Session = Depends(get_db),
):
    """Suspende o reactiva un negocio. Toggle rápido sin enviar body."""
    negocio = service.get_negocio(db, negocio_id)
    if not negocio:
        raise HTTPException(status_code=404, detail="Negocio no encontrado")
    return service.toggle_negocio_activo(db, negocio)
