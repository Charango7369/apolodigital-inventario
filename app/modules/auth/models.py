"""
Modelos SQLAlchemy — módulo auth

Tabla:
  users → usuarios del sistema, vinculados a un negocio (multi-tenant)
"""

import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index,
    String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def new_uuid() -> str:
    return str(uuid.uuid4())


class UserRole(str, Enum):
    """Roles disponibles en el sistema."""
    SUPERADMIN = "superadmin"  # dueño de la plataforma ApoloDigital
    ADMIN = "admin"
    EMPLEADO = "empleado"


class User(Base):
    """
    Usuario del sistema.
    
    Reglas:
    - Cada usuario pertenece a UN negocio (multi-tenant)
    - Email único POR negocio, no global
    - Admin: control total de su negocio
    - Empleado: solo ventas y lectura de inventario
    """
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", "negocio_id", name="uq_user_email_negocio"),
        Index("ix_users_email", "email"),
        Index("ix_users_negocio_id", "negocio_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    nombre: Mapped[str] = mapped_column(String(200), nullable=False)
    
    negocio_id: Mapped[str] = mapped_column(
        String(36), 
        ForeignKey("negocios.id"), 
        nullable=False
    )
    
    rol: Mapped[str] = mapped_column(String(20), default=UserRole.EMPLEADO.value)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, 
        default=datetime.utcnow, 
        onupdate=datetime.utcnow
    )
    ultimo_login: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Relación con Negocio
    negocio: Mapped["Negocio"] = relationship("Negocio", backref="usuarios")

    def __repr__(self) -> str:
        return f"<User {self.email} ({self.rol})>"
    
    @property
    def is_admin(self) -> bool:
        return self.rol == UserRole.ADMIN.value
