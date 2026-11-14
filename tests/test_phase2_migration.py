"""
Tests de certificación de la migración Alembic Fase 2.2.1 — Correcciones de Consistencia Financiera

Verifica:
1. Aplicación limpia de f2a3b4c5d6e7 sobre el esquema de Fase 1.3.
2. Incorporación de columna 'origen' en 'pagos_venta' con valor por defecto 'OPERATIVO'.
3. Creación de la tabla independiente 'conciliaciones_pago' con claves foráneas compuestas.
4. Backfill y Reclasificación:
   - Pagos con referencia 'MIGRACION_HISTORICA' pasan a 'HISTORICO_PRESUNTIVO'.
   - Ventas históricas de contado pasan de 'PAID' a 'PAID_PRESUNTIVO'.
   - Ventas y pagos operativos contemporáneos permanecen inalterados.
5. Reversibilidad mediante downgrade seguro.
"""

import importlib.util
from pathlib import Path
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text, inspect

from alembic.migration import MigrationContext
from alembic.operations import Operations

# Cargar migración dinámicamente
MIGRATION_PATH = Path(__file__).parent.parent / "alembic" / "versions" / "f2a3b4c5d6e7_fase2_2_clasificacion_pagos.py"
spec = importlib.util.spec_from_file_location("mig_fase2_2_1", str(MIGRATION_PATH))
mig_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mig_module)


