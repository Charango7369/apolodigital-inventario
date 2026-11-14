"""fase1_3_financiero_pos_pagos_cxc

Revision ID: f1a2b3c4d5e6
Revises: e7f8a9b0c1d2
Create Date: 2026-11-11 22:30:00.000000

Cambios:
1. Incorporación de UniqueConstraint compuesto en 'users(negocio_id, id)'.
2. Incorporación de UniqueConstraint compuesto en 'clientes(negocio_id, id)'.
3. Incorporación de columnas 'cliente_nit' y 'estado_financiero' en 'ventas'.
4. Incorporación de UniqueConstraint compuesto en 'ventas(negocio_id, id)' e índice por 'estado_financiero'.
5. Creación de tabla 'pagos_venta' con FKs compuestas, Checks y sin default en estado.
6. Creación de tabla 'reversiones_pago' con FK compuesta de 3 columnas (negocio_id, pago_id, venta_id).
7. Creación de tabla 'cuentas_por_cobrar' con FKs compuestas, Checks y soporte para históricos no pactados.
8. Migración controlada de NIT desde 'notas' mediante regex (sin alterar texto original de notas).
9. Backfill financiero conservador de ventas históricas:
   - Ventas COMPLETADAS contado -> estado_financiero='PAID' y registro de PagoVenta con monto=total.
   - Ventas COMPLETADAS crédito -> estado_financiero='CREDIT' y CuentaPorCobrar(vencimiento=NULL, REQUIERE_CONCILIACION).
   - Ventas CANCELADAS -> estado_financiero='NOT_APPLICABLE'.
10. Downgrade seguro y no destructivo.
"""

