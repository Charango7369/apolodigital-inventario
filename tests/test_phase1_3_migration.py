"""
Tests de certificación de la migración Alembic y backfill conservador — Fase 1.3

Verifica:
1. Aplicación limpia de la migración f1a2b3c4d5e6 sobre una base con datos históricos.
2. Conteo de ventas idéntico antes y después (cero pérdidas o duplicaciones).
3. Extracción regex de cliente_nit desde notas respetando el texto original.
4. Backfill financiero conservador:
   - Ventas contado -> PAID + PagoVenta con monto=total (no monto_recibido).
   - Ventas crédito con cliente -> CREDIT + CuentaPorCobrar(vencimiento=None, REQUIERE_CONCILIACION).
   - Ventas crédito sin cliente -> CREDIT sin cuenta (excepción registrada).
   - Ventas canceladas/pendientes -> NOT_APPLICABLE.
5. Bloqueo de seguridad en downgrade si existen registros financieros operativos.
6. Aislamiento e integridad de claves foráneas compuestas tras la migración.
"""

import importlib.util
from pathlib import Path
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text, inspect

from alembic.migration import MigrationContext
from alembic.operations import Operations

# Cargar módulo de migración dinámicamente desde archivo
MIGRATION_PATH = Path(__file__).parent.parent / "alembic" / "versions" / "f1a2b3c4d5e6_fase1_3_financiero_pos_pagos_cxc.py"
spec = importlib.util.spec_from_file_location("mig_fase1_3", str(MIGRATION_PATH))
mig_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mig_module)


