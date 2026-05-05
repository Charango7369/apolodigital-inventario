"""bloque B: defaults por categoria + atributos esperados

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-05-04 12:00:00.000000

Cambios:
1. Categoria.controla_vencimiento_default: bool, default=False, nullable=False
   Heredado por productos al crearse. NO propaga al cambiar.
2. Categoria.atributos_esperados: jsonb, default={}, nullable=False
   Sets permitidos por atributo. Si vacio {}, no valida.

Estrategia de seguridad:
- Las dos columnas tienen defaults que aplican a registros existentes.
- En SQLite (tests) JSONB cae a JSON automaticamente via JsonAttr en el modelo.
- Migracion preservativa: NO toca categorias existentes mas alla de
  poblarles los valores default.

Rollback: drop de las dos columnas.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers
revision = "e7f8a9b0c1d2"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    # 1. controla_vencimiento_default
    with op.batch_alter_table("categorias") as batch:
        batch.add_column(
            sa.Column(
                "controla_vencimiento_default",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            )
        )

    # 2. atributos_esperados como JSONB en Postgres, JSON en SQLite
    if _is_postgres():
        op.execute(
            "ALTER TABLE categorias ADD COLUMN atributos_esperados JSONB "
            "NOT NULL DEFAULT '{}'::jsonb"
        )
    else:
        with op.batch_alter_table("categorias") as batch:
            batch.add_column(
                sa.Column(
                    "atributos_esperados",
                    sa.JSON(),
                    nullable=False,
                    server_default=sa.text("'{}'"),
                )
            )


def downgrade() -> None:
    with op.batch_alter_table("categorias") as batch:
        batch.drop_column("atributos_esperados")
        batch.drop_column("controla_vencimiento_default")
