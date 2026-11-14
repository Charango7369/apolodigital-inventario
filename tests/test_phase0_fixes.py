"""
Tests unitarios y de integración para las correcciones de Fase 0 (Saneamiento y Seguridad).

Cubre:
- H-01: Prevención IDOR en cliente_id durante crear_venta.
- H-08: Preservación de cliente_nit en notas de la venta.
- H-09: Verificación de sincronización FEFO sin doble descuento/incremento de stock.
- H-05: Conciliación de caja excluyendo crédito de total_general.
- H-03: Inyección de dependencia db en endpoint /api/v1/auth/register.
- H-06: Búsqueda multi-tenant de usuarios activos en authenticate_user.
- H-02: Ejecución de obtener_utilidad_periodo sin NameError.
"""

import uuid
from datetime import datetime, date, timezone
from decimal import Decimal

import pytest

from app.modules.auth.models import User, UserRole
from app.modules.auth.service import hash_password, authenticate_user, create_access_token
from app.modules.inventario.models import Almacen, Negocio, Producto, Variante, Lote, Stock
from app.modules.inventario.service import get_or_create_stock
from app.modules.ventas.models import Cliente, EstadoVenta, MetodoPago, Venta
from app.modules.ventas.schemas import VentaCreate, DetalleVentaCreate
from app.modules.ventas.service import (
    crear_venta,
    completar_venta,
    cancelar_venta,
    get_resumen_caja,
    obtener_utilidad_periodo,
)


def _crear_negocio(db, nombre="Tienda Fase 0"):
    negocio = Negocio(
        id=str(uuid.uuid4()),
        nombre=nombre,
        propietario="Admin",
        moneda="BOB",
        activo=True,
    )
    db.add(negocio)
    db.flush()
    return negocio


def _crear_usuario(db, negocio_id, nombre="Cajero Fase 0"):
    usuario = User(
        id=str(uuid.uuid4()),
        email=f"user_{uuid.uuid4().hex[:6]}@apolo.com",
        password_hash="pwd",
        nombre=nombre,
        negocio_id=negocio_id,
        rol=UserRole.EMPLEADO.value,
        activo=True,
    )
    db.add(usuario)
    db.flush()
    return usuario


@pytest.fixture
def almacen_test(db):
    negocio = _crear_negocio(db, "Negocio Almacén Test")
    almacen = Almacen(
        id=str(uuid.uuid4()),
        negocio_id=negocio.id,
        nombre="Almacén Fase 0",
        es_principal=True,
    )
    db.add(almacen)
    db.commit()
    db.refresh(almacen)
    return almacen


def _crear_producto_con_stock(db, negocio_id: str, almacen_id: str, stock_qty: Decimal, precio: Decimal = Decimal("50.00")):
    producto = Producto(
        id=str(uuid.uuid4()),
        negocio_id=negocio_id,
        nombre=f"Producto Test {uuid.uuid4().hex[:6]}",
        unidad_medida="unidad",
        tiene_variantes=False,
        es_servicio=False,
        activo=True,
        controla_vencimiento=False,
    )
    db.add(producto)
    db.flush()

    variante = Variante(
        id=str(uuid.uuid4()),
        producto_id=producto.id,
        atributos={},
        precio_venta=precio,
        precio_costo=Decimal("20.00"),
        activa=True,
    )
    db.add(variante)
    db.flush()

    if stock_qty > 0:
        lote = Lote(
            id=str(uuid.uuid4()),
            variante_id=variante.id,
            almacen_id=almacen_id,
            fecha_ingreso=datetime.now(timezone.utc),
            cantidad_inicial=stock_qty,
            cantidad_actual=stock_qty,
            costo_unitario=Decimal("20.00"),
            activo=True,
        )
        db.add(lote)

    stock = Stock(
        id=str(uuid.uuid4()),
        variante_id=variante.id,
        almacen_id=almacen_id,
        cantidad_actual=stock_qty,
    )
    db.add(stock)
    db.commit()
    return producto, variante


