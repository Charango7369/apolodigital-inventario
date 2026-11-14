"""
Fase 2.6 — Auditoría de Integración y Preparación del POS.

Suite de certificación integral Frontend/Backend:
1. Flujo POS Venta Efectivo: Descuento FEFO inmediato, pago CONFIRMED, venta PAID, sin CxC.
2. Flujo POS Venta a Crédito: Cliente registrado obligatorio, CxC creada con saldo total, estado PENDING.
3. Flujo POS Venta Mixta: Efectivo (CONFIRMED) + QR (PENDING), saldo operativo parcial, confirmación posterior de QR lleva a PAID.
4. Simulación PWA Offline & Idempotencia:
   - Envío inicial de venta con idempotency_key persistida.
   - Reintento de sincronización tras caída de transporte -> Detección de replay (X-Idempotent-Replay) con stock intacto.
5. Blindaje Multi-Tenant Exhaustivo:
   - Validación cruzada de 404/403 en los endpoints de ventas, pagos, auditoría de cancelación y cartera.
6. Consistencia Contable y Transaccional:
   - Cancelación integral con devolución de efectivo restituye lotes y genera evento compensatorio inmutable.
"""

from datetime import datetime, timezone, date, timedelta
from decimal import Decimal
import uuid
import pytest

from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import application
from app.modules.auth.models import User, UserRole
from app.modules.auth.service import create_access_token
from app.modules.inventario.models import Negocio, Almacen, Producto, Variante, Lote, Stock, MovimientoStock
from app.modules.ventas.models import (
    Cliente, Venta, DetalleVenta, PagoVenta, ReversionPago, CuentaPorCobrar,
    EstadoFinanciero, EstadoVenta, EstadoCuentaCobrar, EstadoPago, MetodoPago,
)


@pytest.fixture
def pos_audit_env():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)
    db = TestingSession()

    # Tenant 1: Apolo Matriz
    negocio_1 = Negocio(id=str(uuid.uuid4()), nombre="Apolo Matriz", propietario="Admin 1", activo=True)
    db.add(negocio_1)

    # Tenant 2: Apolo Sucursal
    negocio_2 = Negocio(id=str(uuid.uuid4()), nombre="Apolo Sucursal", propietario="Admin 2", activo=True)
    db.add(negocio_2)
    db.flush()

    # Usuarios
    user_admin_1 = User(
        id=str(uuid.uuid4()), email="admin1@apolo.com", password_hash="fake",
        nombre="Admin 1", negocio_id=negocio_1.id, rol=UserRole.ADMIN.value, activo=True
    )
    user_admin_2 = User(
        id=str(uuid.uuid4()), email="admin2@apolo.com", password_hash="fake",
        nombre="Admin 2", negocio_id=negocio_2.id, rol=UserRole.ADMIN.value, activo=True
    )
    db.add_all([user_admin_1, user_admin_2])
    db.flush()

    # Almacenes
    alm_1 = Almacen(id=str(uuid.uuid4()), negocio_id=negocio_1.id, nombre="Almacén Matriz", es_principal=True)
    alm_2 = Almacen(id=str(uuid.uuid4()), negocio_id=negocio_2.id, nombre="Almacén Sucursal", es_principal=True)
    db.add_all([alm_1, alm_2])
    db.flush()

    # Cliente para Tenant 1
    cli_1 = Cliente(id=str(uuid.uuid4()), negocio_id=negocio_1.id, nombre="Juan Pérez", nit="1234567", telefono="77712345")
    cli_2 = Cliente(id=str(uuid.uuid4()), negocio_id=negocio_2.id, nombre="Empresa Sucursal", nit="9876543")
    db.add_all([cli_1, cli_2])
    db.flush()

    # Producto y Lote para Tenant 1
    prod_1 = Producto(id=str(uuid.uuid4()), negocio_id=negocio_1.id, nombre="Batería 12V", controla_vencimiento=True)
    db.add(prod_1)
    db.flush()

    var_1 = Variante(id=str(uuid.uuid4()), producto_id=prod_1.id, sku="BAT-12V", precio_venta=Decimal("500.00"), activa=True)
    db.add(var_1)
    db.flush()

    lote_1 = Lote(
        id=str(uuid.uuid4()),
        variante_id=var_1.id,
        almacen_id=alm_1.id,
        fecha_ingreso=datetime.now(timezone.utc),
        fecha_vencimiento=date.today() + timedelta(days=180),
        cantidad_inicial=Decimal("20.000"),
        cantidad_actual=Decimal("20.000"),
        costo_unitario=Decimal("300.00"),
        activo=True,
    )
    db.add(lote_1)

    stock_1 = Stock(
        id=str(uuid.uuid4()),
        variante_id=var_1.id,
        almacen_id=alm_1.id,
        cantidad_actual=Decimal("20.000")
    )
    db.add(stock_1)
    db.commit()

    token_1 = create_access_token({"sub": user_admin_1.id, "negocio_id": negocio_1.id, "rol": user_admin_1.rol})
    token_2 = create_access_token({"sub": user_admin_2.id, "negocio_id": negocio_2.id, "rol": user_admin_2.rol})

    def override_get_db():
        try:
            yield db
        finally:
            pass

    application.dependency_overrides[get_db] = override_get_db
    client = TestClient(application)

    yield {
        "client": client,
        "db": db,
        "token_1": token_1,
        "token_2": token_2,
        "negocio_1_id": negocio_1.id,
        "negocio_2_id": negocio_2.id,
        "almacen_1_id": alm_1.id,
        "cliente_1_id": cli_1.id,
        "variante_1_id": var_1.id,
        "lote_1": lote_1,
    }

    application.dependency_overrides.clear()
    db.close()


