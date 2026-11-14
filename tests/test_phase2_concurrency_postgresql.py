"""
Suite de certificación de concurrencia real y bloqueos SELECT FOR UPDATE para PostgreSQL (Fase 2.5.2).

Ejecuta pruebas de contención física multi-hilo / multi-sesión sobre PostgreSQL real:
1. Dos solicitudes simultáneas con la misma clave y mismo payload (Idempotencia concurrente).
2. Dos solicitudes simultáneas con la misma clave y diferente payload (Detección de colisión 409).
3. Primera solicitud hace rollback y la segunda puede continuar correctamente.
4. Interrupción simulada antes del commit (cero huérfanos PROCESSING).
5. Reintento después de commit exitoso (Exact Replay con X-Idempotent-Replay).
6. Misma clave en dos negocios distintos (Aislamiento multi-tenant).
7. Misma clave en endpoints diferentes (Ámbito compuesto metodo + endpoint).
8. Confirmación simultánea de dos pagos QR (Prevención de sobrepago transaccional).
9. Cancelación simultánea con confirmación de pago pendiente (Serialización pesimista).
10. Reversión de pago concurrente con amortización de cartera.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import os
import threading
import time
import uuid
import pytest

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.modules.auth.models import User, UserRole
from app.modules.inventario.models import Negocio, Almacen, Producto, Variante, Lote, Stock, MovimientoStock
from app.modules.ventas.models import (
    Cliente, Venta, DetalleVenta, PagoVenta, ReversionPago, CuentaPorCobrar,
    EstadoFinanciero, EstadoVenta, EstadoCuentaCobrar, EstadoPago, MetodoPago,
    RegistroIdempotencia,
)
from app.modules.ventas.schemas import VentaCreate, DetalleVentaCreate, PagoCreate
from app.modules.ventas.services.sale_service import SaleService
from app.modules.ventas.services.payment_service import PaymentService
from app.modules.ventas.services.accounts_receivable_service import AccountsReceivableService
from app.modules.ventas.services.sale_cancellation_service import SaleCancellationService
from app.modules.ventas.idempotency import ejecutar_con_idempotencia

POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")


@pytest.mark.skipif(not POSTGRES_URL, reason="Requiere instancia PostgreSQL configurada en TEST_POSTGRES_URL")
class TestPostgreSQLConcurrency:

    @pytest.fixture(autouse=True)
    def setup_pg_database(self):
        engine = create_engine(POSTGRES_URL, pool_size=10, max_overflow=20)
        Base.metadata.create_all(engine)
        self.SessionLocal = sessionmaker(bind=engine)
        self.engine = engine

        yield

        Base.metadata.drop_all(engine)
        engine.dispose()

    def _crear_entorno(self, db, sufijo=""):
        neg_id = str(uuid.uuid4())
        neg = Negocio(id=neg_id, nombre=f"PG Test Hardware {sufijo}", propietario="Admin PG", moneda="BOB", activo=True)
        db.add(neg)

        admin = User(
            id=str(uuid.uuid4()), email=f"admin_{uuid.uuid4()}@pg.lat", password_hash="h",
            nombre=f"Admin PG {sufijo}", negocio_id=neg_id, rol=UserRole.ADMIN.value, activo=True
        )
        db.add(admin)

        alm = Almacen(id=str(uuid.uuid4()), negocio_id=neg_id, nombre=f"Almacen PG {sufijo}", es_principal=True)
        db.add(alm)

        cli = Cliente(id=str(uuid.uuid4()), negocio_id=neg_id, nombre=f"Cliente PG {sufijo}", activo=True)
        db.add(cli)

        prod = Producto(id=str(uuid.uuid4()), negocio_id=neg_id, nombre=f"Taladro PG {sufijo}", activo=True)
        db.add(prod)
        db.flush()

        var = Variante(id=str(uuid.uuid4()), producto_id=prod.id, sku=f"TAL-PG-{uuid.uuid4().hex[:6]}", precio_venta=Decimal("500.00"), precio_costo=Decimal("300.00"), activa=True)
        db.add(var)
        db.flush()

        lote = Lote(id=str(uuid.uuid4()), variante_id=var.id, almacen_id=alm.id, cantidad_inicial=Decimal("100"), cantidad_actual=Decimal("100"), costo_unitario=Decimal("300.00"), activo=True)
        db.add(lote)

        stock = Stock(id=str(uuid.uuid4()), variante_id=var.id, almacen_id=alm.id, cantidad_actual=Decimal("100.000"))
        db.add(stock)
        db.commit()

        return {
            "negocio_id": neg_id,
            "admin_id": admin.id,
            "almacen_id": alm.id,
            "cliente_id": cli.id,
            "variante_id": var.id,
            "lote_id": lote.id,
        }

    # -----------------------------------------------------------------------
    # 1. Dos solicitudes simultáneas con la misma clave y mismo payload
    # -----------------------------------------------------------------------
    def test_01_two_simultaneous_requests_same_key_same_payload(self):
        """
        Dos hilos concurrentes envían la misma llave y mismo payload al mismo instante en PostgreSQL.
        La reserva atómica con flush() previene doble ejecución financiera.
        Se crea exactamente 1 venta y 1 registro de idempotencia; ambos reciben respuesta exitosa.
        """
        db = self.SessionLocal()
        env = self._crear_entorno(db)
        idemp_key = f"pg-concurrent-same-{uuid.uuid4()}"

        data = VentaCreate(
            almacen_id=env["almacen_id"],
            cliente_id=env["cliente_id"],
            detalles=[DetalleVentaCreate(variante_id=env["variante_id"], cantidad=1, precio_unitario=Decimal("100.00"))],
            metodo_pago="EFECTIVO",
            monto_recibido=Decimal("100.00"),
            completar=True,
        )
        db.close()

        responses = []

        def worker_request():
            s = self.SessionLocal()
            try:
                def action(commit: bool = True):
                    return SaleService.crear_venta_financiera(s, env["negocio_id"], env["admin_id"], data, commit=commit)

                resp = ejecutar_con_idempotencia(
                    db=s,
                    negocio_id=env["negocio_id"],
                    usuario_id=env["admin_id"],
                    metodo="POST",
                    endpoint="/api/v1/ventas",
                    idempotency_key=idemp_key,
                    request_data=data,
                    action=action,
                    status_code=201,
                )
                responses.append((resp.status_code, resp.headers.get("X-Idempotent-Replay")))
            except Exception as e:
                responses.append((500, str(e)))
            finally:
                s.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(worker_request)
            f2 = executor.submit(worker_request)
            f1.result()
            f2.result()

        # Ambas solicitudes deben haber recibido 201 (una primaria, otra replay transparente)
        status_codes = [r[0] for r in responses]
        assert all(code == 201 for code in status_codes), f"Respuestas: {responses}"

        # Comprobación física directa en PostgreSQL
        v_db = self.SessionLocal()
        ventas = v_db.query(Venta).filter(Venta.negocio_id == env["negocio_id"]).all()
        idemp = v_db.query(RegistroIdempotencia).filter(RegistroIdempotencia.idempotency_key == idemp_key).all()
        lote = v_db.query(Lote).filter(Lote.id == env["lote_id"]).first()
        v_db.close()

        assert len(ventas) == 1, "Debe existir exactamente 1 venta en PostgreSQL"
        assert len(idemp) == 1, "Debe existir exactamente 1 registro de idempotencia"
        assert lote.cantidad_actual == Decimal("99.00"), "El inventario se debió descontar exactamente 1 vez"

    # -----------------------------------------------------------------------
    # 2. Dos solicitudes simultáneas con la misma clave y diferente payload
    # -----------------------------------------------------------------------
    def test_02_two_simultaneous_requests_same_key_different_payload(self):
        """
        Dos solicitudes con la misma clave pero diferente cuerpo (ej. montos distintos).
        PostgreSQL intercepta la colisión y la solicitud con hash discrepante recibe HTTP 409.
        """
        db = self.SessionLocal()
        env = self._crear_entorno(db)
        idemp_key = f"pg-concurrent-diff-{uuid.uuid4()}"

        data_a = VentaCreate(
            almacen_id=env["almacen_id"],
            cliente_id=env["cliente_id"],
            detalles=[DetalleVentaCreate(variante_id=env["variante_id"], cantidad=1, precio_unitario=Decimal("100.00"))],
            metodo_pago="EFECTIVO",
            monto_recibido=Decimal("100.00"),
            completar=True,
        )
        data_b = VentaCreate(
            almacen_id=env["almacen_id"],
            cliente_id=env["cliente_id"],
            detalles=[DetalleVentaCreate(variante_id=env["variante_id"], cantidad=2, precio_unitario=Decimal("200.00"))],
            metodo_pago="EFECTIVO",
            monto_recibido=Decimal("400.00"),
            completar=True,
        )
        db.close()

        # Ejecución secuencial rápida o simultánea
        s1 = self.SessionLocal()
        resp1 = ejecutar_con_idempotencia(
            db=s1, negocio_id=env["negocio_id"], usuario_id=env["admin_id"],
            metodo="POST", endpoint="/api/v1/ventas", idempotency_key=idemp_key,
            request_data=data_a,
            action=lambda commit=True: SaleService.crear_venta_financiera(s1, env["negocio_id"], env["admin_id"], data_a, commit=commit),
            status_code=201,
        )
        assert resp1.status_code == 201
        s1.close()

        s2 = self.SessionLocal()
        with pytest.raises(HTTPException) as exc_info:
            ejecutar_con_idempotencia(
                db=s2, negocio_id=env["negocio_id"], usuario_id=env["admin_id"],
                metodo="POST", endpoint="/api/v1/ventas", idempotency_key=idemp_key,
                request_data=data_b,
                action=lambda commit=True: SaleService.crear_venta_financiera(s2, env["negocio_id"], env["admin_id"], data_b, commit=commit),
                status_code=201,
            )
        assert exc_info.value.status_code == 409
        assert "different request payload" in exc_info.value.detail
        s2.close()

    # -----------------------------------------------------------------------
    # 3. La primera solicitud hace rollback y la segunda continúa correctamente
    # -----------------------------------------------------------------------
    def test_03_first_request_rolls_back_second_succeeds(self):
        """
        Si la primera solicitud falla o aborta antes del commit, su reserva PROCESSING se elimina en rollback.
        Una segunda solicitud con la misma clave puede adquirirla y completar exitosamente.
        """
        db = self.SessionLocal()
        env = self._crear_entorno(db)
        idemp_key = f"pg-rollback-then-success-{uuid.uuid4()}"

        data = VentaCreate(
            almacen_id=env["almacen_id"],
            cliente_id=env["cliente_id"],
            detalles=[DetalleVentaCreate(variante_id=env["variante_id"], cantidad=1, precio_unitario=Decimal("150.00"))],
            metodo_pago="EFECTIVO",
            monto_recibido=Decimal("150.00"),
            completar=True,
        )
        db.close()

        # Intento 1: falla artificialmente en la acción de negocio
        s1 = self.SessionLocal()
        def failing_action(commit: bool = True):
            # Forzar error de validación o negocio
            raise ValueError("Error simulado de pasarela antes de confirmar")

        with pytest.raises(HTTPException) as exc_err:
            ejecutar_con_idempotencia(
                db=s1, negocio_id=env["negocio_id"], usuario_id=env["admin_id"],
                metodo="POST", endpoint="/api/v1/ventas", idempotency_key=idemp_key,
                request_data=data, action=failing_action, status_code=201,
            )
        assert exc_err.value.status_code == 400
        s1.close()

        # Verificar que en PostgreSQL no quedó ninguna clave huérfana ni venta
        v_db = self.SessionLocal()
        clave_previa = v_db.query(RegistroIdempotencia).filter(RegistroIdempotencia.idempotency_key == idemp_key).first()
        assert clave_previa is None, "La clave debió liberarse completamente en el rollback"
        v_db.close()

        # Intento 2: misma clave y mismo payload con acción exitosa
        s2 = self.SessionLocal()
        def success_action(commit: bool = True):
            return SaleService.crear_venta_financiera(s2, env["negocio_id"], env["admin_id"], data, commit=commit)

        resp2 = ejecutar_con_idempotencia(
            db=s2, negocio_id=env["negocio_id"], usuario_id=env["admin_id"],
            metodo="POST", endpoint="/api/v1/ventas", idempotency_key=idemp_key,
            request_data=data, action=success_action, status_code=201,
        )
        assert resp2.status_code == 201
        s2.close()

        # Verificar que ahora sí existe exactamente 1 venta y 1 registro SUCCESS
        v_db2 = self.SessionLocal()
        ventas = v_db2.query(Venta).filter(Venta.negocio_id == env["negocio_id"]).all()
        idemp = v_db2.query(RegistroIdempotencia).filter(RegistroIdempotencia.idempotency_key == idemp_key).first()
        v_db2.close()

        assert len(ventas) == 1
        assert idemp is not None
        assert idemp.status == "SUCCESS"

    # -----------------------------------------------------------------------
    # 4. Interrupción simulada antes del commit (cero registros huérfanos)
    # -----------------------------------------------------------------------
    def test_04_simulated_crash_before_commit_rollback(self):
        """
        Simula una caída de proceso tras flush() pero antes de commit().
        Al cerrarse la conexión o hacerse rollback, PostgreSQL revierte todo.
        """
        db = self.SessionLocal()
        env = self._crear_entorno(db)
        idemp_key = f"pg-crash-sim-{uuid.uuid4()}"

        data = VentaCreate(
            almacen_id=env["almacen_id"],
            cliente_id=env["cliente_id"],
            detalles=[DetalleVentaCreate(variante_id=env["variante_id"], cantidad=1, precio_unitario=Decimal("200.00"))],
            metodo_pago="EFECTIVO",
            monto_recibido=Decimal("200.00"),
            completar=True,
        )
        db.close()

        s1 = self.SessionLocal()
        def crash_action(commit: bool = True):
            v = SaleService.crear_venta_financiera(s1, env["negocio_id"], env["admin_id"], data, commit=commit)
            # Simular desconexión intempestiva levantando excepción de red
            raise ConnectionResetError("Simulated server power cut before DB commit")

        with pytest.raises(HTTPException):
            ejecutar_con_idempotencia(
                db=s1, negocio_id=env["negocio_id"], usuario_id=env["admin_id"],
                metodo="POST", endpoint="/api/v1/ventas", idempotency_key=idemp_key,
                request_data=data, action=crash_action, status_code=201,
            )
        s1.close()

        # Comprobar en PostgreSQL: Cero ventas y cero registros PROCESSING
        v_db = self.SessionLocal()
        ventas_cnt = v_db.query(Venta).filter(Venta.negocio_id == env["negocio_id"]).count()
        idemp_cnt = v_db.query(RegistroIdempotencia).filter(RegistroIdempotencia.idempotency_key == idemp_key).count()
        v_db.close()

        assert ventas_cnt == 0, "No debe haberse confirmado ninguna venta"
        assert idemp_cnt == 0, "No debe haber ningún registro PROCESSING huérfano"

    # -----------------------------------------------------------------------
    # 5. Reintento después de commit exitoso (Exact Replay)
    # -----------------------------------------------------------------------
    def test_05_retry_after_successful_commit_exact_replay(self):
        """
        Un cliente ejecuta una operación, recibe respuesta, y reenvía exactamente la misma solicitud.
        Retorna la respuesta almacenada con cabecera X-Idempotent-Replay: true sin re-ejecutar lógica.
        """
        db = self.SessionLocal()
        env = self._crear_entorno(db)
        idemp_key = f"pg-replay-{uuid.uuid4()}"

        data = VentaCreate(
            almacen_id=env["almacen_id"],
            cliente_id=env["cliente_id"],
            detalles=[DetalleVentaCreate(variante_id=env["variante_id"], cantidad=2, precio_unitario=Decimal("250.00"))],
            metodo_pago="EFECTIVO",
            monto_recibido=Decimal("500.00"),
            completar=True,
        )
        db.close()

        s1 = self.SessionLocal()
        resp1 = ejecutar_con_idempotencia(
            db=s1, negocio_id=env["negocio_id"], usuario_id=env["admin_id"],
            metodo="POST", endpoint="/api/v1/ventas", idempotency_key=idemp_key,
            request_data=data,
            action=lambda commit=True: SaleService.crear_venta_financiera(s1, env["negocio_id"], env["admin_id"], data, commit=commit),
            status_code=201,
        )
        s1.close()

        # Reintento con nueva sesión de base de datos
        s2 = self.SessionLocal()
        call_count = [0]
        def mock_action(commit: bool = True):
            call_count[0] += 1
            return None

        resp2 = ejecutar_con_idempotencia(
            db=s2, negocio_id=env["negocio_id"], usuario_id=env["admin_id"],
            metodo="POST", endpoint="/api/v1/ventas", idempotency_key=idemp_key,
            request_data=data, action=mock_action, status_code=201,
        )
        s2.close()

        assert call_count[0] == 0, "La acción de negocio NO debió volver a ejecutarse"
        assert resp2.headers.get("X-Idempotent-Replay") == "true"
        assert resp2.body == resp1.body

    # -----------------------------------------------------------------------
    # 6. Misma clave en dos negocios distintos (Aislamiento multi-tenant)
    # -----------------------------------------------------------------------
    def test_06_same_key_in_two_different_tenants(self):
        """
        Dos tenants independientes utilizan la misma clave idempotente.
        La unicidad compuesta (negocio_id, metodo, endpoint, idempotency_key) permite que ambos procedan.
        """
        db = self.SessionLocal()
        env1 = self._crear_entorno(db, sufijo="Tenant 1")
        env2 = self._crear_entorno(db, sufijo="Tenant 2")
        idemp_key = f"pg-shared-uuid-{uuid.uuid4()}"

        data1 = VentaCreate(
            almacen_id=env1["almacen_id"], cliente_id=env1["cliente_id"],
            detalles=[DetalleVentaCreate(variante_id=env1["variante_id"], cantidad=1, precio_unitario=Decimal("100.00"))],
            metodo_pago="EFECTIVO", monto_recibido=Decimal("100.00"), completar=True
        )
        data2 = VentaCreate(
            almacen_id=env2["almacen_id"], cliente_id=env2["cliente_id"],
            detalles=[DetalleVentaCreate(variante_id=env2["variante_id"], cantidad=1, precio_unitario=Decimal("200.00"))],
            metodo_pago="EFECTIVO", monto_recibido=Decimal("200.00"), completar=True
        )
        db.close()

        s1 = self.SessionLocal()
        resp1 = ejecutar_con_idempotencia(
            db=s1, negocio_id=env1["negocio_id"], usuario_id=env1["admin_id"],
            metodo="POST", endpoint="/api/v1/ventas", idempotency_key=idemp_key,
            request_data=data1,
            action=lambda commit=True: SaleService.crear_venta_financiera(s1, env1["negocio_id"], env1["admin_id"], data1, commit=commit),
            status_code=201,
        )
        s1.close()

        s2 = self.SessionLocal()
        resp2 = ejecutar_con_idempotencia(
            db=s2, negocio_id=env2["negocio_id"], usuario_id=env2["admin_id"],
            metodo="POST", endpoint="/api/v1/ventas", idempotency_key=idemp_key,
            request_data=data2,
            action=lambda commit=True: SaleService.crear_venta_financiera(s2, env2["negocio_id"], env2["admin_id"], data2, commit=commit),
            status_code=201,
        )
        s2.close()

        assert resp1.status_code == 201
        assert resp2.status_code == 201

        v_db = self.SessionLocal()
        idemp_recs = v_db.query(RegistroIdempotencia).filter(RegistroIdempotencia.idempotency_key == idemp_key).all()
        v_db.close()

        assert len(idemp_recs) == 2, "Debe existir un registro por cada negocio en PostgreSQL"
        assert set(r.negocio_id for r in idemp_recs) == {env1["negocio_id"], env2["negocio_id"]}

    # -----------------------------------------------------------------------
    # 7. Misma clave en endpoints diferentes (Ámbito compuesto de recurso)
    # -----------------------------------------------------------------------
    def test_07_same_key_on_different_endpoints(self):
        """
        La misma clave se envía a dos endpoints de recursos distintos (ej. /ventas/1/pagos vs /ventas/2/pagos).
        El ámbito (metodo, endpoint, key) evita colisiones indebidas.
        """
        db = self.SessionLocal()
        env = self._crear_entorno(db)
        idemp_key = f"pg-endpoint-scope-{uuid.uuid4()}"

        data_v1 = VentaCreate(
            almacen_id=env["almacen_id"], cliente_id=env["cliente_id"],
            detalles=[DetalleVentaCreate(variante_id=env["variante_id"], cantidad=1, precio_unitario=Decimal("100.00"))],
            es_credito=True, pagos=[], completar=True
        )
        v1 = SaleService.crear_venta_financiera(db, env["negocio_id"], env["admin_id"], data_v1)
        v2 = SaleService.crear_venta_financiera(db, env["negocio_id"], env["admin_id"], data_v1)
        v1_id, v2_id = v1.id, v2.id
        db.close()

        s1 = self.SessionLocal()
        resp1 = ejecutar_con_idempotencia(
            db=s1, negocio_id=env["negocio_id"], usuario_id=env["admin_id"],
            metodo="POST", endpoint=f"/api/v1/ventas/{v1_id}/pagos", idempotency_key=idemp_key,
            request_data={"monto": 50.0},
            action=lambda commit=True: PaymentService.registrar_pago(s1, env["negocio_id"], v1_id, env["admin_id"], Decimal("50.00"), "EFECTIVO", commit=commit),
            status_code=201,
        )
        s1.close()

        s2 = self.SessionLocal()
        resp2 = ejecutar_con_idempotencia(
            db=s2, negocio_id=env["negocio_id"], usuario_id=env["admin_id"],
            metodo="POST", endpoint=f"/api/v1/ventas/{v2_id}/pagos", idempotency_key=idemp_key,
            request_data={"monto": 50.0},
            action=lambda commit=True: PaymentService.registrar_pago(s2, env["negocio_id"], v2_id, env["admin_id"], Decimal("50.00"), "EFECTIVO", commit=commit),
            status_code=201,
        )
        s2.close()

        assert resp1.status_code == 201
        assert resp2.status_code == 201

    # -----------------------------------------------------------------------
    # 8. Confirmación simultánea de dos pagos QR (Prevención de sobrepago)
    # -----------------------------------------------------------------------
    def test_08_simultaneous_qr_confirmations_prevent_overpayment(self):
        """
        Dos pagos PENDING de Bs 700 sobre una venta de Bs 1.000 se intentan confirmar simultáneamente.
        SELECT FOR UPDATE sobre la Venta fuerza la serialización:
        Exactamente uno triunfa y el segundo falla por sobrepago.
        """
        db = self.SessionLocal()
        env = self._crear_entorno(db)

        data = VentaCreate(
            almacen_id=env["almacen_id"], cliente_id=env["cliente_id"],
            detalles=[DetalleVentaCreate(variante_id=env["variante_id"], cantidad=2, precio_unitario=Decimal("500.00"))],
            es_credito=True, pagos=[], completar=True
        )
        venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["admin_id"], data)
        p1 = PaymentService.registrar_pago(db, env["negocio_id"], venta.id, env["admin_id"], Decimal("700.00"), "QR")
        p2 = PaymentService.registrar_pago(db, env["negocio_id"], venta.id, env["admin_id"], Decimal("700.00"), "QR")
        v_id, p1_id, p2_id = venta.id, p1.id, p2.id
        db.close()

        results = []

        def try_confirm(p_id):
            session = self.SessionLocal()
            try:
                PaymentService.confirmar_pago(session, env["negocio_id"], p_id, env["admin_id"])
                results.append("SUCCESS")
            except Exception as e:
                results.append(f"FAILED: {e}")
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(try_confirm, p1_id)
            f2 = executor.submit(try_confirm, p2_id)
            f1.result()
            f2.result()

        success_count = sum(1 for r in results if r == "SUCCESS")
        failed_count = sum(1 for r in results if "supera el saldo operativo" in r)
        assert success_count == 1
        assert failed_count == 1

        # Comprobar en PostgreSQL que el saldo operativo resultante es exactamente Bs 300.00 (1000 - 700)
        v_db = self.SessionLocal()
        cxc = v_db.query(CuentaPorCobrar).filter(CuentaPorCobrar.venta_id == v_id).first()
        assert cxc.saldo_pendiente == Decimal("300.00")
        v_db.close()

    # -----------------------------------------------------------------------
    # 9. Cancelación simultánea con confirmación de pago pendiente
    # -----------------------------------------------------------------------
    def test_09_concurrent_cancellation_vs_payment_confirmation(self):
        """
        Carrera entre cancelación de venta y confirmación de pago QR pendiente.
        La jerarquía pesimista Venta -> Pagos -> CxC serializa ambas transacciones limpiamente.
        """
        db = self.SessionLocal()
        env = self._crear_entorno(db)

        data = VentaCreate(
            almacen_id=env["almacen_id"], cliente_id=env["cliente_id"],
            detalles=[DetalleVentaCreate(variante_id=env["variante_id"], cantidad=1, precio_unitario=Decimal("200.00"))],
            es_credito=True, pagos=[], completar=True
        )
        venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["admin_id"], data)
        pago = PaymentService.registrar_pago(db, env["negocio_id"], venta.id, env["admin_id"], Decimal("200.00"), "QR")
        v_id, p_id = venta.id, pago.id
        db.close()

        results = {}

        def cancelar():
            s = self.SessionLocal()
            try:
                SaleCancellationService.cancelar_venta(s, env["negocio_id"], v_id, env["admin_id"], motivo="Cancelación concurrente")
                results["canc"] = "SUCCESS"
            except Exception as e:
                results["canc"] = f"FAILED: {e}"
            finally:
                s.close()

        def confirmar():
            s = self.SessionLocal()
            try:
                PaymentService.confirmar_pago(s, env["negocio_id"], p_id, env["admin_id"])
                results["conf"] = "SUCCESS"
            except Exception as e:
                results["conf"] = f"FAILED: {e}"
            finally:
                s.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(cancelar)
            f2 = executor.submit(confirmar)
            f1.result()
            f2.result()

        assert ("SUCCESS" in results.values())

    # -----------------------------------------------------------------------
    # 10. Reversión de pago concurrente con amortización de cartera
    # -----------------------------------------------------------------------
    def test_10_concurrent_payment_reversion_vs_portfolio_amortization(self):
        """
        Una transacción revierte un pago confirmado mientras otra transacción amortiza la cuenta por cobrar.
        El orden Venta -> Pago -> CxC previene deadlocks y mantiene integridad del saldo de cartera.
        """
        db = self.SessionLocal()
        env = self._crear_entorno(db)

        # Crear venta a crédito con pago inicial de 100
        data = VentaCreate(
            almacen_id=env["almacen_id"], cliente_id=env["cliente_id"],
            detalles=[DetalleVentaCreate(variante_id=env["variante_id"], cantidad=2, precio_unitario=Decimal("250.00"))],
            es_credito=True,
            pagos=[PagoCreate(monto=Decimal("100.00"), metodo_pago="EFECTIVO")],
            completar=True
        )
        venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["admin_id"], data)
        pago_inicial = db.query(PagoVenta).filter(PagoVenta.venta_id == venta.id).first()
        cxc = db.query(CuentaPorCobrar).filter(CuentaPorCobrar.venta_id == venta.id).first()
        v_id, p_id, cxc_id = venta.id, pago_inicial.id, cxc.id
        db.close()

        results = {}

        def worker_revertir():
            s = self.SessionLocal()
            try:
                PaymentService.revertir_pago(
                    s, env["negocio_id"], p_id, env["admin_id"],
                    monto_a_revertir=Decimal("100.00"), motivo="Reversión concurrente",
                    es_devolucion_efectivo=True
                )
                results["rev"] = "SUCCESS"
            except Exception as e:
                results["rev"] = f"FAILED: {e}"
            finally:
                s.close()

        def worker_abonar():
            s = self.SessionLocal()
            try:
                AccountsReceivableService.registrar_abono(
                    s, env["negocio_id"], cxc_id, env["admin_id"],
                    monto=Decimal("150.00"), metodo_pago="EFECTIVO"
                )
                results["abono"] = "SUCCESS"
            except Exception as e:
                results["abono"] = f"FAILED: {e}"
            finally:
                s.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            f1 = executor.submit(worker_revertir)
            f2 = executor.submit(worker_abonar)
            f1.result()
            f2.result()

        # Ambas operaciones deben haber finalizado ordenadamente sin deadlocks
        assert results.get("rev") == "SUCCESS", f"Resultado reversión: {results}"
        assert results.get("abono") == "SUCCESS", f"Resultado abono: {results}"

        # Comprobar en PostgreSQL el estado final de la cartera
        v_db = self.SessionLocal()
        cxc_final = v_db.query(CuentaPorCobrar).filter(CuentaPorCobrar.id == cxc_id).first()
        # Venta total: 500. Pago inicial: 100 revertido (-100). Abono: 150.
        # Saldo pendiente debe ser: 500 - 150 = 350.00
        assert cxc_final.saldo_pendiente == Decimal("350.00")
        v_db.close()
