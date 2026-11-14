"""fase2_5_idempotencia

Revision ID: f3a4b5c6d7e8
Revises: f2a3b4c5d6e7
Create Date: 2026-11-12 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a4b5c6d7e8'
down_revision: Union[str, None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'idempotencia_claves',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('negocio_id', sa.String(36), sa.ForeignKey('negocios.id', ondelete='CASCADE'), nullable=False),
        sa.Column('metodo', sa.String(10), server_default='POST', nullable=False),
        sa.Column('endpoint', sa.String(255), nullable=False),
        sa.Column('idempotency_key', sa.String(128), nullable=False),
        sa.Column('usuario_id', sa.String(36), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('status', sa.String(20), server_default='SUCCESS', nullable=False),
        sa.Column('response_code', sa.Integer(), nullable=True),
        sa.Column('response_json', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['negocio_id', 'usuario_id'], ['users.negocio_id', 'users.id'], name='fk_idempotencia_negocio_usuario', ondelete='CASCADE'),
        sa.UniqueConstraint('negocio_id', 'metodo', 'endpoint', 'idempotency_key', name='uq_idempotencia_negocio_metodo_endpoint_key'),
    )
    op.create_index('ix_idempotencia_lookup', 'idempotencia_claves', ['negocio_id', 'metodo', 'endpoint', 'idempotency_key'])
    op.create_index('ix_idempotencia_expires_at', 'idempotencia_claves', ['expires_at'])


def downgrade() -> None:
    op.drop_index('ix_idempotencia_expires_at', table_name='idempotencia_claves')
    op.drop_index('ix_idempotencia_lookup', table_name='idempotencia_claves')
    op.drop_table('idempotencia_claves')