import re
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.orm import Session


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'e7f8a9b0c1d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -----------------------------------------------------------------------
    # 1. Restricciones compuestas en tablas existentes
    # -----------------------------------------------------------------------
    with op.batch_alter_table("users") as batch:
        batch.create_unique_constraint("uq_users_negocio_id", ["negocio_id", "id"])

    with op.batch_alter_table("clientes") as batch:
        batch.create_unique_constraint("uq_clientes_negocio_id", ["negocio_id", "id"])

    # -----------------------------------------------------------------------
    # 2. Columnas, restricciones e índices en 'ventas'
    # -----------------------------------------------------------------------
    with op.batch_alter_table("ventas") as batch:
        batch.add_column(sa.Column("cliente_nit", sa.String(30), nullable=True))
        batch.add_column(
            sa.Column(
                "estado_financiero",
                sa.String(20),
                nullable=False,
                server_default="NOT_APPLICABLE",
            )
        )
        batch.create_unique_constraint("uq_ventas_negocio_id", ["negocio_id", "id"])
        batch.create_index(
            "ix_ventas_estado_financiero",
            ["negocio_id", "estado_financiero"],
        )

    # -----------------------------------------------------------------------
    # 3. Creación de tabla 'pagos_venta'
    # -----------------------------------------------------------------------
    op.create_table(
        "pagos_venta",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("negocio_id", sa.String(36), nullable=False),
        sa.Column("venta_id", sa.String(36), nullable=False),
        sa.Column("monto", sa.Numeric(12, 2), nullable=False),
        sa.Column("metodo_pago", sa.String(20), nullable=False),
        sa.Column("estado", sa.String(20), nullable=False),
        sa.Column("usuario_id", sa.String(36), nullable=False),
        sa.Column("fecha_pago", sa.DateTime(timezone=True), nullable=False),
        sa.Column("referencia", sa.String(100), nullable=True),
        sa.ForeignKeyConstraint(["negocio_id"], ["negocios.id"], name="fk_pagos_venta_negocio", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["negocio_id", "venta_id"], ["ventas.negocio_id", "ventas.id"], name="fk_pagos_venta_negocio_venta", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["negocio_id", "usuario_id"], ["users.negocio_id", "users.id"], name="fk_pagos_venta_negocio_user", ondelete="RESTRICT"),
        sa.UniqueConstraint("negocio_id", "id", "venta_id", name="uq_pagos_venta_negocio_id_venta"),
        sa.UniqueConstraint("negocio_id", "id", name="uq_pagos_venta_negocio_id"),
        sa.CheckConstraint("monto > 0", name="chk_pagos_venta_monto_positivo"),
        sa.CheckConstraint("estado IN ('PENDING', 'CONFIRMED', 'FAILED')", name="chk_pagos_venta_estado_valido"),
    )
    op.create_index("ix_pagos_venta_negocio_fecha", "pagos_venta", ["negocio_id", "fecha_pago"])
    op.create_index("ix_pagos_venta_negocio_metodo", "pagos_venta", ["negocio_id", "metodo_pago"])
    op.create_index("ix_pagos_venta_negocio_estado", "pagos_venta", ["negocio_id", "estado"])

    # -----------------------------------------------------------------------
    # 4. Creación de tabla 'reversiones_pago'
    # -----------------------------------------------------------------------
    op.create_table(
        "reversiones_pago",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("negocio_id", sa.String(36), nullable=False),
        sa.Column("pago_id", sa.String(36), nullable=False),
        sa.Column("venta_id", sa.String(36), nullable=False),
        sa.Column("monto_revertido", sa.Numeric(12, 2), nullable=False),
        sa.Column("motivo", sa.Text(), nullable=False),
        sa.Column("usuario_id", sa.String(36), nullable=False),
        sa.Column("fecha_reversion", sa.DateTime(timezone=True), nullable=False),
        sa.Column("es_devolucion_efectivo", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.ForeignKeyConstraint(["negocio_id"], ["negocios.id"], name="fk_reversiones_pago_negocio", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["negocio_id", "pago_id", "venta_id"], ["pagos_venta.negocio_id", "pagos_venta.id", "pagos_venta.venta_id"], name="fk_reversiones_pago_venta", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["negocio_id", "usuario_id"], ["users.negocio_id", "users.id"], name="fk_reversiones_negocio_user", ondelete="RESTRICT"),
        sa.CheckConstraint("monto_revertido > 0", name="chk_reversiones_pago_monto_positivo"),
    )
    op.create_index("ix_reversiones_negocio_fecha", "reversiones_pago", ["negocio_id", "fecha_reversion"])

    # -----------------------------------------------------------------------
    # 5. Creación de tabla 'cuentas_por_cobrar'
    # -----------------------------------------------------------------------
    op.create_table(
        "cuentas_por_cobrar",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("negocio_id", sa.String(36), nullable=False),
        sa.Column("venta_id", sa.String(36), nullable=False),
        sa.Column("cliente_id", sa.String(36), nullable=False),
        sa.Column("monto_total_venta", sa.Numeric(12, 2), nullable=False),
        sa.Column("monto_inicial_financiado", sa.Numeric(12, 2), nullable=False),
        sa.Column("saldo_pendiente", sa.Numeric(12, 2), nullable=False),
        sa.Column("fecha_vencimiento", sa.Date(), nullable=True),
        sa.Column("estado", sa.String(30), nullable=False, server_default="PENDIENTE"),
        sa.Column("notas", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["negocio_id"], ["negocios.id"], name="fk_cuentas_cobrar_negocio", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["negocio_id", "venta_id"], ["ventas.negocio_id", "ventas.id"], name="fk_cxc_negocio_venta", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["negocio_id", "cliente_id"], ["clientes.negocio_id", "clientes.id"], name="fk_cxc_negocio_cliente", ondelete="RESTRICT"),
        sa.UniqueConstraint("negocio_id", "venta_id", name="uq_cxc_negocio_venta"),
        sa.CheckConstraint("monto_total_venta > 0", name="chk_cxc_monto_total_positivo"),
        sa.CheckConstraint("monto_inicial_financiado >= 0 AND monto_inicial_financiado <= monto_total_venta", name="chk_cxc_monto_inicial_rango"),
        sa.CheckConstraint("saldo_pendiente >= 0 AND saldo_pendiente <= monto_total_venta", name="chk_cxc_saldo_pendiente_rango"),
        sa.CheckConstraint("estado IN ('PENDIENTE', 'PAGADA', 'CANCELADA', 'REQUIERE_CONCILIACION')", name="chk_cxc_estado_valido"),
    )
    op.create_index("ix_cxc_negocio_vencimiento", "cuentas_por_cobrar", ["negocio_id", "fecha_vencimiento"])
    op.create_index("ix_cxc_negocio_estado", "cuentas_por_cobrar", ["negocio_id", "estado"])

    # -----------------------------------------------------------------------
    # 6. Extracción controlada de NIT desde notas (Regex)
    # -----------------------------------------------------------------------
    bind = op.get_bind()
    session = Session(bind=bind)

    nit_pattern = re.compile(r'\[NIT:\s*([A-Za-z0-9\-]+)\]')

    # Metadatos para consulta cruda
    ventas_t = sa.Table(
        "ventas",
        sa.MetaData(),
        sa.Column("id", sa.String(36)),
        sa.Column("negocio_id", sa.String(36)),
        sa.Column("numero", sa.Integer()),
        sa.Column("cliente_id", sa.String(36)),
        sa.Column("cliente_nit", sa.String(30)),
        sa.Column("total", sa.Numeric(12, 2)),
        sa.Column("metodo_pago", sa.String(20)),
        sa.Column("estado", sa.String(20)),
        sa.Column("estado_financiero", sa.String(20)),
        sa.Column("usuario_id", sa.String(36)),
        sa.Column("notas", sa.Text()),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("completed_at", sa.DateTime()),
    )

    pagos_t = sa.Table(
        "pagos_venta",
        sa.MetaData(),
        sa.Column("id", sa.String(36)),
        sa.Column("negocio_id", sa.String(36)),
        sa.Column("venta_id", sa.String(36)),
        sa.Column("monto", sa.Numeric(12, 2)),
        sa.Column("metodo_pago", sa.String(20)),
        sa.Column("estado", sa.String(20)),
        sa.Column("usuario_id", sa.String(36)),
        sa.Column("fecha_pago", sa.DateTime(timezone=True)),
        sa.Column("referencia", sa.String(100)),
    )

    cxc_t = sa.Table(
        "cuentas_por_cobrar",
        sa.MetaData(),
        sa.Column("id", sa.String(36)),
        sa.Column("negocio_id", sa.String(36)),
        sa.Column("venta_id", sa.String(36)),
        sa.Column("cliente_id", sa.String(36)),
        sa.Column("monto_total_venta", sa.Numeric(12, 2)),
        sa.Column("monto_inicial_financiado", sa.Numeric(12, 2)),
        sa.Column("saldo_pendiente", sa.Numeric(12, 2)),
        sa.Column("fecha_vencimiento", sa.Date()),
        sa.Column("estado", sa.String(30)),
        sa.Column("notas", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )

    users_t = sa.Table(
        "users",
        sa.MetaData(),
        sa.Column("id", sa.String(36)),
        sa.Column("negocio_id", sa.String(36)),
        sa.Column("rol", sa.String(20)),
    )

    # 6.1 Extracción regex de NIT
    select_ventas_notas = sa.select(ventas_t.c.id, ventas_t.c.notas).where(ventas_t.c.notas.isnot(None))
    for v_id, notas_txt in session.execute(select_ventas_notas).fetchall():
        if not notas_txt:
            continue
        match = nit_pattern.search(notas_txt)
        if match:
            nit_extraido = match.group(1).strip()
            if nit_extraido:
                session.execute(
                    sa.update(ventas_t)
                    .where(ventas_t.c.id == v_id)
                    .values(cliente_nit=nit_extraido)
                )

    # -----------------------------------------------------------------------
    # 7. Backfill financiero conservador
    # -----------------------------------------------------------------------
    # Resolver usuarios admin de respaldo por tenant si alguna venta tiene usuario_id NULL
    admins_por_negocio = {}
    users_rows = session.execute(sa.select(users_t.c.negocio_id, users_t.c.id)).fetchall()
    for neg_id, u_id in users_rows:
        if neg_id not in admins_por_negocio:
            admins_por_negocio[neg_id] = u_id

    # Consultar todas las ventas para clasificación financiera
    todas_ventas = session.execute(sa.select(ventas_t)).fetchall()

    for v in todas_ventas:
        # Resolver usuario_id para registros financieros
        usuario_responsable = v.usuario_id or admins_por_negocio.get(v.negocio_id)
        fecha_registro = v.completed_at or v.created_at

        # Caso 1: Venta COMPLETADA con cobro de contado verificable
        if v.estado == "COMPLETADA" and v.metodo_pago in ("EFECTIVO", "QR", "TARJETA", "TRANSFERENCIA"):
            session.execute(
                sa.update(ventas_t)
                .where(ventas_t.c.id == v.id)
                .values(estado_financiero="PAID")
            )
            # Si el total > 0 y tenemos usuario, creamos el pago histórico confirmado
            if v.total and v.total > 0 and usuario_responsable:
                session.execute(
                    sa.insert(pagos_t).values(
                        id=str(uuid.uuid4()),
                        negocio_id=v.negocio_id,
                        venta_id=v.id,
                        monto=v.total,  # NETO FACTURADO (no monto_recibido)
                        metodo_pago=v.metodo_pago,
                        estado="CONFIRMED",
                        usuario_id=usuario_responsable,
                        fecha_pago=fecha_registro,
                        referencia="MIGRACION_HISTORICA",
                    )
                )

        # Caso 2: Venta COMPLETADA a crédito
        elif v.estado == "COMPLETADA" and v.metodo_pago == "CREDITO":
            session.execute(
                sa.update(ventas_t)
                .where(ventas_t.c.id == v.id)
                .values(estado_financiero="CREDIT")
            )
            # Solo si tiene cliente registrado creamos la CuentaPorCobrar histórica
            if v.cliente_id and v.total and v.total > 0:
                session.execute(
                    sa.insert(cxc_t).values(
                        id=str(uuid.uuid4()),
                        negocio_id=v.negocio_id,
                        venta_id=v.id,
                        cliente_id=v.cliente_id,
                        monto_total_venta=v.total,
                        monto_inicial_financiado=v.total,
                        saldo_pendiente=v.total,
                        fecha_vencimiento=None,  # H-14: No inventar 30 días
                        estado="REQUIERE_CONCILIACION",
                        notas="Crédito histórico migrado: pendiente de acordar fecha límite con el cliente",
                        created_at=v.created_at,
                        updated_at=fecha_registro,
                    )
                )

        # Caso 3: Venta CANCELADA
        elif v.estado == "CANCELADA":
            session.execute(
                sa.update(ventas_t)
                .where(ventas_t.c.id == v.id)
                .values(estado_financiero="NOT_APPLICABLE")
            )

        # Caso 4: Venta PENDIENTE
        else:
            session.execute(
                sa.update(ventas_t)
                .where(ventas_t.c.id == v.id)
                .values(estado_financiero="NOT_APPLICABLE")
            )

    session.flush()


def downgrade() -> None:
    # -----------------------------------------------------------------------
    # Downgrade protector: abortar si existen registros financieros operativos
    # -----------------------------------------------------------------------
    bind = op.get_bind()
    session = Session(bind=bind)

    # Verificar si hay pagos operativos (no generados por la migración histórica)
    try:
        pagos_count = session.execute(
            sa.text("SELECT count(*) FROM pagos_venta WHERE referencia != 'MIGRACION_HISTORICA' OR referencia IS NULL")
        ).scalar()
        if pagos_count and pagos_count > 0:
            raise RuntimeError(
                f"Downgrade bloqueado por seguridad: existen {pagos_count} pagos operativos "
                "que se perderían irreversiblemente."
            )
    except Exception as e:
        if "no such table" not in str(e).lower() and "does not exist" not in str(e).lower():
            # Si la tabla existe y tiene registros, relanzar
            if isinstance(e, RuntimeError):
                raise e

    # 1. Eliminar tablas financieras
    op.drop_table("cuentas_por_cobrar")
    op.drop_table("reversiones_pago")
    op.drop_table("pagos_venta")

    # 2. Revertir columnas y restricciones en ventas
    with op.batch_alter_table("ventas") as batch:
        batch.drop_index("ix_ventas_estado_financiero")
        batch.drop_constraint("uq_ventas_negocio_id", type_="unique")
        batch.drop_column("estado_financiero")
        batch.drop_column("cliente_nit")

    # 3. Revertir restricciones en clientes y users
    with op.batch_alter_table("clientes") as batch:
        batch.drop_constraint("uq_clientes_negocio_id", type_="unique")

    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("uq_users_negocio_id", type_="unique")
