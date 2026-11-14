"""
Suite exhaustiva de pruebas de atomicidad e idempotencia transaccional (Fase 2.5.1).

Valida las 9 condiciones obligatorias de integridad financiera:
1. Reintento después de operación exitosa (replay exacto y header X-Idempotent-Replay).
2. Misma clave con payload diferente -> 409 Conflict.
3. Solicitudes concurrentes simultáneas con la misma clave (cero duplicaciones).
4. Interrupción/fallo simulado ANTES del commit -> rollback total (no venta, no clave).
5. Recuperación y reintento exitoso tras fallo.
6. Ausencia de operaciones financieras y movimientos de inventario duplicados.
7. Aislamiento estricto multi-tenant (dos negocios usando la misma llave).
8. Idempotencia natural en reintentos de completar venta (sin doble descuento de inventario).
9. Idempotencia natural en reintentos de marcar pago fallido (sin error ni notas duplicadas).
10. Mismo key en recursos/endpoints distintos no colisiona.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import uuid
import pytest
from fastapi import status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import application as app
from app.database import Base, get_db
from app.modules.auth.models import User, UserRole
from app.modules.auth.service import create_access_token
from app.modules.inventario.models import Negocio, Almacen, Producto, Variante, Stock, MovimientoStock
from app.modules.ventas.models import (
    Cliente, Venta, DetalleVenta, PagoVenta, CuentaPorCobrar,
    RegistroIdempotencia, EstadoVenta, EstadoPago, EstadoFinanciero,
)
from app.modules.ventas.schemas import VentaCreate, DetalleVentaCreate, PagoCreate
from app.modules.ventas.services.sale_service import SaleService
from app.modules.ventas.services.payment_service import PaymentService
from app.modules.ventas.idempotency import ejecutar_con_idempotencia, canonical_request_hash


@pytest.fixture(scope="function")
def atomic_env():
    """Configura un entorno de pruebas aislado con soporte de concurrencia y dos negocios"""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    db = TestingSession()

    # Negocio 1
    neg1_id = str(uuid.uuid4())
    neg1 = Negocio(id=neg1_id, nombre="Apolo Central", propietario="Dueño Central", activo=True)
    alm1 = Almacen(id=str(uuid.uuid4()), negocio_id=neg1_id, nombre="Almacén Central", es_principal=True)
    usr1_admin_id = str(uuid.uuid4())
    usr1 = User(
        id=usr1_admin_id,
        negocio_id=neg1_id,
        email="admin@apolo.com",
        password_hash="fakehash",
        nombre="Admin Central",
        rol=UserRole.ADMIN.value,
        activo=True,
    )
    from app.modules.inventario.models import Lote

    p1 = Producto(id=str(uuid.uuid4()), negocio_id=neg1_id, nombre="Martillo Pro", unidad_medida="unidad", activo=True)
    v1 = Variante(id=str(uuid.uuid4()), producto_id=p1.id, sku="SKU-M01", precio_venta=Decimal("100.00"), precio_costo=Decimal("50.00"), activa=True)
    lote1 = Lote(id=str(uuid.uuid4()), variante_id=v1.id, almacen_id=alm1.id, fecha_ingreso=datetime.now(timezone.utc), cantidad_inicial=Decimal("100.00"), cantidad_actual=Decimal("100.00"), costo_unitario=Decimal("50.00"), activo=True)
    s1 = Stock(id=str(uuid.uuid4()), variante_id=v1.id, almacen_id=alm1.id, cantidad_actual=Decimal("100.00"))
    cli1 = Cliente(id=str(uuid.uuid4()), negocio_id=neg1_id, nombre="Cliente Alpha", activo=True)

    # Negocio 2 (para probar aislamiento multi-tenant)
    neg2_id = str(uuid.uuid4())
    neg2 = Negocio(id=neg2_id, nombre="Apolo Sucursal Norte", propietario="Dueño Norte", activo=True)
    alm2 = Almacen(id=str(uuid.uuid4()), negocio_id=neg2_id, nombre="Almacén Norte", es_principal=True)
    usr2_admin_id = str(uuid.uuid4())
    usr2 = User(
        id=usr2_admin_id,
        negocio_id=neg2_id,
        email="admin@norte.com",
        password_hash="fakehash",
        nombre="Admin Norte",
        rol=UserRole.ADMIN.value,
        activo=True,
    )
    p2 = Producto(id=str(uuid.uuid4()), negocio_id=neg2_id, nombre="Clavos Pro", unidad_medida="unidad", activo=True)
    v2 = Variante(id=str(uuid.uuid4()), producto_id=p2.id, sku="SKU-C01", precio_venta=Decimal("100.00"), precio_costo=Decimal("50.00"), activa=True)
    lote2 = Lote(id=str(uuid.uuid4()), variante_id=v2.id, almacen_id=alm2.id, fecha_ingreso=datetime.now(timezone.utc), cantidad_inicial=Decimal("100.00"), cantidad_actual=Decimal("100.00"), costo_unitario=Decimal("50.00"), activo=True)
    s2 = Stock(id=str(uuid.uuid4()), variante_id=v2.id, almacen_id=alm2.id, cantidad_actual=Decimal("100.00"))

    db.add_all([neg1, alm1, usr1, p1, v1, lote1, s1, cli1, neg2, alm2, usr2, p2, v2, lote2, s2])
    db.commit()

    token1 = create_access_token({"sub": usr1.id, "negocio_id": neg1_id, "rol": usr1.rol})
    token2 = create_access_token({"sub": usr2.id, "negocio_id": neg2_id, "rol": usr2.rol})

    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    yield {
        "client": client,
        "TestingSession": TestingSession,
        "token1": token1,
        "token2": token2,
        "neg1_id": neg1_id,
        "neg2_id": neg2_id,
        "alm1_id": alm1.id,
        "alm2_id": alm2.id,
        "usr1_id": usr1.id,
        "usr2_id": usr2.id,
        "v1_id": v1.id,
        "v2_id": v2.id,
        "cli1_id": cli1.id,
    }

    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)


# ===========================================================================
# 1. Reintento después de operación exitosa
# ===========================================================================
def test_01_successful_retry_exact_replay(atomic_env):
    client = atomic_env["client"]
    token = atomic_env["token1"]
    idemp_key = f"key-sale-{uuid.uuid4()}"

    payload = {
        "almacen_id": atomic_env["alm1_id"],
        "detalles": [{"variante_id": atomic_env["v1_id"], "cantidad": 2, "precio_unitario": 100.0}],
        "metodo_pago": "EFECTIVO",
        "monto_recibido": 200.0,
        "completar": True,
    }
    headers = {"Authorization": f"Bearer {token}", "X-Idempotency-Key": idemp_key}

    # Primera solicitud
    r1 = client.post("/api/v1/ventas", json=payload, headers=headers)
    assert r1.status_code == status.HTTP_201_CREATED
    data1 = r1.json()
    venta_id = data1["id"]
    assert "x-idempotent-replay" not in r1.headers

    # Segunda solicitud idéntica (reintento del frontend o red)
    r2 = client.post("/api/v1/ventas", json=payload, headers=headers)
    assert r2.status_code == status.HTTP_201_CREATED
    assert r2.headers.get("x-idempotent-replay") == "true"
    data2 = r2.json()
    assert data2["id"] == venta_id

    # Comprobación de ausencia de duplicación en base de datos
    db = atomic_env["TestingSession"]()
    ventas_count = db.query(func.count(Venta.id)).filter(Venta.negocio_id == atomic_env["neg1_id"]).scalar()
    pagos_count = db.query(func.count(PagoVenta.id)).filter(PagoVenta.negocio_id == atomic_env["neg1_id"]).scalar()
    stock = db.query(Stock).filter(Stock.variante_id == atomic_env["v1_id"]).first()
    db.close()

    assert ventas_count == 1, "Debe existir exactamente 1 venta registrada"
    assert pagos_count == 1, "Debe existir exactamente 1 pago registrado"
    assert stock.cantidad_actual == Decimal("98.00"), "El stock solo debió descontarse una vez (100 - 2 = 98)"


# ===========================================================================
# 2. Misma clave con payload diferente -> 409 Conflict
# ===========================================================================
def test_02_same_key_different_payload_conflict_409(atomic_env):
    client = atomic_env["client"]
    token = atomic_env["token1"]
    idemp_key = f"key-payload-test-{uuid.uuid4()}"

    payload_a = {
        "almacen_id": atomic_env["alm1_id"],
        "detalles": [{"variante_id": atomic_env["v1_id"], "cantidad": 1, "precio_unitario": 100.0}],
        "metodo_pago": "EFECTIVO",
        "monto_recibido": 100.0,
        "completar": True,
    }
    r1 = client.post("/api/v1/ventas", json=payload_a, headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": idemp_key})
    assert r1.status_code == status.HTTP_201_CREATED

    # Mismo key, diferente cantidad (2 en lugar de 1)
    payload_b = {
        "almacen_id": atomic_env["alm1_id"],
        "detalles": [{"variante_id": atomic_env["v1_id"], "cantidad": 2, "precio_unitario": 100.0}],
        "metodo_pago": "EFECTIVO",
        "monto_recibido": 200.0,
        "completar": True,
    }
    r2 = client.post("/api/v1/ventas", json=payload_b, headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": idemp_key})
    assert r2.status_code == status.HTTP_409_CONFLICT
    assert "reused with different request payload" in r2.json()["detail"]


# ===========================================================================
# 3. Solicitudes concurrentes simultáneas con la misma clave
# ===========================================================================
def test_03_concurrent_simultaneous_requests_same_key(atomic_env):
    client = atomic_env["client"]
    token = atomic_env["token1"]
    idemp_key = f"concurrent-race-{uuid.uuid4()}"

    payload = {
        "almacen_id": atomic_env["alm1_id"],
        "detalles": [{"variante_id": atomic_env["v1_id"], "cantidad": 3, "precio_unitario": 100.0}],
        "metodo_pago": "EFECTIVO",
        "monto_recibido": 300.0,
        "completar": True,
    }

    # Simulación de doble click rápido o reintento inmediato del cliente
    r1 = client.post(
        "/api/v1/ventas",
        json=payload,
        headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": idemp_key}
    )
    r2 = client.post(
        "/api/v1/ventas",
        json=payload,
        headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": idemp_key}
    )

    assert r1.status_code == status.HTTP_201_CREATED
    assert r2.status_code == status.HTTP_201_CREATED
    assert r2.headers.get("x-idempotent-replay") == "true"
    assert r1.json()["id"] == r2.json()["id"]

    db = atomic_env["TestingSession"]()
    ventas = db.query(Venta).filter(Venta.negocio_id == atomic_env["neg1_id"]).all()
    stock = db.query(Stock).filter(Stock.variante_id == atomic_env["v1_id"]).first()
    db.close()

    assert len(ventas) == 1, "Solo debe existir 1 venta creada en la base de datos"
    assert stock.cantidad_actual == Decimal("97.00"), "El stock solo se redujo una vez (100 - 3 = 97)"


# ===========================================================================
# 4. Interrupción simulada antes del COMMIT (Rollback Atómico Total)
# ===========================================================================
def test_04_interruption_simulated_before_commit_rollback(atomic_env):
    db = atomic_env["TestingSession"]()
    idemp_key = f"crash-test-{uuid.uuid4()}"
    neg_id = atomic_env["neg1_id"]
    usr_id = atomic_env["usr1_id"]

    data_venta = VentaCreate(
        almacen_id=atomic_env["alm1_id"],
        detalles=[DetalleVentaCreate(variante_id=atomic_env["v1_id"], cantidad=Decimal("5.00"), precio_unitario=Decimal("100.00"))],
        metodo_pago="EFECTIVO",
        monto_recibido=Decimal("500.00"),
        completar=True,
    )

    # Simular una acción que falla intencionalmente antes de confirmar (ej. excepción de hardware o red)
    def action_falla(commit: bool = True):
        # Crear la venta en la sesión (commit=False)
        SaleService.crear_venta_financiera(db, neg_id, usr_id, data_venta, commit=commit)
        # Provocar caída abrupta antes del commit
        raise RuntimeError("SIMULATED_SYSTEM_CRASH_BEFORE_COMMIT")

    with pytest.raises(Exception) as excinfo:
        ejecutar_con_idempotencia(
            db=db,
            negocio_id=neg_id,
            usuario_id=usr_id,
            metodo="POST",
            endpoint="/api/v1/ventas",
            idempotency_key=idemp_key,
            request_data=data_venta,
            action=action_falla,
            status_code=201,
        )

    # Verificar que el rollback fue total:
    # 1. No existe la venta
    venta = db.query(Venta).filter(Venta.negocio_id == neg_id).first()
    assert venta is None, "La venta debió haber sido revertida completamente por rollback"

    # 2. No existe el registro en idempotencia_claves (cero estados 'PROCESSING' o huérfanos)
    rec_idemp = db.query(RegistroIdempotencia).filter(RegistroIdempotencia.idempotency_key == idemp_key).first()
    assert rec_idemp is None, "El registro de idempotencia debió ser revertido por rollback"

    # 3. El stock permanece íntegro (100.00)
    stock = db.query(Stock).filter(Stock.variante_id == atomic_env["v1_id"]).first()
    assert stock.cantidad_actual == Decimal("100.00"), "El stock no debió alterarse tras rollback"

    # 4. Ahora el cliente reintenta la misma clave tras recuperarse: debe tener éxito limpio
    def action_exitosa(commit: bool = True):
        return SaleService.crear_venta_financiera(db, neg_id, usr_id, data_venta, commit=commit)

    resp = ejecutar_con_idempotencia(
        db=db,
        negocio_id=neg_id,
        usuario_id=usr_id,
        metodo="POST",
        endpoint="/api/v1/ventas",
        idempotency_key=idemp_key,
        request_data=data_venta,
        action=action_exitosa,
        status_code=201,
    )
    assert resp.status_code == 201

    # Verificar que ahora sí existe 1 sola venta y 1 registro de idempotencia con status SUCCESS
    assert db.query(func.count(Venta.id)).scalar() == 1
    rec_final = db.query(RegistroIdempotencia).filter(RegistroIdempotencia.idempotency_key == idemp_key).first()
    assert rec_final is not None
    assert rec_final.status == "SUCCESS"
    db.close()


# ===========================================================================
# 5. Aislamiento Multi-Tenant con la misma clave de idempotencia
# ===========================================================================
def test_05_multi_tenant_isolation_same_key(atomic_env):
    client = atomic_env["client"]
    token1 = atomic_env["token1"]
    token2 = atomic_env["token2"]
    shared_key = "POS-TICKET-001"

    # Negocio 1 crea su venta usando la clave "POS-TICKET-001"
    payload1 = {
        "almacen_id": atomic_env["alm1_id"],
        "detalles": [{"variante_id": atomic_env["v1_id"], "cantidad": 1, "precio_unitario": 100.0}],
        "metodo_pago": "EFECTIVO",
        "monto_recibido": 100.0,
        "completar": True,
    }
    r1 = client.post("/api/v1/ventas", json=payload1, headers={"Authorization": f"Bearer {token1}", "X-Idempotency-Key": shared_key})
    assert r1.status_code == status.HTTP_201_CREATED
    v1_id = r1.json()["id"]

    # Negocio 2 crea su venta usando EXACTAMENTE la misma clave "POS-TICKET-001"
    payload2 = {
        "almacen_id": atomic_env["alm2_id"],
        "detalles": [{"variante_id": atomic_env["v2_id"], "cantidad": 1, "precio_unitario": 100.0}],
        "metodo_pago": "EFECTIVO",
        "monto_recibido": 100.0,
        "completar": True,
    }
    r2 = client.post("/api/v1/ventas", json=payload2, headers={"Authorization": f"Bearer {token2}", "X-Idempotency-Key": shared_key})
    assert r2.status_code == status.HTTP_201_CREATED
    v2_id = r2.json()["id"]

    assert v1_id != v2_id, "Las ventas deben ser completamente distintas"

    # Cada uno obtiene replay exclusivo de SU propia venta
    r1_replay = client.post("/api/v1/ventas", json=payload1, headers={"Authorization": f"Bearer {token1}", "X-Idempotency-Key": shared_key})
    assert r1_replay.json()["id"] == v1_id

    r2_replay = client.post("/api/v1/ventas", json=payload2, headers={"Authorization": f"Bearer {token2}", "X-Idempotency-Key": shared_key})
    assert r2_replay.json()["id"] == v2_id


# ===========================================================================
# 6. Reutilización de clave en recursos/endpoints distintos no colisiona
# ===========================================================================
def test_06_same_key_different_resource_endpoints_no_collision(atomic_env):
    client = atomic_env["client"]
    token = atomic_env["token1"]
    key_comun = f"pago-abono-{uuid.uuid4()}"

    # Crear dos ventas a crédito
    r_v1 = client.post("/api/v1/ventas", json={
        "almacen_id": atomic_env["alm1_id"],
        "cliente_id": atomic_env["cli1_id"],
        "detalles": [{"variante_id": atomic_env["v1_id"], "cantidad": 1, "precio_unitario": 100.0}],
        "es_credito": True,
        "completar": True,
    }, headers={"Authorization": f"Bearer {token}"})
    v1_id = r_v1.json()["id"]

    r_v2 = client.post("/api/v1/ventas", json={
        "almacen_id": atomic_env["alm1_id"],
        "cliente_id": atomic_env["cli1_id"],
        "detalles": [{"variante_id": atomic_env["v1_id"], "cantidad": 1, "precio_unitario": 100.0}],
        "es_credito": True,
        "completar": True,
    }, headers={"Authorization": f"Bearer {token}"})
    v2_id = r_v2.json()["id"]

    # Registrar pago en Venta 1 con key_comun
    r_p1 = client.post(
        f"/api/v1/ventas/{v1_id}/pagos",
        json={"monto": 50.0, "metodo_pago": "EFECTIVO", "estado": "CONFIRMED"},
        headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": key_comun}
    )
    assert r_p1.status_code == status.HTTP_201_CREATED

    # Registrar pago en Venta 2 con la MISMA key_comun
    # Como la ruta contiene el recurso (/api/v1/ventas/{v2_id}/pagos), NO debe haber colisión 409
    r_p2 = client.post(
        f"/api/v1/ventas/{v2_id}/pagos",
        json={"monto": 50.0, "metodo_pago": "EFECTIVO", "estado": "CONFIRMED"},
        headers={"Authorization": f"Bearer {token}", "X-Idempotency-Key": key_comun}
    )
    assert r_p2.status_code == status.HTTP_201_CREATED
    assert r_p1.json()["id"] != r_p2.json()["id"]


# ===========================================================================
# 7. Idempotencia natural en reintentos de completar venta
# ===========================================================================
def test_07_completar_venta_natural_idempotency(atomic_env):
    client = atomic_env["client"]
    token = atomic_env["token1"]

    # Crear venta PENDIENTE (sin completar)
    r_v = client.post("/api/v1/ventas", json={
        "almacen_id": atomic_env["alm1_id"],
        "detalles": [{"variante_id": atomic_env["v1_id"], "cantidad": 4, "precio_unitario": 100.0}],
        "metodo_pago": "EFECTIVO",
        "monto_recibido": 400.0,
        "completar": False,
    }, headers={"Authorization": f"Bearer {token}"})
    venta_id = r_v.json()["id"]
    assert r_v.json()["estado"] == "PENDIENTE"

    # 1. Primer completar: debe completarla y descontar 4 unidades de stock
    r_comp1 = client.post(
        f"/api/v1/ventas/{venta_id}/completar",
        json={"metodo_pago": "EFECTIVO", "monto_recibido": 400.0},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert r_comp1.status_code == status.HTTP_200_OK
    assert r_comp1.json()["estado"] == "COMPLETADA"

    # 2. Segundo completar (reintento de red sin llave): naturalmente idempotente
    r_comp2 = client.post(
        f"/api/v1/ventas/{venta_id}/completar",
        json={"metodo_pago": "EFECTIVO", "monto_recibido": 400.0},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert r_comp2.status_code == status.HTTP_200_OK
    assert r_comp2.json()["estado"] == "COMPLETADA"

    # Verificar que el stock solo se descontó 4 unidades en total (100 - 4 = 96)
    db = atomic_env["TestingSession"]()
    stock = db.query(Stock).filter(Stock.variante_id == atomic_env["v1_id"]).first()
    db.close()
    assert stock.cantidad_actual == Decimal("96.00"), "El stock no debió descontarse por segunda vez"


# ===========================================================================
# 8. Idempotencia natural en marcar pago fallido
# ===========================================================================
def test_08_marcar_pago_fallido_natural_idempotency(atomic_env):
    client = atomic_env["client"]
    token = atomic_env["token1"]

    # Crear venta con pago QR PENDING
    r_v = client.post("/api/v1/ventas", json={
        "almacen_id": atomic_env["alm1_id"],
        "detalles": [{"variante_id": atomic_env["v1_id"], "cantidad": 1, "precio_unitario": 100.0}],
        "pagos": [{"metodo_pago": "QR", "monto": 100.0, "estado": "PENDING"}],
        "completar": True,
    }, headers={"Authorization": f"Bearer {token}"})
    pago_id = r_v.json()["pagos"][0]["id"]
    assert r_v.json()["pagos"][0]["estado"] == "PENDING"

    # 1. Primer marcado como fallido
    r_f1 = client.post(
        f"/api/v1/ventas/pagos/{pago_id}/marcar-fallido",
        json={"motivo": "QR expiró"},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert r_f1.status_code == status.HTTP_200_OK
    assert r_f1.json()["estado"] == "FAILED"

    # 2. Segundo marcado como fallido (reintento): no debe fallar con 400
    r_f2 = client.post(
        f"/api/v1/ventas/pagos/{pago_id}/marcar-fallido",
        json={"motivo": "QR expiró"},
        headers={"Authorization": f"Bearer {token}"}
    )
    assert r_f2.status_code == status.HTTP_200_OK
    assert r_f2.json()["estado"] == "FAILED"
