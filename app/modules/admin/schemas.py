"""Schemas Pydantic para el módulo de superadmin."""
from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, Field


class NegocioCreate(BaseModel):
    """Payload para crear un negocio con su primer admin en una sola operación."""
    # Datos del negocio
    nombre: str = Field(..., min_length=2, max_length=200)
    propietario: str = Field(..., min_length=2, max_length=200)
    telefono: str | None = Field(None, max_length=30)
    moneda: str = Field("BOB", max_length=10)

    # Datos del primer admin del negocio (obligatorio)
    admin_email: EmailStr
    admin_nombre: str = Field(..., min_length=2, max_length=200)
    admin_password: str = Field(..., min_length=6, max_length=100)


class NegocioUpdate(BaseModel):
    """Actualización de datos del negocio. Todos los campos opcionales."""
    nombre: str | None = Field(None, min_length=2, max_length=200)
    propietario: str | None = Field(None, min_length=2, max_length=200)
    telefono: str | None = None
    moneda: str | None = None
    activo: bool | None = None


class NegocioResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    nombre: str
    propietario: str
    telefono: str | None
    moneda: str
    activo: bool
    created_at: datetime


class NegocioConAdminResponse(BaseModel):
    """Respuesta al crear un negocio: incluye el admin recién creado."""
    model_config = ConfigDict(from_attributes=True)

    negocio: NegocioResponse
    admin_id: str
    admin_email: str
    admin_nombre: str


class NegocioListItem(BaseModel):
    """Item para el listado de negocios con conteos."""
    model_config = ConfigDict(from_attributes=True)

    id: str
    nombre: str
    propietario: str
    telefono: str | None
    moneda: str
    activo: bool
    created_at: datetime
    # Estadísticas agregadas
    num_usuarios: int = 0
    num_productos: int = 0