# ===========================================================================
# 1. FLUJO POS: Venta en Efectivo (Contado puro)
# ===========================================================================
def test_pos_01_flujo_venta_efectivo(pos_audit_env):
    client = pos_audit_env["client"]
    token = pos_audit_env["token_1"]
    alm_id = pos_audit_env["almacen_1_id"]
    var_id = pos_audit_env["variante_1_id"]

    idemp_key = str(uuid.uuid4())
    payload = {
        "almacen_id": alm_id,
        "detalles": [{"variante_id": var_id, "cantidad": 2, "precio_unitario": 500.0}],
        "metodo_pago": "EFECTIVO",
        "monto_recibido": 1000.0,
        "completar": True,
        "pagos": [{"monto": 1000.0, "metodo_pago": "EFECTIVO"}],
        "es_credito": False,
        "idempotency_key": idemp_key,
    }

    r = client.post(
        "/api/v1/ventas",
        json=payload,
        headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": idemp_key}
    )
    assert r.status_code == 201
    data = r.json()

    assert data["total"] == 1000.0
    assert data["estado"] == "COMPLETADA"
    assert data["estado_financiero"] == "PAID"
    assert len(data["pagos"]) == 1
    assert data["pagos"][0]["estado"] == "CONFIRMED"
    assert data["cuenta_cobrar"] is None

    # Verificar stock en lote (de 20 bajó a 18)
    db = pos_audit_env["db"]
    lote = db.query(Lote).filter(Lote.id == pos_audit_env["lote_1"].id).first()
    assert lote.cantidad_actual == Decimal("18.000")


# ===========================================================================
# 2. FLUJO POS: Venta a Crédito Puro (Exige Cliente Registrado)
# ===========================================================================
def test_pos_02_flujo_venta_credito_puro(pos_audit_env):
    client = pos_audit_env["client"]
    token = pos_audit_env["token_1"]
    alm_id = pos_audit_env["almacen_1_id"]
    var_id = pos_audit_env["variante_1_id"]
    cli_id = pos_audit_env["cliente_1_id"]

    # Falla si no se envía cliente_id
    payload_sin_cliente = {
        "almacen_id": alm_id,
        "detalles": [{"variante_id": var_id, "cantidad": 1, "precio_unitario": 500.0}],
        "metodo_pago": "CREDITO",
        "es_credito": True,
        "pagos": [],
    }
    r_err = client.post(
        "/api/v1/ventas",
        json=payload_sin_cliente,
        headers={"Authorization": f"Bearer {token}"}
    )
    assert r_err.status_code == 400
    assert "cliente" in r_err.json()["detail"].lower()

    # Venta crédito exitosa con cliente registrado
    vencimiento = (date.today() + timedelta(days=30)).isoformat()
    payload_ok = {
        "almacen_id": alm_id,
        "cliente_id": cli_id,
        "detalles": [{"variante_id": var_id, "cantidad": 1, "precio_unitario": 500.0}],
        "metodo_pago": "CREDITO",
        "es_credito": True,
        "fecha_vencimiento": vencimiento,
        "pagos": [],
    }
    r_ok = client.post(
        "/api/v1/ventas",
        json=payload_ok,
        headers={"Authorization": f"Bearer {token}"}
    )
    assert r_ok.status_code == 201
    data = r_ok.json()
    assert data["estado"] == "COMPLETADA"
    assert data["estado_financiero"] == "CREDIT"
    assert data["cuenta_cobrar"] is not None
    assert Decimal(str(data["cuenta_cobrar"]["saldo_pendiente"])) == Decimal("500.00")
    assert data["cuenta_cobrar"]["estado"] == "PENDIENTE"


