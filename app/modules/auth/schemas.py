"""
Schemas Pydantic — módulo auth

Validación de entrada/salida para endpoints de autenticación.
"""

from datetime import datetime
from pydantic import BaseModel, EmailStr, Field

from app.modules.auth.models import UserRole


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------
class UserCreate(BaseModel):
    """Registro de nuevo usuario."""
    email: EmailStr
    password: str = Field(..., min_length=8, description="Mínimo 8 caracteres")
    nombre: str = Field(..., min_length=2, max_length=200)
    negocio_id: str = Field(..., description="UUID del negocio al que pertenece")
    rol: UserRole = Field(default=UserRole.EMPLEADO)


class UserLogin(BaseModel):
    """Login con email y contraseña."""
    email: EmailStr
    password: str


class UserUpdate(BaseModel):
    """Actualización parcial de usuario."""
    nombre: str | None = Field(None, min_length=2, max_length=200)
    rol: UserRole | None = None
    activo: bool | None = None


class PasswordChange(BaseModel):
    """Cambio de contraseña."""
    current_password: str
    new_password: str = Field(..., min_length=8)


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------
class UserResponse(BaseModel):
    """Usuario sin datos sensibles."""
    id: str
    email: str
    nombre: str
    negocio_id: str
    rol: str
    activo: bool
    created_at: datetime
    updated_at: datetime
    ultimo_login: datetime | None = None

    model_config = {"from_attributes": True}


class TokenResponse(BaseModel):
    """Respuesta del login exitoso."""
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class MessageResponse(BaseModel):
    """Respuesta simple con mensaje."""
    message: str
    success: bool = True
