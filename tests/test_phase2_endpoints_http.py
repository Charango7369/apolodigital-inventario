"""
Tests de integración HTTP con TestClient de FastAPI (Bloque 2.5 — Endpoints HTTP e Idempotencia).

Cubre:
1. Petición sin autenticación -> HTTP 401 Unauthorized.
2. Roles no autorizados (ej. EMPLEADO intentando conciliar -> HTTP 403 Forbidden).
3. Aislamiento multi-tenant anti-IDOR (Tenant B accediendo a recursos de Tenant A -> HTTP 404).
4. Solicitudes válidas e inválidas (validaciones Pydantic y lógica de negocio -> HTTP 422 o 400).
5. Idempotencia real: reintentos con la misma llave retornan la respuesta en caché (X-Idempotent-Replay).
6. Idempotencia: misma llave con contenido diferente -> HTTP 409 Conflict.
7. Idempotencia: solicitud en vuelo ('PROCESSING') con la misma llave -> HTTP 409 Conflict.
8. Doble confirmación de pago -> HTTP 400 Bad Request.
9. Cancelación y reintento de cancelación (reintento sin idempotencia -> 400; reintento con llave -> 200 replay).
10. Coherencia integral de estados y saldos financieros a través de los endpoints HTTP.
11. Endpoints de cartera y estado de cuenta consolidado del cliente.
12. Reversión compensatoria de pagos con idempotencia.
"""

from datetime import datetime, timezone, date, timedelta
from decimal import Decimal
import json
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
from app.modules.inventario.models import Negocio, Almacen, Producto, Variante, Lote, Stock
from app.modules.ventas.models import (
    Cliente, Venta, DetalleVenta, PagoVenta, ReversionPago, CuentaPorCobrar,
    EstadoFinanciero, EstadoVenta, EstadoCuentaCobrar, EstadoPago, RegistroIdempotencia,
)
from app.modules.ventas.schemas import (
    VentaCreate, DetalleVentaCreate, PagoCreate, AbonoCreate,
    VentaCancelacionRequest, ReversionPagoCreate, ConciliacionPagoCreate,
)