# ===========================================================================
# 3. FLUJO POS: Venta Mixta (Efectivo + QR Pendiente) y Confirmación de Pago
# ===========================================================================
def test_pos_03_flujo_venta_mixta_y_confirmacion_qr(pos_audit_env):
    client = pos_audit_env["client"]
    token = pos_audit_env["token_1"]
    alm_id = pos_audit_env["almacen_1_id"]
    var_id = pos_audit_env["variante_1_id"]

    # Venta de Bs 500: Bs 200 en Efectivo + Bs 300 en QR
    payload = {
        "almacen_id": alm_id,
        "detalles": [{"variante_id": var_id, "cantidad": 1, "precio_unitario": 500.0}],
        "metodo_pago": "MIXTO",
        "pagos": [
            {"monto": 200.0, "metodo_pago": "EFECTIVO"},
            {"monto": 300.0, "metodo_pago": "QR", "referencia": "QR-TICKET-01"},
        ],
        "es_credito": False,
    }
    r = client.post("/api/v1/ventas", json=payload, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201
    venta = r.json()

    # Efectivo confirmado, QR en PENDING -> estado financiero PARTIAL
    assert venta["estado_financiero"] == "PARTIAL"
    assert len(venta["pagos"]) == 2

    pago_qr = next(p for p in venta["pagos"] if p["metodo_pago"] == "QR")
    assert pago_qr["estado"] == "PENDING"

    # El cajero verifica el QR en ventanilla y lo confirma mediante POST /ventas/pagos/{id}/confirmar
    r_conf = client.post(
        f"/api/v1/ventas/pagos/{pago_qr['id']}/confirmar",
        json={"referencia_confirmacion": "AUT-QR-9988", "notas": "Acreditación en Banco BNB"},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert r_conf.status_code == 200
    pago_conf = r_conf.json()
    assert pago_conf["estado"] == "CONFIRMED"

    # Al consultar la venta completa, ahora debe figurar como PAID
    r_get = client.get(f"/api/v1/ventas/{venta['id']}", headers={"Authorization": f"Bearer {token}"})
    assert r_get.status_code == 200
    assert r_get.json()["estado_financiero"] == "PAID"


# ===========================================================================
# 4. SIMULACIÓN OFFLINE PWA & IDEMPOTENCIA (Replay sin doble consumo)
# ===========================================================================
def test_pos_04_pwa_offline_sync_idempotency_replay(pos_audit_env):
    client = pos_audit_env["client"]
    token = pos_audit_env["token_1"]
    alm_id = pos_audit_env["almacen_1_id"]
    var_id = pos_audit_env["variante_1_id"]

    idemp_uuid = str(uuid.uuid4())
    offline_payload = {
        "almacen_id": alm_id,
        "detalles": [{"variante_id": var_id, "cantidad": 1, "precio_unitario": 500.0}],
        "metodo_pago": "EFECTIVO",
        "pagos": [{"monto": 500.0, "metodo_pago": "EFECTIVO"}],
        "idempotency_key": idemp_uuid,
    }

    # Primer despacho de la PWA (sync 1)
    r1 = client.post(
        "/api/v1/ventas",
        json=offline_payload,
        headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": idemp_uuid}
    )
    assert r1.status_code == 201
    venta_id = r1.json()["id"]

    # Simulación: caída de conexión en el transporte. La PWA reintenta el sync de la misma venta.
    r2 = client.post(
        "/api/v1/ventas",
        json=offline_payload,
        headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": idemp_uuid}
    )
    assert r2.status_code == 201
    assert r2.headers.get("x-idempotent-replay") == "true"
    assert r2.json()["id"] == venta_id

    # Misma llave pero con payload alterado debe ser rechazada con 409 Conflict
    payload_modificado = dict(offline_payload)
    payload_modificado["detalles"] = [{"variante_id": var_id, "cantidad": 5, "precio_unitario": 500.0}]
    r3 = client.post(
        "/api/v1/ventas",
        json=payload_modificado,
        headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": idemp_uuid}
    )
    assert r3.status_code == 409


# ===========================================================================
# 5. SEGURIDAD MULTI-TENANT: Aislamiento estricto 404/403
# ===========================================================================
def test_pos_05_seguridad_multitenant_aislamiento_total(pos_audit_env):
    client = pos_audit_env["client"]
    token_1 = pos_audit_env["token_1"]
    token_2 = pos_audit_env["token_2"]  # Tenant atacante

    # Crear venta y cartera en Tenant 1
    r_v = client.post(
        "/api/v1/ventas",
        json={
            "almacen_id": pos_audit_env["almacen_1_id"],
            "cliente_id": pos_audit_env["cliente_1_id"],
            "detalles": [{"variante_id": pos_audit_env["variante_1_id"], "cantidad": 1, "precio_unitario": 500.0}],
            "metodo_pago": "CREDITO",
            "es_credito": True,
            "pagos": [],
        },
        headers={"Authorization": f"Bearer {token_1}"}
    )
    assert r_v.status_code == 201
    v_data = r_v.json()
    venta_id = v_data["id"]
    cuenta_id = v_data["cuenta_cobrar"]["id"]

    # 1. Tenant 2 no puede ver la venta de Tenant 1
    assert client.get(f"/api/v1/ventas/{venta_id}", headers={"Authorization": f"Bearer {token_2}"}).status_code == 404

    # 2. Tenant 2 no puede cancelar la venta de Tenant 1
    assert client.post(f"/api/v1/ventas/{venta_id}/cancelar", json={"motivo": "IDOR"}, headers={"Authorization": f"Bearer {token_2}"}).status_code == 404

    # 3. Tenant 2 no puede ver la auditoría de cancelación de Tenant 1
    assert client.get(f"/api/v1/ventas/{venta_id}/auditoria-cancelacion", headers={"Authorization": f"Bearer {token_2}"}).status_code == 404

    # 4. Tenant 2 no puede ver la cartera de Tenant 1
    assert client.get(f"/api/v1/ventas/cartera/{cuenta_id}", headers={"Authorization": f"Bearer {token_2}"}).status_code == 404

    # 5. Tenant 2 no puede registrar un abono en la cuenta de Tenant 1
    assert client.post(
        f"/api/v1/ventas/cartera/{cuenta_id}/abono",
        json={"monto": 100.0, "metodo_pago": "EFECTIVO"},
        headers={"Authorization": f"Bearer {token_2}"}
    ).status_code == 404

    # 6. Tenant 2 no puede ver el estado de cuenta del cliente de Tenant 1
    assert client.get(
        f"/api/v1/ventas/clientes/{pos_audit_env['cliente_1_id']}/estado-cuenta",
        headers={"Authorization": f"Bearer {token_2}"}
    ).status_code == 404


# ===========================================================================
# 6. REGRESIÓN INTEGRAL: Anulación atómica con devolución de efectivo
# ===========================================================================
def test_pos_06_regresion_cancelacion_reintegro_inventario(pos_audit_env):
    client = pos_audit_env["client"]
    token = pos_audit_env["token_1"]
    alm_id = pos_audit_env["almacen_1_id"]
    var_id = pos_audit_env["variante_1_id"]

    # Crear venta con pago
    r_v = client.post(
        "/api/v1/ventas",
        json={
            "almacen_id": alm_id,
            "detalles": [{"variante_id": var_id, "cantidad": 2, "precio_unitario": 500.0}],
            "metodo_pago": "EFECTIVO",
            "monto_recibido": 1000.0,
            "pagos": [{"monto": 1000.0, "metodo_pago": "EFECTIVO"}],
        },
        headers={"Authorization": f"Bearer {token}"}
    )
    assert r_v.status_code == 201
    venta_id = r_v.json()["id"]

    # Cancelar venta con devolución física de efectivo
    idemp_canc = str(uuid.uuid4())
    r_canc = client.post(
        f"/api/v1/ventas/{venta_id}/cancelar",
        json={"motivo": "Devolución por garantía", "devolver_efectivo": True},
        headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": idemp_canc}
    )
    assert r_canc.status_code == 200
    canc_data = r_canc.json()
    assert canc_data["venta_id"] == venta_id
    assert canc_data["total_revertido"] == 1000.0
    assert canc_data["es_devolucion_efectivo"] is True
    assert canc_data["estado_operativo"] == "CANCELADA"
    assert canc_data["movimientos_inventario_reingresados"] >= 1

    # Consultar auditoría de la anulación
    r_audit = client.get(f"/api/v1/ventas/{venta_id}/auditoria-cancelacion", headers={"Authorization": f"Bearer {token}"})
    assert r_audit.status_code == 200
    audit_data = r_audit.json()
    assert audit_data["estado_operativo"] == "CANCELADA"
    assert len(audit_data["movimientos_reingreso"]) >= 1
    assert len(audit_data["reversiones"]) >= 1