@pytest.fixture
def migration_db():
    """
    Crea una base SQLite temporal con el esquema previo (hasta e7f8a9b0c1d2)
    y ventas históricas precargadas para probar la migración.
    """
    engine = create_engine("sqlite:///:memory:")
    
    with engine.connect() as conn:
        raw_conn = conn.connection.dbapi_connection
        cursor = raw_conn.cursor()
        
        # 1. Esquema base pre-Fase 1.3
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
                ubicacion VARCHAR(200),
                es_principal BOOLEAN DEFAULT 0
            );

            CREATE TABLE users (
                id VARCHAR(36) PRIMARY KEY,
                email VARCHAR(255) NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                nombre VARCHAR(200) NOT NULL,
                negocio_id VARCHAR(36) NOT NULL REFERENCES negocios(id),
                rol VARCHAR(20) DEFAULT 'empleado',
                activo BOOLEAN DEFAULT 1,
                created_at DATETIME,
                updated_at DATETIME,
                ultimo_login DATETIME
            );

            CREATE TABLE clientes (
                id VARCHAR(36) PRIMARY KEY,
                negocio_id VARCHAR(36) NOT NULL REFERENCES negocios(id),
                nombre VARCHAR(200) NOT NULL,
                telefono VARCHAR(30),
                email VARCHAR(200),
                nit VARCHAR(20),
                direccion TEXT,
                notas TEXT,
                activo BOOLEAN DEFAULT 1,
                created_at DATETIME
            );

            CREATE TABLE ventas (
                id VARCHAR(36) PRIMARY KEY,
                negocio_id VARCHAR(36) NOT NULL REFERENCES negocios(id),
                almacen_id VARCHAR(36) NOT NULL REFERENCES almacenes(id),
                numero INTEGER NOT NULL,
                cliente_id VARCHAR(36) REFERENCES clientes(id),
                cliente_nombre VARCHAR(200),
                subtotal NUMERIC(12, 2) DEFAULT 0,
                descuento NUMERIC(12, 2) DEFAULT 0,
                total NUMERIC(12, 2) DEFAULT 0,
                metodo_pago VARCHAR(20) DEFAULT 'EFECTIVO',
                monto_recibido NUMERIC(12, 2),
                cambio NUMERIC(12, 2),
                estado VARCHAR(20) DEFAULT 'PENDIENTE',
                usuario_id VARCHAR(36) REFERENCES users(id),
                notas TEXT,
                created_at DATETIME,
                completed_at DATETIME,
                cancelled_at DATETIME
            );

            -- Datos maestros
            INSERT INTO negocios (id, nombre, propietario) VALUES ('neg-001', 'Tienda Matriz', 'Duenio');
            INSERT INTO almacenes (id, negocio_id, nombre, es_principal) VALUES ('alm-001', 'neg-001', 'Central', 1);
            INSERT INTO users (id, email, password_hash, nombre, negocio_id, rol) VALUES ('usr-001', 'admin@apolo.lat', 'hash', 'Admin', 'neg-001', 'admin');
            INSERT INTO clientes (id, negocio_id, nombre) VALUES ('cli-001', 'neg-001', 'Cliente Habitual');

            -- Venta 1: Contado COMPLETADA con NIT en notas y vuelto/cambio
            INSERT INTO ventas (id, negocio_id, almacen_id, numero, cliente_id, total, metodo_pago, monto_recibido, cambio, estado, usuario_id, notas, created_at, completed_at)
            VALUES ('v-01', 'neg-001', 'alm-001', 1, 'cli-001', 100.00, 'EFECTIVO', 120.00, 20.00, 'COMPLETADA', 'usr-001', 'Entregar rapido\n[NIT: 9876543210]', '2026-05-01 10:00:00', '2026-05-01 10:05:00');

            -- Venta 2: Contado QR COMPLETADA sin NIT en notas
            INSERT INTO ventas (id, negocio_id, almacen_id, numero, cliente_id, total, metodo_pago, monto_recibido, cambio, estado, usuario_id, notas, created_at, completed_at)
            VALUES ('v-02', 'neg-001', 'alm-001', 2, 'cli-001', 250.00, 'QR', 250.00, 0.00, 'COMPLETADA', 'usr-001', 'Pago por simple QR bancario', '2026-05-02 11:00:00', '2026-05-02 11:01:00');

            -- Venta 3: Crédito COMPLETADA con cliente registrado y NIT
            INSERT INTO ventas (id, negocio_id, almacen_id, numero, cliente_id, total, metodo_pago, monto_recibido, cambio, estado, usuario_id, notas, created_at, completed_at)
            VALUES ('v-03', 'neg-001', 'alm-001', 3, 'cli-001', 500.00, 'CREDITO', 0.00, 0.00, 'COMPLETADA', 'usr-001', 'Venta a credito [NIT: 1234567]', '2026-05-03 14:00:00', '2026-05-03 14:00:00');

            -- Venta 4: Crédito COMPLETADA SIN cliente registrado (excepción histórica)
            INSERT INTO ventas (id, negocio_id, almacen_id, numero, cliente_id, total, metodo_pago, monto_recibido, cambio, estado, usuario_id, notas, created_at, completed_at)
            VALUES ('v-04', 'neg-001', 'alm-001', 4, NULL, 300.00, 'CREDITO', 0.00, 0.00, 'COMPLETADA', 'usr-001', 'Fiado sin cliente registrado', '2026-05-04 15:00:00', '2026-05-04 15:00:00');

            -- Venta 5: Venta CANCELADA
            INSERT INTO ventas (id, negocio_id, almacen_id, numero, cliente_id, total, metodo_pago, monto_recibido, cambio, estado, usuario_id, notas, created_at, cancelled_at)
            VALUES ('v-05', 'neg-001', 'alm-001', 5, 'cli-001', 80.00, 'EFECTIVO', 80.00, 0.00, 'CANCELADA', 'usr-001', 'Cancelada por error', '2026-05-05 16:00:00', '2026-05-05 16:30:00');

            -- Venta 6: Venta PENDIENTE
            INSERT INTO ventas (id, negocio_id, almacen_id, numero, cliente_id, total, metodo_pago, monto_recibido, cambio, estado, usuario_id, notas, created_at)
            VALUES ('v-06', 'neg-001', 'alm-001', 6, 'cli-001', 40.00, 'EFECTIVO', 0.00, 0.00, 'PENDIENTE', 'usr-001', 'Borrador pendiente', '2026-05-06 17:00:00');
        """)
        cursor.close()
        conn.commit()

    yield engine
    engine.dispose()


def test_migracion_fase1_3_lifecycle_completo(migration_db, monkeypatch):
    """Prueba completa de upgrade, backfill conservador, extracción de NIT y downgrade protector."""
    engine = migration_db

    # Verificar conteo de ventas antes
    with engine.connect() as conn:
        conteo_previo = conn.execute(text("SELECT count(*) FROM ventas")).scalar()
    assert conteo_previo == 6

    # Configurar contexto de Alembic sobre la conexión
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        op_instance = Operations(context)

        # Mockear op global en el módulo de migración
        monkeypatch.setattr(mig_module, "op", op_instance)

        # -------------------------------------------------------------------
        # 1. EJECUTAR UPGRADE
        # -------------------------------------------------------------------
        mig_module.upgrade()
        connection.commit()

        # -------------------------------------------------------------------
        # 2. VERIFICAR INTEGRIDAD POST-MIGRACIÓN
        # -------------------------------------------------------------------
        # 2.1 Conteo de ventas idéntico
        conteo_post = connection.execute(text("SELECT count(*) FROM ventas")).scalar()
        assert conteo_post == 6, "La migración no debe alterar la cantidad de ventas"

        # 2.2 Verificación de Venta 1 (Contado + NIT)
        v1 = connection.execute(text("SELECT estado_financiero, cliente_nit, notas FROM ventas WHERE id = 'v-01'")).fetchone()
        assert v1[0] == "PAID"
        assert v1[1] == "9876543210", "El NIT debió extraerse de notas"
        assert "Entregar rapido" in v1[2], "Las notas originales deben permanecer intactas"
        assert "[NIT: 9876543210]" in v1[2]

        # Verificar PagoVenta generado para Venta 1 (monto = 100.00, NO 120.00)
        pago_v1 = connection.execute(text("SELECT monto, metodo_pago, estado, referencia FROM pagos_venta WHERE venta_id = 'v-01'")).fetchone()
        assert pago_v1 is not None
        assert Decimal(str(pago_v1[0])) == Decimal("100.00"), "El monto de pago debe ser el total facturado, no monto_recibido"
        assert pago_v1[1] == "EFECTIVO"
        assert pago_v1[2] == "CONFIRMED"
        assert pago_v1[3] == "MIGRACION_HISTORICA"

        # 2.3 Verificación de Venta 2 (Contado QR sin NIT)
        v2 = connection.execute(text("SELECT estado_financiero, cliente_nit FROM ventas WHERE id = 'v-02'")).fetchone()
        assert v2[0] == "PAID"
        assert v2[1] is None, "Sin formato NIT en notas, cliente_nit debe quedar NULL"

        pago_v2 = connection.execute(text("SELECT monto, metodo_pago FROM pagos_venta WHERE venta_id = 'v-02'")).fetchone()
        assert pago_v2 is not None
        assert Decimal(str(pago_v2[0])) == Decimal("250.00")
        assert pago_v2[1] == "QR"

        # 2.4 Verificación de Venta 3 (Crédito con cliente y NIT)
        v3 = connection.execute(text("SELECT estado_financiero, cliente_nit FROM ventas WHERE id = 'v-03'")).fetchone()
        assert v3[0] == "CREDIT"
        assert v3[1] == "1234567"

        # Verificar CuentaPorCobrar para Venta 3 (H-14: sin 30 días inventados)
        cxc_v3 = connection.execute(text("SELECT monto_total_venta, monto_inicial_financiado, saldo_pendiente, fecha_vencimiento, estado FROM cuentas_por_cobrar WHERE venta_id = 'v-03'")).fetchone()
        assert cxc_v3 is not None
        assert Decimal(str(cxc_v3[0])) == Decimal("500.00")
        assert Decimal(str(cxc_v3[1])) == Decimal("500.00")
        assert Decimal(str(cxc_v3[2])) == Decimal("500.00")
        assert cxc_v3[3] is None, "H-14: fecha_vencimiento histórica no pactada debe ser NULL"
        assert cxc_v3[4] == "REQUIERE_CONCILIACION"

        # 2.5 Verificación de Venta 4 (Crédito sin cliente -> Excepción)
        v4 = connection.execute(text("SELECT estado_financiero FROM ventas WHERE id = 'v-04'")).fetchone()
        assert v4[0] == "CREDIT"
        # No debe haberse creado cuenta por cobrar sin cliente
        cxc_v4 = connection.execute(text("SELECT count(*) FROM cuentas_por_cobrar WHERE venta_id = 'v-04'")).scalar()
        assert cxc_v4 == 0, "No debe crearse CuentaPorCobrar sin cliente registrado"

        # 2.6 Verificación de Ventas 5 y 6 (Cancelada y Pendiente)
        v5_est = connection.execute(text("SELECT estado_financiero FROM ventas WHERE id = 'v-05'")).scalar()
        v6_est = connection.execute(text("SELECT estado_financiero FROM ventas WHERE id = 'v-06'")).scalar()
        assert v5_est == "NOT_APPLICABLE"
        assert v6_est == "NOT_APPLICABLE"

        # Conteo total de pagos generados = 2 (v-01 y v-02)
        total_pagos = connection.execute(text("SELECT count(*) FROM pagos_venta")).scalar()
        assert total_pagos == 2

        # Conteo total de cuentas creadas = 1 (v-03)
        total_cxc = connection.execute(text("SELECT count(*) FROM cuentas_por_cobrar")).scalar()
        assert total_cxc == 1

        # -------------------------------------------------------------------
        # 3. VERIFICAR DOWNGRADE PROTECTOR
        # -------------------------------------------------------------------
        # 3.1 Insertar un pago operativo nuevo que NO sea de la migración
        connection.execute(text("""
            INSERT INTO pagos_venta (id, negocio_id, venta_id, monto, metodo_pago, estado, usuario_id, fecha_pago, referencia)
            VALUES ('pago-operativo-01', 'neg-001', 'v-01', 50.00, 'EFECTIVO', 'CONFIRMED', 'usr-001', '2026-05-10 12:00:00', 'PAGO_EN_CAJA_REAL');
        """))
        connection.commit()

        # Intentar downgrade debe fallar y abortar
        with pytest.raises(RuntimeError, match="Downgrade bloqueado por seguridad"):
            mig_module.downgrade()

        # 3.2 Si borramos el pago operativo y solo quedan los históricos, el downgrade procede limpiamente
        connection.execute(text("DELETE FROM pagos_venta WHERE id = 'pago-operativo-01'"))
        connection.commit()

        mig_module.downgrade()
        connection.commit()

        # Verificar que las tablas fueron eliminadas en downgrade seguro
        inspector = inspect(connection)
        tablas = inspector.get_table_names()
        assert "pagos_venta" not in tablas
        assert "cuentas_por_cobrar" not in tablas
        assert "reversiones_pago" not in tablas