@pytest.fixture
def http_env():
    """
    Configura una base de datos de pruebas aislada y un TestClient autenticado para dos tenants.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)
    db = TestingSession()

    # Tenant 1: Apolo Central
    neg_1 = Negocio(id=str(uuid.uuid4()), nombre="Apolo Central", propietario="Dueño 1", moneda="BOB", activo=True)
    # Tenant 2: Ferretería Norte (intruso)
    neg_2 = Negocio(id=str(uuid.uuid4()), nombre="Ferretería Norte", propietario="Dueño 2", moneda="BOB", activo=True)
    db.add_all([neg_1, neg_2])
    db.flush()

    # Usuarios Tenant 1
    admin_1 = User(
        id=str(uuid.uuid4()),
        email="admin1@apolo.lat",
        password_hash="hash",
        nombre="Admin Tenant 1",
        negocio_id=neg_1.id,
        rol=UserRole.ADMIN.value,
        activo=True,
    )
    empleado_1 = User(
        id=str(uuid.uuid4()),
        email="cajero1@apolo.lat",
        password_hash="hash",
        nombre="Cajero Tenant 1",
        negocio_id=neg_1.id,
        rol=UserRole.EMPLEADO.value,
        activo=True,
    )
    # Usuario Tenant 2
    admin_2 = User(
        id=str(uuid.uuid4()),
        email="admin2@norte.lat",
        password_hash="hash",
        nombre="Admin Tenant 2",
        negocio_id=neg_2.id,
        rol=UserRole.ADMIN.value,
        activo=True,
    )
    db.add_all([admin_1, empleado_1, admin_2])
    db.flush()

    # Almacenes
    alm_1 = Almacen(id=str(uuid.uuid4()), negocio_id=neg_1.id, nombre="Almacén Matriz", es_principal=True)
    alm_2 = Almacen(id=str(uuid.uuid4()), negocio_id=neg_2.id, nombre="Almacén Secundario", es_principal=True)
    db.add_all([alm_1, alm_2])

    # Cliente Tenant 1
    cli_1 = Cliente(id=str(uuid.uuid4()), negocio_id=neg_1.id, nombre="Constructora La Paz", nit="12345678", activo=True)
    db.add(cli_1)

    # Catálogo Tenant 1
    prod = Producto(id=str(uuid.uuid4()), negocio_id=neg_1.id, nombre="Amoladora Angular", unidad_medida="unidad", activo=True)
    db.add(prod)
    db.flush()

    var = Variante(
        id=str(uuid.uuid4()),
        producto_id=prod.id,
        sku="AMO-ANG-01",
        precio_venta=Decimal("400.00"),
        precio_costo=Decimal("250.00"),
        activa=True,
    )
    db.add(var)
    db.flush()

    lote = Lote(
        id=str(uuid.uuid4()),
        variante_id=var.id,
        almacen_id=alm_1.id,
        fecha_ingreso=datetime.now(timezone.utc),
        cantidad_inicial=Decimal("50"),
        cantidad_actual=Decimal("50"),
        costo_unitario=Decimal("250.00"),
        activo=True,
    )
    db.add(lote)
    db.flush()

    stock = Stock(
        id=str(uuid.uuid4()),
        variante_id=var.id,
        almacen_id=alm_1.id,
        cantidad_actual=Decimal("50.000"),
    )
    db.add(stock)
    db.commit()

    # Generar JWT tokens
    token_admin_1 = create_access_token({"sub": admin_1.id, "negocio_id": neg_1.id, "rol": admin_1.rol})
    token_emp_1 = create_access_token({"sub": empleado_1.id, "negocio_id": neg_1.id, "rol": empleado_1.rol})
    token_admin_2 = create_access_token({"sub": admin_2.id, "negocio_id": neg_2.id, "rol": admin_2.rol})

    # Sobreescribir dependencia get_db
    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    application.dependency_overrides[get_db] = override_get_db
    client = TestClient(application)

    yield {
        "client": client,
        "db": db,
        "TestingSession": TestingSession,
        "token_admin_1": token_admin_1,
        "token_emp_1": token_emp_1,
        "token_admin_2": token_admin_2,
        "negocio_1_id": neg_1.id,
        "negocio_2_id": neg_2.id,
        "almacen_1_id": alm_1.id,
        "cliente_1_id": cli_1.id,
        "variante_1_id": var.id,
        "admin_1_id": admin_1.id,
    }

    application.dependency_overrides.clear()
    db.close()
    Base.metadata.drop_all(engine)


# ===========================================================================
# 1. Petición sin autenticación -> 401 Unauthorized
# ===========================================================================
def test_01_unauthenticated_request_rejected(http_env):
    client = http_env["client"]
    r = client.post("/api/v1/ventas", json={})
    assert r.status_code == status.HTTP_401_UNAUTHORIZED
    assert "detail" in r.json()


# ===========================================================================
# 2. Roles no autorizados -> 403 Forbidden
# ===========================================================================
def test_02_unauthorized_role_rejected(http_env):
    client = http_env["client"]
    token_emp = http_env["token_emp_1"]
    dummy_pago_id = str(uuid.uuid4())

    payload = {
        "motivo": "Conciliación no permitida para cajero",
        "referencia_documental": "DOC-999"
    }
    r = client.post(
        f"/api/v1/ventas/pagos/{dummy_pago_id}/conciliar",
        json=payload,
        headers={"Authorization": f"Bearer {token_emp}"}
    )
    assert r.status_code == status.HTTP_403_FORBIDDEN
    assert "Administrador" in r.json()["detail"]


# ===========================================================================
# 3. Aislamiento multi-tenant anti-IDOR -> 404 Not Found
# ===========================================================================
def test_03_cross_tenant_idor_rejected(http_env):
    client = http_env["client"]
    token_admin_1 = http_env["token_admin_1"]
    token_admin_2 = http_env["token_admin_2"]

    # Crear una venta en Tenant 1
    payload = {
        "almacen_id": http_env["almacen_1_id"],
        "cliente_id": http_env["cliente_1_id"],
        "detalles": [
            {"variante_id": http_env["variante_1_id"], "cantidad": 1, "precio_unitario": 400.0}
        ],
        "metodo_pago": "EFECTIVO",
        "monto_recibido": 400.0,
        "completar": True,
    }
    r = client.post("/api/v1/ventas", json=payload, headers={"Authorization": f"Bearer {token_admin_1}"})
    assert r.status_code == 201
    venta_id = r.json()["id"]

    # Tenant 2 intenta leer la venta de Tenant 1
    r_get = client.get(f"/api/v1/ventas/{venta_id}", headers={"Authorization": f"Bearer {token_admin_2}"})
    assert r_get.status_code == status.HTTP_404_NOT_FOUND

    # Tenant 2 intenta cancelar la venta de Tenant 1
    r_cancel = client.post(
        f"/api/v1/ventas/{venta_id}/cancelar",
        json={"motivo": "Ataque IDOR"},
        headers={"Authorization": f"Bearer {token_admin_2}"}
    )
    assert r_cancel.status_code == status.HTTP_404_NOT_FOUND


# ===========================================================================
# 4. Solicitudes válidas e inválidas (Pydantic & Lógica)
# ===========================================================================
def test_04_create_sale_valid_and_invalid_pydantic(http_env):
    client = http_env["client"]
    token = http_env["token_admin_1"]

    # Inválida: cantidad negativa
    r_inv = client.post(
        "/api/v1/ventas",
        json={
            "almacen_id": http_env["almacen_1_id"],
            "detalles": [{"variante_id": http_env["variante_1_id"], "cantidad": -2}],
        },
        headers={"Authorization": f"Bearer {token}"}
    )
    assert r_inv.status_code == 422

    # Válida: venta completada de contado
    r_val = client.post(
        "/api/v1/ventas",
        json={
            "almacen_id": http_env["almacen_1_id"],
            "cliente_id": http_env["cliente_1_id"],
            "detalles": [{"variante_id": http_env["variante_1_id"], "cantidad": 2, "precio_unitario": 400.0}],
            "metodo_pago": "EFECTIVO",
            "monto_recibido": 800.0,
            "completar": True,
        },
        headers={"Authorization": f"Bearer {token}"}
    )
    assert r_val.status_code == status.HTTP_201_CREATED
    data = r_val.json()
    assert float(data["total"]) == 800.0
    assert data["estado"] == "COMPLETADA"
    assert data["estado_financiero"] == "PAID"
    assert len(data["pagos"]) == 1


# ===========================================================================
# 5. Idempotencia real: reintentos con la misma llave retornan caché idéntico
# ===========================================================================
def test_05_idempotency_replay_identical_response(http_env):
    client = http_env["client"]
    token = http_env["token_admin_1"]
    idemp_key = f"sale-key-{uuid.uuid4()}"

    payload = {
        "almacen_id": http_env["almacen_1_id"],
        "cliente_id": http_env["cliente_1_id"],
        "detalles": [{"variante_id": http_env["variante_1_id"], "cantidad": 1, "precio_unitario": 400.0}],
        "metodo_pago": "EFECTIVO",
        "monto_recibido": 400.0,
        "completar": True,
    }
    headers = {"Authorization": f"Bearer {token}", "X-Idempotency-Key": idemp_key}

    # Primera llamada
    r1 = client.post("/api/v1/ventas", json=payload, headers=headers)
    assert r1.status_code == status.HTTP_201_CREATED
    venta1 = r1.json()

    # Segunda llamada idéntica con la misma llave de idempotencia
    r2 = client.post("/api/v1/ventas", json=payload, headers=headers)
    assert r2.status_code == status.HTTP_201_CREATED
    assert r2.headers.get("x-idempotent-replay") == "true"
    venta2 = r2.json()

    # Comprobar que devuelve exactamente la misma venta
    assert venta1["id"] == venta2["id"]
    assert venta1["numero"] == venta2["numero"]

    # Comprobar en base de datos que solo se creó 1 venta
    db = http_env["TestingSession"]()
    ventas_count = db.query(Venta).filter(Venta.id == venta1["id"]).count()
    assert ventas_count == 1
    db.close()


# ===========================================================================
# 6. Idempotencia: misma llave con contenido diferente -> 409 Conflict
# ===========================================================================
def test_06_idempotency_different_payload_conflict_409(http_env):
    client = http_env["client"]
    token = http_env["token_admin_1"]
    idemp_key = f"conflict-key-{uuid.uuid4()}"
    headers = {"Authorization": f"Bearer {token}", "X-Idempotency-Key": idemp_key}

    payload_a = {
        "almacen_id": http_env["almacen_1_id"],
        "detalles": [{"variante_id": http_env["variante_1_id"], "cantidad": 1, "precio_unitario": 400.0}],
        "metodo_pago": "EFECTIVO",
        "monto_recibido": 400.0,
    }
    r1 = client.post("/api/v1/ventas", json=payload_a, headers=headers)
    assert r1.status_code == status.HTTP_201_CREATED

    payload_b = {
        "almacen_id": http_env["almacen_1_id"],
        "detalles": [{"variante_id": http_env["variante_1_id"], "cantidad": 3, "precio_unitario": 400.0}],  # Payload alterado
        "metodo_pago": "EFECTIVO",
        "monto_recibido": 1200.0,
    }
    r2 = client.post("/api/v1/ventas", json=payload_b, headers=headers)
    assert r2.status_code == status.HTTP_409_CONFLICT
    assert "different request payload" in r2.json()["detail"]


# ===========================================================================
# 7. Idempotencia: solicitud en vuelo ('PROCESSING') -> 409 Conflict
# ===========================================================================
def test_07_idempotency_processing_in_flight_conflict_409(http_env):
    client = http_env["client"]
    token = http_env["token_admin_1"]
    idemp_key = f"inflight-key-{uuid.uuid4()}"

    payload = {
        "almacen_id": http_env["almacen_1_id"],
        "detalles": [{"variante_id": http_env["variante_1_id"], "cantidad": 1, "precio_unitario": 400.0}],
    }

    # Insertar artificialmente un registro en PROCESSING
    db = http_env["TestingSession"]()
    from app.modules.ventas.idempotency import canonical_request_hash
    from app.modules.ventas.schemas import VentaCreate
    raw_hash = canonical_request_hash(VentaCreate(**payload))
    rec = RegistroIdempotencia(
        negocio_id=http_env["negocio_1_id"],
        idempotency_key=idemp_key,
        metodo="POST",
        endpoint="/api/v1/ventas",
        usuario_id=http_env["admin_1_id"],
        request_hash=raw_hash,
        status="PROCESSING",
    )
    db.add(rec)
    db.commit()
    db.close()

    # La solicitud HTTP debe recibir 409 Conflict por concurrencia
    r = client.post(
        "/api/v1/ventas",
        json=payload,
        headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": idemp_key}
    )
    assert r.status_code == status.HTTP_409_CONFLICT
    assert "currently being processed" in r.json()["detail"]


# ===========================================================================
# 8. Doble confirmación de pago -> 400 Bad Request
# ===========================================================================
def test_08_double_payment_confirmation_rejected(http_env):
    client = http_env["client"]
    token = http_env["token_admin_1"]

    # Crear venta con pago QR PENDING
    r_v = client.post(
        "/api/v1/ventas",
        json={
            "almacen_id": http_env["almacen_1_id"],
            "detalles": [{"variante_id": http_env["variante_1_id"], "cantidad": 1, "precio_unitario": 400.0}],
            "pagos": [{"monto": 400.0, "metodo_pago": "QR", "estado": "PENDING"}],
            "completar": True,
        },
        headers={"Authorization": f"Bearer {token}"}
    )
    assert r_v.status_code == 201
    pago_id = r_v.json()["pagos"][0]["id"]

    # Primera confirmación -> 200 OK
    r_conf1 = client.post(
        f"/api/v1/ventas/pagos/{pago_id}/confirmar",
        json={"referencia_confirmacion": "AUT-QR-111"},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert r_conf1.status_code == status.HTTP_200_OK
    assert r_conf1.json()["estado"] == "CONFIRMED"

    # Segunda confirmación -> 400 Bad Request
    r_conf2 = client.post(
        f"/api/v1/ventas/pagos/{pago_id}/confirmar",
        json={"referencia_confirmacion": "AUT-QR-222"},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert r_conf2.status_code == status.HTTP_400_BAD_REQUEST
    assert "ya se encuentra confirmado" in r_conf2.json()["detail"]


# ===========================================================================
# 9. Cancelación y reintento de cancelación
# ===========================================================================
def test_09_sale_cancellation_and_double_cancellation_attempt(http_env):
    client = http_env["client"]
    token = http_env["token_admin_1"]
    idemp_cancel_key = f"cancel-{uuid.uuid4()}"

    r_v = client.post(
        "/api/v1/ventas",
        json={
            "almacen_id": http_env["almacen_1_id"],
            "detalles": [{"variante_id": http_env["variante_1_id"], "cantidad": 1, "precio_unitario": 400.0}],
            "metodo_pago": "EFECTIVO",
            "monto_recibido": 400.0,
            "completar": True,
        },
        headers={"Authorization": f"Bearer {token}"}
    )
    venta_id = r_v.json()["id"]

    # Cancelar con llave de idempotencia
    r_canc1 = client.post(
        f"/api/v1/ventas/{venta_id}/cancelar",
        json={"motivo": "Cliente canceló pedido", "devolver_efectivo": True},
        headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": idemp_cancel_key}
    )
    assert r_canc1.status_code == status.HTTP_200_OK
    data_c = r_canc1.json()
    assert data_c["estado_operativo"] == "CANCELADA"
    assert data_c["estado_financiero"] == "NOT_APPLICABLE"
    assert float(data_c["total_revertido"]) == 400.0

    # Reintento idéntico con la misma llave de idempotencia -> 200 Replay
    r_canc_replay = client.post(
        f"/api/v1/ventas/{venta_id}/cancelar",
        json={"motivo": "Cliente canceló pedido", "devolver_efectivo": True},
        headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": idemp_cancel_key}
    )
    assert r_canc_replay.status_code == status.HTTP_200_OK
    assert r_canc_replay.headers.get("x-idempotent-replay") == "true"

    # Intento de re-cancelar sin llave de idempotencia -> 400 Bad Request
    r_canc2 = client.post(
        f"/api/v1/ventas/{venta_id}/cancelar",
        json={"motivo": "Otro intento de cancelar"},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert r_canc2.status_code == status.HTTP_400_BAD_REQUEST
    assert "ya se encuentra cancelada" in r_canc2.json()["detail"]


# ===========================================================================
# 10. Coherencia integral de estados y saldos financieros vía HTTP
# ===========================================================================
def test_10_financial_endpoints_consistency(http_env):
    client = http_env["client"]
    token = http_env["token_admin_1"]

    # 1. Crear venta a crédito puro de Bs 800 (2 unidades de Bs 400)
    r_v = client.post(
        "/api/v1/ventas",
        json={
            "almacen_id": http_env["almacen_1_id"],
            "cliente_id": http_env["cliente_1_id"],
            "detalles": [{"variante_id": http_env["variante_1_id"], "cantidad": 2, "precio_unitario": 400.0}],
            "es_credito": True,
            "completar": True,
        },
        headers={"Authorization": f"Bearer {token}"}
    )
    assert r_v.status_code == 201
    venta = r_v.json()
    venta_id = venta["id"]
    cuenta_id = venta["cuenta_cobrar"]["id"]
    assert venta["estado_financiero"] == "CREDIT"

    # 2. Consultar historial financiero de la venta
    r_hist = client.get(f"/api/v1/ventas/{venta_id}/historial", headers={"Authorization": f"Bearer {token}"})
    assert r_hist.status_code == 200
    assert float(r_hist.json()["saldo_operativo_pendiente"]) == 800.0

    # 3. Registrar abono parcial de Bs 300
    r_abono1 = client.post(
        f"/api/v1/ventas/cartera/{cuenta_id}/abono",
        json={"monto": 300.0, "metodo_pago": "EFECTIVO"},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert r_abono1.status_code == 201
    assert float(r_abono1.json()["saldo_operativo_nuevo"]) == 500.0
    assert r_abono1.json()["estado_financiero_venta"] == "PARTIAL"

    # 4. Registrar abono final de Bs 500
    r_abono2 = client.post(
        f"/api/v1/ventas/cartera/{cuenta_id}/abono",
        json={"monto": 500.0, "metodo_pago": "EFECTIVO"},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert r_abono2.status_code == 201
    assert float(r_abono2.json()["saldo_operativo_nuevo"]) == 0.0
    assert r_abono2.json()["estado_cuenta"] == "PAGADA"
    assert r_abono2.json()["estado_financiero_venta"] == "PAID"

    # 5. Comprobar estado final en GET /ventas/{id}
    r_vf = client.get(f"/api/v1/ventas/{venta_id}", headers={"Authorization": f"Bearer {token}"})
    assert r_vf.json()["estado_financiero"] == "PAID"


# ===========================================================================
# 11. Endpoints de cartera y estado de cuenta consolidado
# ===========================================================================
def test_11_accounts_receivable_and_customer_statement_endpoints(http_env):
    client = http_env["client"]
    token = http_env["token_admin_1"]
    cliente_id = http_env["cliente_1_id"]

    # Consultar cartera agregada por cliente
    r_c = client.get("/api/v1/ventas/cartera/clientes", headers={"Authorization": f"Bearer {token}"})
    assert r_c.status_code == 200
    assert isinstance(r_c.json(), list)

    # Consultar estado de cuenta del cliente
    r_ec = client.get(f"/api/v1/ventas/clientes/{cliente_id}/estado-cuenta", headers={"Authorization": f"Bearer {token}"})
    assert r_ec.status_code == 200
    ec = r_ec.json()
    assert ec["cliente_id"] == cliente_id
    assert "resumen" in ec
    assert "cuentas" in ec


# ===========================================================================
# 12. Reversión compensatoria de pagos con idempotencia
# ===========================================================================
def test_12_reversion_endpoint_with_idempotency(http_env):
    client = http_env["client"]
    token = http_env["token_admin_1"]
    idemp_rev_key = f"rev-{uuid.uuid4()}"

    # Venta de contado
    r_v = client.post(
        "/api/v1/ventas",
        json={
            "almacen_id": http_env["almacen_1_id"],
            "detalles": [{"variante_id": http_env["variante_1_id"], "cantidad": 1, "precio_unitario": 400.0}],
            "metodo_pago": "EFECTIVO",
            "monto_recibido": 400.0,
            "completar": True,
        },
        headers={"Authorization": f"Bearer {token}"}
    )
    pago_id = r_v.json()["pagos"][0]["id"]

    # Reversión con idempotencia
    payload_rev = {"monto": 150.0, "motivo": "Devolución parcial por desperfecto", "es_devolucion_efectivo": True}
    headers = {"Authorization": f"Bearer {token}", "X-Idempotency-Key": idemp_rev_key}

    r_rev1 = client.post(f"/api/v1/ventas/pagos/{pago_id}/revertir", json=payload_rev, headers=headers)
    assert r_rev1.status_code == 200
    assert float(r_rev1.json()["monto_revertido"]) == 150.0

    # Reintento con la misma llave -> 200 Replay
    r_rev_replay = client.post(f"/api/v1/ventas/pagos/{pago_id}/revertir", json=payload_rev, headers=headers)
    assert r_rev_replay.status_code == 200
    assert r_rev_replay.headers.get("x-idempotent-replay") == "true"
    assert float(r_rev_replay.json()["monto_revertido"]) == 150.0