# ---------------------------------------------------------------------------
# H-01: Prevención IDOR en cliente_id
# ---------------------------------------------------------------------------
def test_h01_cliente_idor_cross_tenant_rejected(db, almacen_test):
    negocio_a = almacen_test.negocio_id
    negocio_b = _crear_negocio(db, "Negocio B").id

    # Cliente que pertenece a Negocio B
    cliente_b = Cliente(
        id=str(uuid.uuid4()),
        negocio_id=negocio_b,
        nombre="Cliente Negocio B",
        activo=True,
    )
    db.add(cliente_b)

    # Cliente inactivo en Negocio A
    cliente_inactivo_a = Cliente(
        id=str(uuid.uuid4()),
        negocio_id=negocio_a,
        nombre="Cliente Inactivo A",
        activo=False,
    )
    db.add(cliente_inactivo_a)
    db.commit()

    _, variante = _crear_producto_con_stock(db, negocio_a, almacen_test.id, Decimal("10"))
    usuario = _crear_usuario(db, negocio_a)

    # Caso 1: Intentar crear venta en Negocio A con cliente de Negocio B (IDOR)
    venta_data_idor = VentaCreate(
        almacen_id=almacen_test.id,
        cliente_id=cliente_b.id,
        metodo_pago=MetodoPago.EFECTIVO.value,
        detalles=[DetalleVentaCreate(variante_id=variante.id, cantidad=Decimal("1"))],
    )
    with pytest.raises(ValueError, match="Cliente no encontrado, inactivo o no pertenece a su negocio"):
        crear_venta(db, negocio_a, venta_data_idor, usuario.id)

    # Caso 2: Intentar crear venta con cliente inactivo
    venta_data_inactivo = VentaCreate(
        almacen_id=almacen_test.id,
        cliente_id=cliente_inactivo_a.id,
        metodo_pago=MetodoPago.EFECTIVO.value,
        detalles=[DetalleVentaCreate(variante_id=variante.id, cantidad=Decimal("1"))],
    )
    with pytest.raises(ValueError, match="Cliente no encontrado, inactivo o no pertenece a su negocio"):
        crear_venta(db, negocio_a, venta_data_inactivo, usuario.id)

    # Caso 3: Cliente válido y activo en Negocio A -> Éxito
    cliente_valido_a = Cliente(
        id=str(uuid.uuid4()),
        negocio_id=negocio_a,
        nombre="Cliente Válido A",
        activo=True,
    )
    db.add(cliente_valido_a)
    db.commit()

    venta_valida = VentaCreate(
        almacen_id=almacen_test.id,
        cliente_id=cliente_valido_a.id,
        metodo_pago=MetodoPago.EFECTIVO.value,
        detalles=[DetalleVentaCreate(variante_id=variante.id, cantidad=Decimal("1"))],
    )
    v = crear_venta(db, negocio_a, venta_valida, usuario.id)
    assert v.cliente_id == cliente_valido_a.id
    assert v.cliente_nombre == "Cliente Válido A"


# ---------------------------------------------------------------------------
# H-08: Preservación de cliente_nit en notas
# ---------------------------------------------------------------------------
def test_h08_cliente_nit_preserved_in_notas(db, almacen_test):
    negocio_id = almacen_test.negocio_id
    _, variante = _crear_producto_con_stock(db, negocio_id, almacen_test.id, Decimal("10"))
    usuario = _crear_usuario(db, negocio_id)

    venta_data = VentaCreate(
        almacen_id=almacen_test.id,
        cliente_nombre="Consumidor Final",
        cliente_nit="8472910015",
        notas="Entregar por la tarde",
        metodo_pago=MetodoPago.EFECTIVO.value,
        detalles=[DetalleVentaCreate(variante_id=variante.id, cantidad=Decimal("1"))],
    )
    venta = crear_venta(db, negocio_id, venta_data, usuario.id)
    assert "[NIT: 8472910015]" in venta.notas
    assert "Entregar por la tarde" in venta.notas


