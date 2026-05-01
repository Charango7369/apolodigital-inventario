"""lotes y atributos jsonb

Revision ID: d6e7f8a9b0c1
Revises: c5a7d9e3f1b2
Create Date: 2026-04-30 12:00:00.000000

Cambios:
1. Variante.atributos: JSON → JSONB (Postgres) + índice GIN
2. Producto: nueva columna controla_vencimiento (default False)
3. Tabla nueva `lotes`
4. MovimientoStock: nueva columna lote_id (nullable)
5. Migración de datos: por cada Stock con cantidad > 0, crear un Lote inicial
   sin fecha de vencimiento, copiando el costo de Variante.precio_costo.

Estrategia de seguridad:
- Operaciones idempotentes donde es posible.
- Migración de datos en bloque con SQL crudo (más rápido que ORM).
- En SQLite (tests) los pasos específicos de Postgres se omiten silenciosamente.

Rollback:
- downgrade() revierte: drop tabla lotes, drop columna lote_id, drop columna
  controla_vencimiento, vuelve atributos a JSON. Los datos de stock NO se tocan
  en downgrade — siguen estando en la tabla `stock`.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = "d6e7f8a9b0c1"
down_revision = "c5a7d9e3f1b2"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # -----------------------------------------------------------------------
    # 1. Producto: agregar controla_vencimiento
    # -----------------------------------------------------------------------
    with op.batch_alter_table("productos") as batch:
        batch.add_column(
            sa.Column(
                "controla_vencimiento",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )

    # -----------------------------------------------------------------------
    # 2. Variante.atributos: JSON → JSONB (solo Postgres)
    # -----------------------------------------------------------------------
    if _is_postgres():
        op.execute(
            "ALTER TABLE variantes "
            "ALTER COLUMN atributos TYPE JSONB "
            "USING atributos::jsonb"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_variantes_atributos_gin "
            "ON variantes USING GIN (atributos)"
        )

    # -----------------------------------------------------------------------
    # 3. Crear tabla lotes
    # -----------------------------------------------------------------------
    op.create_table(
        "lotes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("variante_id", sa.String(36), sa.ForeignKey("variantes.id"), nullable=False),
        sa.Column("almacen_id", sa.String(36), sa.ForeignKey("almacenes.id"), nullable=False),
        sa.Column("codigo_lote", sa.String(50), nullable=True),
        sa.Column("fecha_vencimiento", sa.Date(), nullable=True),
        sa.Column("fecha_ingreso", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("cantidad_inicial", sa.Numeric(12, 3), nullable=False),
        sa.Column("cantidad_actual", sa.Numeric(12, 3), nullable=False),
        sa.Column("costo_unitario", sa.Numeric(12, 2), nullable=True),
        sa.Column("referencia_compra", sa.String(100), nullable=True),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("notas", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_lotes_fefo", "lotes",
        ["variante_id", "almacen_id", "activo", "fecha_vencimiento"],
    )
    op.create_index("ix_lotes_vencimiento", "lotes", ["fecha_vencimiento"])

    # -----------------------------------------------------------------------
    # 4. MovimientoStock: agregar lote_id nullable
    # -----------------------------------------------------------------------
    with op.batch_alter_table("movimientos_stock") as batch:
        batch.add_column(
            sa.Column("lote_id", sa.String(36), sa.ForeignKey("lotes.id"), nullable=True)
        )

    # -----------------------------------------------------------------------
    # 5. Migración de datos: convertir stock actual en lotes iniciales
    # -----------------------------------------------------------------------
    # Por cada (variante_id, almacen_id) con cantidad > 0 en stock, crear un
    # Lote con esa cantidad. Sin fecha de vencimiento. Costo = precio_costo
    # de la variante (puede ser NULL).
    #
    # uuid_generate_v4() en Postgres requiere extension pgcrypto o uuid-ossp.
    # Para no asumir extensión, usamos gen_random_uuid() (pgcrypto en PG13+,
    # built-in en PG14+) con fallback a md5(random()::text || clock_timestamp()::text)
    # en su defecto. En SQLite usamos hex(randomblob(16)) que da 32 hex chars.
    if _is_postgres():
        # Asegurar extensión pgcrypto para gen_random_uuid()
        op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        op.execute(
            """
            INSERT INTO lotes (
                id, variante_id, almacen_id,
                codigo_lote, fecha_vencimiento, fecha_ingreso,
                cantidad_inicial, cantidad_actual,
                costo_unitario, referencia_compra, activo, notas
            )
            SELECT
                gen_random_uuid()::text,
                s.variante_id,
                s.almacen_id,
                'INICIAL',
                NULL,
                NOW(),
                s.cantidad_actual,
                s.cantidad_actual,
                v.precio_costo,
                'migracion-d6e7f8a9b0c1',
                TRUE,
                'Lote inicial creado por migracion. Stock previo a soporte de lotes.'
            FROM stock s
            JOIN variantes v ON v.id = s.variante_id
            WHERE s.cantidad_actual > 0
            """
        )
    else:
        # SQLite: usar hex(randomblob(16)) para id y reemplazar NOW por datetime('now')
        op.execute(
            """
            INSERT INTO lotes (
                id, variante_id, almacen_id,
                codigo_lote, fecha_vencimiento, fecha_ingreso,
                cantidad_inicial, cantidad_actual,
                costo_unitario, referencia_compra, activo, notas
            )
            SELECT
                lower(hex(randomblob(4))) || '-' ||
                lower(hex(randomblob(2))) || '-4' ||
                substr(lower(hex(randomblob(2))), 2) || '-' ||
                substr('89ab', 1 + (abs(random()) % 4), 1) ||
                substr(lower(hex(randomblob(2))), 2) || '-' ||
                lower(hex(randomblob(6))),
                s.variante_id,
                s.almacen_id,
                'INICIAL',
                NULL,
                datetime('now'),
                s.cantidad_actual,
                s.cantidad_actual,
                v.precio_costo,
                'migracion-d6e7f8a9b0c1',
                1,
                'Lote inicial creado por migracion. Stock previo a soporte de lotes.'
            FROM stock s
            JOIN variantes v ON v.id = s.variante_id
            WHERE s.cantidad_actual > 0
            """
        )


def downgrade() -> None:
    # 4. Drop columna lote_id en movimientos
    with op.batch_alter_table("movimientos_stock") as batch:
        batch.drop_column("lote_id")

    # 3. Drop tabla lotes
    op.drop_index("ix_lotes_vencimiento", table_name="lotes")
    op.drop_index("ix_lotes_fefo", table_name="lotes")
    op.drop_table("lotes")

    # 2. Variante.atributos: JSONB → JSON
    if _is_postgres():
        op.execute("DROP INDEX IF EXISTS ix_variantes_atributos_gin")
        op.execute(
            "ALTER TABLE variantes "
            "ALTER COLUMN atributos TYPE JSON "
            "USING atributos::json"
        )

    # 1. Drop controla_vencimiento
    with op.batch_alter_table("productos") as batch:
        batch.drop_column("controla_vencimiento")
