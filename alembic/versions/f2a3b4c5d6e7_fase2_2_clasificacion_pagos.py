"""fase2_2_origen_pagos_y_conciliaciones

Revision ID: f2a3b4c5d6e7
Revises: f1a2b3c4d5e6
Create Date: 2026-11-12 06:15:00.000000

Cambios (Fase 2.2.2 - Validación Final de Integridad Financiera):
1. Desacoplamiento de dimensión de estado de la dimensión de procedencia/evidencia:
   - Agrega columna 'origen' a 'pagos_venta' con valor por defecto 'OPERATIVO'.
   - Agrega CheckConstraint 'chk_pagos_venta_origen' IN ('OPERATIVO', 'HISTORICO_PRESUNTIVO').
   - Agrega índice compuesto por (negocio_id, origen).
2. Creación de tabla 'conciliaciones_pago' para auditoría inmutable de verificación documental:
   - FKs compuestas hacia 'pagos_venta(negocio_id, id, venta_id)' y 'users(negocio_id, id)'.
   - UniqueConstraint 'uq_conciliaciones_negocio_pago' sobre (negocio_id, pago_id) para impedir duplicados.
   - CheckConstraint para estado IN ('APROBADA', 'RECHAZADA').
3. Actualización de restricción CHECK en tabla 'ventas':
   - Modifica/Crea 'chk_ventas_estado_financiero' para admitir 'PAID_PRESUNTIVO' junto a
     ('NOT_APPLICABLE', 'PAID', 'PARTIAL', 'CREDIT').
4. Backfill y Reclasificación conservadora:
   - Pagos migrados con referencia='MIGRACION_HISTORICA' pasan a origen='HISTORICO_PRESUNTIVO'.
   - Ventas completadas históricas de contado pasan de 'PAID' a 'PAID_PRESUNTIVO' hasta conciliarse.
5. Downgrade con guardia de seguridad estricta:
   - Aborta si existen registros en 'conciliaciones_pago' para impedir destrucción de trazabilidad contable.
   - Restaura la restricción original de 4 estados en 'ventas' y revierte el esquema limpiamente.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -----------------------------------------------------------------------
    # 1. Columna 'origen' en 'pagos_venta'
    # -----------------------------------------------------------------------
    with op.batch_alter_table("pagos_venta") as batch:
        batch.add_column(
            sa.Column(
                "origen",
                sa.String(30),
                nullable=False,
                server_default="OPERATIVO",
            )
        )
        batch.create_check_constraint(
            "chk_pagos_venta_origen",
            "origen IN ('OPERATIVO', 'HISTORICO_PRESUNTIVO')",
        )
        batch.create_index(
            "ix_pagos_venta_negocio_origen",
            ["negocio_id", "origen"],
        )

    # -----------------------------------------------------------------------
    # 2. Creación de tabla 'conciliaciones_pago'
    # -----------------------------------------------------------------------
    op.create_table(
        "conciliaciones_pago",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("negocio_id", sa.String(36), nullable=False),
        sa.Column("pago_id", sa.String(36), nullable=False),
        sa.Column("venta_id", sa.String(36), nullable=False),
        sa.Column("usuario_id", sa.String(36), nullable=False),
        sa.Column("fecha_conciliacion", sa.DateTime(timezone=True), nullable=False),
        sa.Column("motivo", sa.Text(), nullable=False),
        sa.Column("referencia_documental", sa.String(100), nullable=False),
        sa.Column("estado", sa.String(20), nullable=False, server_default="APROBADA"),
        sa.Column("notas", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["negocio_id"],
            ["negocios.id"],
            name="fk_conciliaciones_pago_negocio",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["negocio_id", "pago_id", "venta_id"],
            ["pagos_venta.negocio_id", "pagos_venta.id", "pagos_venta.venta_id"],
            name="fk_conciliaciones_pago_venta",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["negocio_id", "usuario_id"],
            ["users.negocio_id", "users.id"],
            name="fk_conciliaciones_negocio_user",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "estado IN ('APROBADA', 'RECHAZADA')",
            name="chk_conciliaciones_estado_valido",
        ),
    )
    op.create_index(
        "ix_conciliaciones_negocio_fecha",
        "conciliaciones_pago",
        ["negocio_id", "fecha_conciliacion"],
    )
    op.create_index(
        "uq_conciliaciones_negocio_pago_aprobada",
        "conciliaciones_pago",
        ["negocio_id", "pago_id"],
        unique=True,
        postgresql_where=sa.text("estado = 'APROBADA'"),
        sqlite_where=sa.text("estado = 'APROBADA'"),
    )

    # -----------------------------------------------------------------------
    # 3. Restricción CHECK en 'ventas.estado_financiero'
    # -----------------------------------------------------------------------
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        bind.execute(
            sa.text(
                "ALTER TABLE ventas DROP CONSTRAINT IF EXISTS chk_ventas_estado_financiero;"
                "ALTER TABLE ventas ADD CONSTRAINT chk_ventas_estado_financiero "
                "CHECK (estado_financiero IN ('NOT_APPLICABLE', 'PAID', 'PAID_PRESUNTIVO', 'PARTIAL', 'CREDIT'));"
            )
        )
    else:
        with op.batch_alter_table("ventas") as batch:
            batch.create_check_constraint(
                "chk_ventas_estado_financiero",
                "estado_financiero IN ('NOT_APPLICABLE', 'PAID', 'PAID_PRESUNTIVO', 'PARTIAL', 'CREDIT')"
            )

    # -----------------------------------------------------------------------
    # 4. Reclasificación de pagos y ventas históricas de Fase 1.3
    # -----------------------------------------------------------------------
    bind = op.get_bind()
    # A. Pagos con referencia MIGRACION_HISTORICA pasan a origen HISTORICO_PRESUNTIVO
    bind.execute(
        sa.text(
            "UPDATE pagos_venta SET origen = 'HISTORICO_PRESUNTIVO' "
            "WHERE referencia = 'MIGRACION_HISTORICA'"
        )
    )

    # B. Ventas históricas que quedaron como PAID con pagos presuntivos pasan a PAID_PRESUNTIVO
    bind.execute(
        sa.text(
            "UPDATE ventas SET estado_financiero = 'PAID_PRESUNTIVO' "
            "WHERE estado_financiero = 'PAID' "
            "AND id IN (SELECT venta_id FROM pagos_venta WHERE origen = 'HISTORICO_PRESUNTIVO')"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()

    # 1. GUARDA DE SEGURIDAD ESTRICTA:
    # Si existen conciliaciones aprobadas u operativas, abortar para impedir pérdida de datos.
    num_conciliaciones = bind.execute(
        sa.text("SELECT COUNT(*) FROM conciliaciones_pago")
    ).scalar() or 0

    if num_conciliaciones > 0:
        raise RuntimeError(
            f"ABORT DOWNGRADE: Existen {num_conciliaciones} registros de conciliación documental en "
            "'conciliaciones_pago'. Eliminar esta tabla provocaría pérdida irreversible de evidencias "
            "contables y trazabilidad de auditoría. Si realmente requiere degradar el esquema, debe "
            "evaluar o respaldar administrativamente estas conciliaciones antes de proceder."
        )

    # 2. Revertir ventas PAID_PRESUNTIVO a PAID antes de restaurar la restricción
    bind.execute(
        sa.text(
            "UPDATE ventas SET estado_financiero = 'PAID' "
            "WHERE estado_financiero = 'PAID_PRESUNTIVO'"
        )
    )

    # 3. Revertir restricción CHECK de estado_financiero en ventas
    dialect = bind.dialect.name
    if dialect == "postgresql":
        bind.execute(
            sa.text(
                "ALTER TABLE ventas DROP CONSTRAINT IF EXISTS chk_ventas_estado_financiero;"
                "ALTER TABLE ventas ADD CONSTRAINT chk_ventas_estado_financiero "
                "CHECK (estado_financiero IN ('NOT_APPLICABLE', 'PAID', 'PARTIAL', 'CREDIT'));"
            )
        )
    else:
        with op.batch_alter_table("ventas") as batch:
            batch.drop_constraint("chk_ventas_estado_financiero", type_="check")
            batch.create_check_constraint(
                "chk_ventas_estado_financiero",
                "estado_financiero IN ('NOT_APPLICABLE', 'PAID', 'PARTIAL', 'CREDIT')"
            )

    # 4. Eliminar tabla de conciliaciones (solo si estaba vacía)
    op.drop_index("uq_conciliaciones_negocio_pago_aprobada", table_name="conciliaciones_pago")
    op.drop_index("ix_conciliaciones_negocio_fecha", table_name="conciliaciones_pago")
    op.drop_table("conciliaciones_pago")

    # 5. Eliminar columna e índice en pagos_venta
    with op.batch_alter_table("pagos_venta") as batch:
        batch.drop_index("ix_pagos_venta_negocio_origen")
        batch.drop_constraint("chk_pagos_venta_origen", type_="check")
        batch.drop_column("origen")