# ---------------------------------------------------------------------------
# H-09: Stock synchronization sin doble decremento ni doble incremento
# ---------------------------------------------------------------------------
def test_h09_fefo_stock_decrement_accuracy(db, almacen_test):
    negocio_id = almacen_test.negocio_id
    _, variante = _crear_producto_con_stock(db, negocio_id, almacen_test.id, Decimal("10.00"))
    usuario = _crear_usuario(db, negocio_id)

    # Venta de 4 unidades
    venta_data = VentaCreate(
        almacen_id=almacen_test.id,
        metodo_pago=MetodoPago.EFECTIVO.value,
        completar=True,
        detalles=[DetalleVentaCreate(variante_id=variante.id, cantidad=Decimal("4.00"))],
    )
    venta = crear_venta(db, negocio_id, venta_data, usuario.id)
    assert venta.estado == EstadoVenta.COMPLETADA.value

    # Stock debe ser exactamente 6.00 (10 - 4 = 6, NO 10 - 4 - 4 = 2)
    stock = get_or_create_stock(db, variante.id, almacen_test.id)
    assert stock.cantidad_actual == Decimal("6.00")

    # Cancelar venta -> devuelve las 4 unidades
    cancelar_venta(db, negocio_id, venta, usuario.id, motivo="Error de digitación")
    db.refresh(stock)
    # Stock debe volver exactamente a 10.00 (6 + 4 = 10, NO 6 + 4 + 4 = 14)
    assert stock.cantidad_actual == Decimal("10.00")


# ---------------------------------------------------------------------------
# H-05: Conciliación de caja excluyendo crédito de total_general
# ---------------------------------------------------------------------------
def test_h05_resumen_caja_excludes_credit_from_total_general(db, almacen_test):
    negocio_id = almacen_test.negocio_id
    _, v1 = _crear_producto_con_stock(db, negocio_id, almacen_test.id, Decimal("20.00"), precio=Decimal("100.00"))
    _, v2 = _crear_producto_con_stock(db, negocio_id, almacen_test.id, Decimal("20.00"), precio=Decimal("250.00"))
    usuario = _crear_usuario(db, negocio_id)

    # Venta 1: Efectivo 100.00 (completada)
    v1_data = VentaCreate(
        almacen_id=almacen_test.id,
        metodo_pago=MetodoPago.EFECTIVO.value,
        completar=True,
        detalles=[DetalleVentaCreate(variante_id=v1.id, cantidad=Decimal("1.00"))],
    )
    crear_venta(db, negocio_id, v1_data, usuario.id)

    # Venta 2: Crédito 250.00 (completada operativamente, sin dinero en caja)
    v2_data = VentaCreate(
        almacen_id=almacen_test.id,
        metodo_pago=MetodoPago.CREDITO.value,
        completar=True,
        detalles=[DetalleVentaCreate(variante_id=v2.id, cantidad=Decimal("1.00"))],
    )
    crear_venta(db, negocio_id, v2_data, usuario.id)

    resumen = get_resumen_caja(db, negocio_id, date.today())

    # Total general en caja debe ser 100.00 (NO 350.00)
    assert resumen["total_efectivo"] == Decimal("100.00")
    assert resumen["total_credito"] == Decimal("250.00")
    assert resumen["total_credito_otorgado"] == Decimal("250.00")
    assert resumen["total_general"] == Decimal("100.00")


