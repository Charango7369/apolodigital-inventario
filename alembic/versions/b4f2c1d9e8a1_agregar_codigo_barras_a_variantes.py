"""agregar codigo_barras a variantes

Revision ID: b4f2c1d9e8a1
Revises: a3b4c5d6e7f8
Create Date: 2026-04-18 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers
revision = 'b4f2c1d9e8a1'
down_revision = 'a3b4c5d6e7f8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Agrega la columna codigo_barras a la tabla variantes.

    - Nullable: hay variantes que no tienen código (artesanías, granel).
    - Sin UNIQUE a nivel DB: la unicidad se valida por negocio en el service,
      porque el mismo EAN puede aparecer en múltiples negocios.
    - Se crea un índice para acelerar búsquedas por código de barras,
      que es el caso de uso principal en el POS.
    """
    op.add_column(
        'variantes',
        sa.Column('codigo_barras', sa.String(length=50), nullable=True),
    )
    op.create_index(
        'ix_variantes_codigo_barras',
        'variantes',
        ['codigo_barras'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_variantes_codigo_barras', table_name='variantes')
    op.drop_column('variantes', 'codigo_barras')