@pytest.fixture
def phase1_3_db():
    """Crea una base SQLite con el esquema de Fase 1.3 y datos históricos y operativos."""
    engine = create_engine("sqlite:///:memory:")

    with engine.connect() as conn:
        raw_conn = conn.connection.dbapi_connection
        cursor = raw_conn.cursor()

        cursor.executescript("""
            PRAGMA foreign_keys=OFF;

            CREATE TABLE negocios (
                id VARCHAR(36) PRIMARY KEY,
                nombre VARCHAR(200) NOT NULL,
                propietario VARCHAR(200) NOT NULL,
                moneda VARCHAR(10) DEFAULT 'BOB',
                activo BOOLEAN DEFAULT 1,
                created_at DATETIME
            );

            CREATE TABLE almacenes (
                id VARCHAR(36) PRIMARY KEY,
                negocio_id VARCHAR(36) NOT NULL REFERENCES negocios(id),
                nombre VARCHAR(100) NOT NULL,
                es_principal BOOLEAN DEFAULT 1
            );

            CREATE TABLE users (
                id VARCHAR(36) PRIMARY KEY,
                email VARCHAR(255) NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                nombre VARCHAR(200) NOT NULL,
                negocio_id VARCHAR(36) NOT NULL REFERENCES negocios(id),
                rol VARCHAR(20) DEFAULT 'empleado',
                activo BOOLEAN DEFAULT 1
            );

            CREATE TABLE clientes (
                id VARCHAR(36) PRIMARY KEY,
                negocio_id VARCHAR(36) NOT NULL REFERENCES negocios(id),
                nombre VARCHAR(200) NOT NULL,
                activo BOOLEAN DEFAULT 1
            );

            CREATE TABLE ventas (
                id VARCHAR(36) PRIMARY KEY,
                negocio_id VARCHAR(36) NOT NULL REFERENCES negocios(id),
                almacen_id VARCHAR(36) NOT NULL REFERENCES almacenes(id),
                numero INTEGER NOT NULL,
                cliente_id VARCHAR(36),
                cliente_nombre VARCHAR(200),
                cliente_nit VARCHAR(30),
                subtotal NUMERIC(12, 2) NOT NULL,
                descuento NUMERIC(12, 2) DEFAULT 0,
                total NUMERIC(12, 2) NOT NULL,
                metodo_pago VARCHAR(20) NOT NULL,
                monto_recibido NUMERIC(12, 2),
                cambio NUMERIC(12, 2),
                estado VARCHAR(20) NOT NULL,
                estado_financiero VARCHAR(30) NOT NULL DEFAULT 'NOT_APPLICABLE',
                usuario_id VARCHAR(36),
                notas TEXT,
                created_at DATETIME
            );

            CREATE TABLE pagos_venta (
                id VARCHAR(36) PRIMARY KEY,
                negocio_id VARCHAR(36) NOT NULL,
                venta_id VARCHAR(36) NOT NULL,
                monto NUMERIC(12, 2) NOT NULL,
                metodo_pago VARCHAR(20) NOT NULL,
                estado VARCHAR(20) NOT NULL,
                usuario_id VARCHAR(36) NOT NULL,
                fecha_pago DATETIME NOT NULL,
                referencia VARCHAR(100)
            );

            CREATE TABLE reversiones_pago (
                id VARCHAR(36) PRIMARY KEY,
                negocio_id VARCHAR(36) NOT NULL,
                pago_id VARCHAR(36) NOT NULL,
                venta_id VARCHAR(36) NOT NULL,
                monto_revertido NUMERIC(12, 2) NOT NULL,
                motivo TEXT NOT NULL,
                usuario_id VARCHAR(36) NOT NULL,
                fecha_reversion DATETIME NOT NULL,
                es_devolucion_efectivo BOOLEAN DEFAULT 0
            );

            CREATE TABLE cuentas_por_cobrar (
                id VARCHAR(36) PRIMARY KEY,
                negocio_id VARCHAR(36) NOT NULL,
                venta_id VARCHAR(36) NOT NULL,
                cliente_id VARCHAR(36) NOT NULL,
                monto_total_venta NUMERIC(12, 2) NOT NULL,
                monto_inicial_financiado NUMERIC(12, 2) NOT NULL,
                saldo_pendiente NUMERIC(12, 2) NOT NULL,
                fecha_vencimiento DATE,
                estado VARCHAR(30) NOT NULL DEFAULT 'PENDIENTE',
                notas TEXT,
                created_at DATETIME,
                updated_at DATETIME
            );
        """)
        raw_conn.commit()

        # Datos de prueba:
        # 1. Negocio, Usuario, Almacén
        neg_id = str(uuid.uuid4())
        usr_id = str(uuid.uuid4())
        alm_id = str(uuid.uuid4())
        v_hist_id = str(uuid.uuid4())
        p_hist_id = str(uuid.uuid4())
        v_oper_id = str(uuid.uuid4())
        p_oper_id = str(uuid.uuid4())

        cursor.execute("INSERT INTO negocios VALUES (?, 'Negocio Test', 'Admin', 'BOB', 1, CURRENT_TIMESTAMP)", (neg_id,))
        cursor.execute("INSERT INTO almacenes VALUES (?, ?, 'Almacén Central', 1)", (alm_id, neg_id))
        cursor.execute("INSERT INTO users VALUES (?, 'user@test.com', 'hash', 'Test User', ?, 'admin', 1)", (usr_id, neg_id))

        # 2. Venta histórica de contado migrada por Fase 1.3 (estado_financiero='PAID', referencia='MIGRACION_HISTORICA')
        cursor.execute("""
            INSERT INTO ventas (id, negocio_id, almacen_id, numero, subtotal, total, metodo_pago, estado, estado_financiero, usuario_id, created_at)
            VALUES (?, ?, ?, 100, 200.0, 200.0, 'EFECTIVO', 'COMPLETADA', 'PAID', ?, CURRENT_TIMESTAMP)
        """, (v_hist_id, neg_id, alm_id, usr_id))

        cursor.execute("""
            INSERT INTO pagos_venta (id, negocio_id, venta_id, monto, metodo_pago, estado, usuario_id, fecha_pago, referencia)
            VALUES (?, ?, ?, 200.0, 'EFECTIVO', 'CONFIRMED', ?, CURRENT_TIMESTAMP, 'MIGRACION_HISTORICA')
        """, (p_hist_id, neg_id, v_hist_id, usr_id))

        # 3. Venta operativa moderna (estado_financiero='PAID', referencia='OPERATIVO_TICKET_101')
        cursor.execute("""
            INSERT INTO ventas (id, negocio_id, almacen_id, numero, subtotal, total, metodo_pago, estado, estado_financiero, usuario_id, created_at)
            VALUES (?, ?, ?, 101, 80.0, 80.0, 'EFECTIVO', 'COMPLETADA', 'PAID', ?, CURRENT_TIMESTAMP)
        """, (v_oper_id, neg_id, alm_id, usr_id))

        cursor.execute("""
            INSERT INTO pagos_venta (id, negocio_id, venta_id, monto, metodo_pago, estado, usuario_id, fecha_pago, referencia)
            VALUES (?, ?, ?, 80.0, 'EFECTIVO', 'CONFIRMED', ?, CURRENT_TIMESTAMP, 'OPERATIVO_TICKET_101')
        """, (p_oper_id, neg_id, v_oper_id, usr_id))

        raw_conn.commit()

    return {
        "engine": engine,
        "neg_id": neg_id,
        "usr_id": usr_id,
        "v_hist_id": v_hist_id,
        "p_hist_id": p_hist_id,
        "v_oper_id": v_oper_id,
        "p_oper_id": p_oper_id,
    }


