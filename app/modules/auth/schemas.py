"""
Schemas Pydantic — módulo auth
"""

from datetime import datetime
from pydantic import BaseModel, EmailStr, Field, ConfigDict


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)
    nombre: str = Field(..., min_length=1, max_length=200)


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserUpdate(BaseModel):
    nombre: str | None = Field(None, min_length=1, max_length=200)
    email: EmailStr | None = None


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8)


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    email: str
    nombre: str
    negocio_id: str
    rol: str
    activo: bool
    created_at: datetime
    updated_at: datetime
    ultimo_login: datetime | None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


class MessageResponse(BaseModel):
    message: str
