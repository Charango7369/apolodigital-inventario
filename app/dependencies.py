"""
Dependencies — compartidas en toda la app

Principales:
  get_current_user  → extrae usuario del JWT, valida que exista y esté activo
  get_current_admin → igual pero solo permite admins
  get_negocio_id    → shortcut para obtener negocio_id del usuario actual
"""

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.database import get_db
from app.modules.auth.models import User, UserRole
from app.modules.auth.service import decode_access_token, get_user_by_id

# OAuth2 scheme — indica que el token viene en header Authorization: Bearer <token>
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    """
    Extrae y valida el usuario actual del JWT.
    
    Levanta 401 si:
    - Token inválido o expirado
    - Usuario no existe
    - Usuario inactivo
    
    El token debe contener:
    - sub: user_id
    - negocio_id: para multi-tenant
    - rol: para permisos
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token inválido o expirado",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    # Decodificar token
    payload = decode_access_token(token)
    if payload is None:
        raise credentials_exception
    
    user_id: str = payload.get("sub")
    if user_id is None:
        raise credentials_exception
    
    # Buscar usuario en DB
    user = get_user_by_id(db, user_id)
    if user is None:
        raise credentials_exception
    
    # Verificar que esté activo
    if not user.activo:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Usuario desactivado",
        )
    
    return user


def get_current_admin(current_user: User = Depends(get_current_user)) -> User:
    """
    Permite ADMIN y SUPERADMIN.
    
    Levanta 403 si el usuario no es admin.
    SUPERADMIN hereda todos los privilegios de admin.
    """

    if current_user.rol not in (UserRole.ADMIN.value, UserRole.SUPERADMIN.value):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere rol de administrador",
        )
    return current_user

def get_current_superadmin(current_user: User = Depends(get_current_user)) -> User:
    """
    Solo permite SUPERADMIN.

    Levanta 403 si el usuario no es superadmin de la plataforma.
    Usado por el panel de gestión de negocios (multi-tenant).
    """
    if current_user.rol != UserRole.SUPERADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere rol de superadministrador de plataforma",
        )
    return current_user

def get_negocio_id(current_user: User = Depends(get_current_user)) -> str:
    """
    Shortcut para obtener el negocio_id del usuario actual.
    
    Útil cuando solo necesitas filtrar por negocio sin todo el objeto User.
    
    Uso:
        @router.get("/productos")
        def listar(negocio_id: str = Depends(get_negocio_id), db = Depends(get_db)):
            return db.query(Producto).filter_by(negocio_id=negocio_id).all()
    """
    return current_user.negocio_id