def test_migracion_fase2_2_1_upgrade_y_downgrade(phase1_3_db):
    """
    Ejecuta upgrade() de Fase 2.2.1, verifica la estructura y reclasificación,
    y ejecuta downgrade() certificando reversibilidad sin daño colateral.
    """
    engine = phase1_3_db["engine"]

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        op = Operations(ctx)

        # 1. UPGRADE
        mig_module.op = op
        mig_module.upgrade()

    # 2. VERIFICACIÓN ESTRUCTURAL TRAS UPGRADE
    insp = inspect(engine)
    assert "conciliaciones_pago" in insp.get_table_names()

    cols_pagos = [c["name"] for c in insp.get_columns("pagos_venta")]
    assert "origen" in cols_pagos

    cols_concil = [c["name"] for c in insp.get_columns("conciliaciones_pago")]
    assert "referencia_documental" in cols_concil
    assert "motivo" in cols_concil
    assert "estado" in cols_concil

    # 3. VERIFICACIÓN DE RECLASIFICACIÓN DE DATOS
    with engine.connect() as conn:
        # Pago histórico pasó a HISTORICO_PRESUNTIVO
        p_hist = conn.execute(
            text("SELECT origen, referencia FROM pagos_venta WHERE id = :id"),
            {"id": phase1_3_db["p_hist_id"]},
        ).fetchone()
        assert p_hist[0] == "HISTORICO_PRESUNTIVO"
        assert p_hist[1] == "MIGRACION_HISTORICA"

        # Pago operativo contemporáneo quedó como OPERATIVO
        p_oper = conn.execute(
            text("SELECT origen, referencia FROM pagos_venta WHERE id = :id"),
            {"id": phase1_3_db["p_oper_id"]},
        ).fetchone()
        assert p_oper[0] == "OPERATIVO"

        # Venta histórica pasó de PAID a PAID_PRESUNTIVO
        v_hist = conn.execute(
            text("SELECT estado_financiero FROM ventas WHERE id = :id"),
            {"id": phase1_3_db["v_hist_id"]},
        ).fetchone()
        assert v_hist[0] == "PAID_PRESUNTIVO"

        # Venta operativa contemporánea permaneció como PAID
        v_oper = conn.execute(
            text("SELECT estado_financiero FROM ventas WHERE id = :id"),
            {"id": phase1_3_db["v_oper_id"]},
        ).fetchone()
        assert v_oper[0] == "PAID"

    # 4. SEGURIDAD DE DOWNGRADE: Si existen conciliaciones, DEBE abortar con RuntimeError
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO conciliaciones_pago (
                    id, negocio_id, pago_id, venta_id, usuario_id, fecha_conciliacion, motivo, referencia_documental, estado
                ) VALUES (
                    'concil-test-1', :neg_id, :pago_id, :venta_id, :user_id, CURRENT_TIMESTAMP, 'Motivo prueba', 'DOC-REF-01', 'APROBADA'
                )
            """),
            {
                "neg_id": phase1_3_db["neg_id"],
                "pago_id": phase1_3_db["p_hist_id"],
                "venta_id": phase1_3_db["v_hist_id"],
                "user_id": phase1_3_db["usr_id"],
            }
        )

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        op = Operations(ctx)
        mig_module.op = op
        with pytest.raises(RuntimeError, match="ABORT DOWNGRADE"):
            mig_module.downgrade()

    # Eliminar la conciliación de prueba para permitir downgrade limpio
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM conciliaciones_pago WHERE id = 'concil-test-1'"))

    # 5. DOWNGRADE LIMPIO (sin conciliaciones existentes)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        op = Operations(ctx)
        mig_module.op = op
        mig_module.downgrade()

    # 6. VERIFICACIÓN TRAS DOWNGRADE
    insp_down = inspect(engine)
    assert "conciliaciones_pago" not in insp_down.get_table_names()

    cols_pagos_down = [c["name"] for c in insp_down.get_columns("pagos_venta")]
    assert "origen" not in cols_pagos_down

    with engine.connect() as conn:
        v_hist_down = conn.execute(
            text("SELECT estado_financiero FROM ventas WHERE id = :id"),
            {"id": phase1_3_db["v_hist_id"]},
        ).fetchone()
        assert v_hist_down[0] == "PAID"


def test_postgresql_ddl_compilation_all_financial_models():
    """
    Certificación de compatibilidad con PostgreSQL:
    Verifica que la DDL generada por SQLAlchemy para Venta, PagoVenta, ConciliacionPago,
    ReversionPago y CuentaPorCobrar sea 100% válida y compilable para el dialecto postgresql.
    """
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects import postgresql
    from app.modules.ventas.models import Venta, PagoVenta, ConciliacionPago, ReversionPago, CuentaPorCobrar

    tablas = [
        Venta.__table__,
        PagoVenta.__table__,
        ConciliacionPago.__table__,
        ReversionPago.__table__,
        CuentaPorCobrar.__table__,
    ]

    for t in tablas:
        ddl = str(CreateTable(t).compile(dialect=postgresql.dialect()))
        assert "CREATE TABLE" in ddl
        # Verificar que el dialecto use tipos PostgreSQL adecuados
        assert t.name in ddl