# ---------------------------------------------------------------------------
# H-06: Candidate matching multi-tenant en authenticate_user
# ---------------------------------------------------------------------------
def test_h06_auth_candidate_matching_multi_tenant(db):
    tenant_a = _crear_negocio(db, "Tenant A").id
    tenant_b = _crear_negocio(db, "Tenant B").id
    email = f"shared_{uuid.uuid4().hex[:8]}@apolodigital.com"
    pwd = "SecretPassword123!"

    # Usuario inactivo en Tenant A
    user_inactive_a = User(
        id=str(uuid.uuid4()),
        email=email,
        password_hash=hash_password("other_pwd"),
        nombre="Inactivo Tenant A",
        negocio_id=tenant_a,
        rol=UserRole.EMPLEADO.value,
        activo=False,
    )
    db.add(user_inactive_a)

    # Usuario activo en Tenant B con credenciales correctas
    user_active_b = User(
        id=str(uuid.uuid4()),
        email=email,
        password_hash=hash_password(pwd),
        nombre="Activo Tenant B",
        negocio_id=tenant_b,
        rol=UserRole.ADMIN.value,
        activo=True,
    )
    db.add(user_active_b)
    db.commit()

    # La autenticación debe encontrar al usuario activo de Tenant B
    auth_user = authenticate_user(db, email, pwd)
    assert auth_user is not None
    assert auth_user.id == user_active_b.id
    assert auth_user.negocio_id == tenant_b


# ---------------------------------------------------------------------------
# H-03: Inyección de db en /api/v1/auth/register
# ---------------------------------------------------------------------------
def test_h03_auth_register_with_db_dependency(client, db):
    negocio = _crear_negocio(db, "Negocio Register")
    negocio_id = negocio.id

    # Crear admin en DB para generar token
    admin_user = User(
        id=str(uuid.uuid4()),
        email=f"admin_{uuid.uuid4().hex[:6]}@test.com",
        password_hash=hash_password("adminpass"),
        nombre="Admin Negocio",
        negocio_id=negocio_id,
        rol=UserRole.ADMIN.value,
        activo=True,
    )
    db.add(admin_user)
    db.commit()

    token = create_access_token({
        "sub": admin_user.id,
        "negocio_id": negocio_id,
        "rol": admin_user.rol,
    })

    headers = {"Authorization": f"Bearer {token}"}
    new_user_payload = {
        "email": f"empleado_{uuid.uuid4().hex[:6]}@test.com",
        "password": "Password123!",
        "nombre": "Empleado 1",
        "rol": "empleado",
    }

    # Llamar al endpoint /api/v1/auth/register que antes fallaba por no tener db inyectada
    response = client.post("/api/v1/auth/register", json=new_user_payload, headers=headers)
    assert response.status_code == 201
    data = response.json()
    assert data["email"] == new_user_payload["email"]
    assert data["negocio_id"] == negocio_id


# ---------------------------------------------------------------------------
# H-02: Ejecución de obtener_utilidad_periodo sin NameError
# ---------------------------------------------------------------------------
def test_h02_obtener_utilidad_periodo_executes_cleanly(db, almacen_test):
    negocio_id = almacen_test.negocio_id
    _, variante = _crear_producto_con_stock(db, negocio_id, almacen_test.id, Decimal("10.00"))
    usuario = _crear_usuario(db, negocio_id)

    # Crear y completar venta
    venta_data = VentaCreate(
        almacen_id=almacen_test.id,
        metodo_pago=MetodoPago.EFECTIVO.value,
        completar=True,
        detalles=[DetalleVentaCreate(variante_id=variante.id, cantidad=Decimal("2.00"))],
    )
    crear_venta(db, negocio_id, venta_data, usuario.id)

    # Rango de fechas
    inicio = datetime(2020, 1, 1)
    fin = datetime(2030, 1, 1)

    # Llamar función que antes fallaba con NameError: 'obtener_reporte_utilidad_venta'
    resultado = obtener_utilidad_periodo(db, negocio_id, inicio, fin)
    assert "metricas_fefo" in resultado
    assert "alertas_legacy" in resultado
    assert resultado["resumen_general"]["total_ventas"] >= 1
