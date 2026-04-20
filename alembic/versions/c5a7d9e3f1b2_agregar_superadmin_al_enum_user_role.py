"""agregar superadmin al enum user_role

Revision ID: c5a7d9e3f1b2
Revises: b4f2c1d9e8a1
Create Date: 2026-04-20 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c5a7d9e3f1b2'
down_revision = 'b4f2c1d9e8a1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """
    Agrega el valor 'superadmin' al enum user_role en PostgreSQL.

    Importante: en PostgreSQL los valores de enum se agregan con ALTER TYPE.
    No se pueden modificar dentro de una transacción explícita, por eso
    usamos op.execute con autocommit_block.
    """
    # La columna 'rol' en tabla 'users' es VARCHAR(20), no ENUM de PostgreSQL.
    # Por eso no necesitamos ALTER TYPE — solo documentamos el nuevo valor.
    # La validación del valor ocurre a nivel aplicación (Pydantic).
    #
    # Si en un futuro migras a un ENUM real de PostgreSQL, aquí iría:
    # op.execute("ALTER TYPE user_role ADD VALUE 'superadmin'")

    # No-op a nivel DB. El valor 'superadmin' es válido inmediatamente
    # porque el constraint es solo longitud de string.
    pass


def downgrade() -> None:
    # No-op. Para revertir habría que eliminar usuarios con rol superadmin,
    # lo que no es automatizable con seguridad.
    pass
