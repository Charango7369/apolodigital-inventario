"""crear tablas ventas

Revision ID: a3b4c5d6e7f8
Revises: 827a45561e1c
Create Date: 2026-04-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, None] = '827a45561e1c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Tabla clientes
    op.create_table(
        'clientes',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('negocio_id', sa.String(36), sa.ForeignKey('negocios.id'), nullable=False),
        sa.Column('nombre', sa.String(200), nullable=False),
        sa.Column('telefono', sa.String(30), nullable=True),
        sa.Column('email', sa.String(200), nullable=True),
        sa.Column('nit', sa.String(20), nullable=True),
        sa.Column('direccion', sa.Text, nullable=True),
        sa.Column('notas', sa.Text, nullable=True),
        sa.Column('activo', sa.Boolean, default=True),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index('ix_clientes_negocio_telefono', 'clientes', ['negocio_id', 'telefono'])

    # Tabla ventas
    op.create_table(
        'ventas',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('negocio_id', sa.String(36), sa.ForeignKey('negocios.id'), nullable=False),
        sa.Column('almacen_id', sa.String(36), sa.ForeignKey('almacenes.id'), nullable=False),
        sa.Column('numero', sa.Integer, nullable=False),
        sa.Column('cliente_id', sa.String(36), sa.ForeignKey('clientes.id'), nullable=True),
        sa.Column('cliente_nombre', sa.String(200), nullable=True),
        sa.Column('subtotal', sa.Numeric(12, 2), default=0),
        sa.Column('descuento', sa.Numeric(12, 2), default=0),
        sa.Column('total', sa.Numeric(12, 2), default=0),
        sa.Column('metodo_pago', sa.String(20), default='EFECTIVO'),
        sa.Column('monto_recibido', sa.Numeric(12, 2), nullable=True),
        sa.Column('cambio', sa.Numeric(12, 2), nullable=True),
        sa.Column('estado', sa.String(20), default='PENDIENTE'),
        sa.Column('usuario_id', sa.String(36), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('notas', sa.Text, nullable=True),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.now()),
        sa.Column('completed_at', sa.DateTime, nullable=True),
        sa.Column('cancelled_at', sa.DateTime, nullable=True),
    )
    op.create_index('ix_ventas_negocio_numero', 'ventas', ['negocio_id', 'numero'], unique=True)
    op.create_index('ix_ventas_negocio_fecha', 'ventas', ['negocio_id', 'created_at'])
    op.create_index('ix_ventas_estado', 'ventas', ['negocio_id', 'estado'])

    # Tabla detalle_ventas
    op.create_table(
        'detalle_ventas',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('venta_id', sa.String(36), sa.ForeignKey('ventas.id'), nullable=False),
        sa.Column('variante_id', sa.String(36), sa.ForeignKey('variantes.id'), nullable=False),
        sa.Column('producto_nombre', sa.String(200), nullable=False),
        sa.Column('variante_sku', sa.String(100), nullable=True),
        sa.Column('cantidad', sa.Numeric(12, 3), nullable=False),
        sa.Column('precio_unitario', sa.Numeric(12, 2), nullable=False),
        sa.Column('descuento_linea', sa.Numeric(12, 2), default=0),
        sa.Column('subtotal', sa.Numeric(12, 2), nullable=False),
        sa.Column('costo_unitario', sa.Numeric(12, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('detalle_ventas')
    op.drop_index('ix_ventas_estado', 'ventas')
    op.drop_index('ix_ventas_negocio_fecha', 'ventas')
    op.drop_index('ix_ventas_negocio_numero', 'ventas')
    op.drop_table('ventas')
    op.drop_index('ix_clientes_negocio_telefono', 'clientes')
    op.drop_table('clientes')
