"""
Router — módulo auth

Endpoints:
  POST /auth/login     → autenticar y obtener JWT
  POST /auth/register  → crear nuevo usuario (solo admin)
  GET  /auth/me        → datos del usuario actual
  PUT  /auth/password  → cambiar contraseña propia
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_user, get_current_admin
from app.modules.auth.models import User
from app.modules.auth.schemas import (
    MessageResponse,
    PasswordChange,
    TokenResponse,
    UserCreate,
    UserResponse,
)
from app.modules.auth.service import (
    authenticate_user,
    create_access_token,
    create_user,
    get_user_by_email,
    update_user_password,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------
@router.post("/login", response_model=TokenResponse)
def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    """
    Autenticar usuario y obtener JWT.
    
    Usa OAuth2PasswordRequestForm para compatibilidad con Swagger UI.
    - username: email del usuario
    - password: contraseña
    """
    user = authenticate_user(db, form_data.username, form_data.password)
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email o contraseña incorrectos",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    # Crear token con datos del usuario
    token_data = {
        "sub": user.id,
        "email": user.email,
        "negocio_id": user.negocio_id,
        "rol": user.rol,
    }
    access_token = create_access_token(token_data)
    
    return TokenResponse(
        access_token=access_token,
        user=UserResponse.model_validate(user),
    )


# ---------------------------------------------------------------------------
# POST /auth/register (solo admin puede crear usuarios)
# ---------------------------------------------------------------------------
@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(
    user_data: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin),
):
    """
    Crear nuevo usuario.
    
    Solo un ADMIN puede registrar usuarios, y solo en SU negocio.
    """
    # Forzar que el nuevo usuario sea del mismo negocio que el admin
    if user_data.negocio_id != current_user.negocio_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo puedes crear usuarios en tu propio negocio",
        )
    
    # Verificar que el email no exista en este negocio
    existing = get_user_by_email(db, user_data.email, user_data.negocio_id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ya existe un usuario con ese email en este negocio",
        )
    
    user = create_user(db, user_data)
    return UserResponse.model_validate(user)


# ---------------------------------------------------------------------------
# GET /auth/me
# ---------------------------------------------------------------------------
@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    """Obtener datos del usuario autenticado."""
    return UserResponse.model_validate(current_user)


# ---------------------------------------------------------------------------
# PUT /auth/password
# ---------------------------------------------------------------------------
@router.put("/password", response_model=MessageResponse)
def change_password(
    data: PasswordChange,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cambiar contraseña del usuario actual."""
    # Verificar contraseña actual
    if not verify_password(data.current_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Contraseña actual incorrecta",
        )
    
    update_user_password(db, current_user, data.new_password)
    return MessageResponse(message="Contraseña actualizada correctamente")
