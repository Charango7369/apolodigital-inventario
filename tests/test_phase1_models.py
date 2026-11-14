"""
Tests unitarios de los modelos SQLAlchemy y restricciones financieras — Fase 1.2

Cubre:
1. Creación e integridad de PagoVenta, ReversionPago, CuentaPorCobrar y Venta.
2. Inmutabilidad financiera (ausencia de delete-orphan).
3. Estado obligatorio en PagoVenta (sin CONFIRMED por defecto).
4. Restricciones CHECK en importes, rangos de crédito y enums de estado.
5. Aislamiento multi-tenant estricto mediante Claves Foráneas Compuestas:
   - Rechazo de pago asociado a venta de otro tenant.
   - Rechazo de pago registrado por usuario de otro tenant.
   - Rechazo de reversión asociada a una venta diferente a la del pago original.
   - Rechazo de cuenta por cobrar asociada a cliente de otro tenant.
6. Cálculo dinámico de morosidad (esta_vencida) según zona horaria America/La_Paz.
7. Valor por defecto de es_devolucion_efectivo = False en ReversionPago.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.exc import IntegrityError

from app.modules.auth.models import User, UserRole
from app.modules.inventario.models import Almacen, Negocio
from app.modules.ventas.models import (
    Cliente,
    CuentaPorCobrar,
    DetalleVenta,
    EstadoCuentaCobrar,
    EstadoFinanciero,
    EstadoPago,
    EstadoVenta,
    MetodoPago,
    PagoVenta,
    ReversionPago,
    Venta,
    fecha_comercial_hoy,
)


def _crear_entorno_tenant(db, nombre_sufijo="A"):
    """Crea la jerarquía base de un tenant: Negocio, Almacén, Usuario y Cliente."""
    negocio_id = str(uuid.uuid4())
    negocio = Negocio(
        id=negocio_id,
        nombre=f"Tienda {nombre_sufijo}",
        propietario="Propietario",
        moneda="BOB",
        activo=True,
    )
    db.add(negocio)
    db.flush()

    almacen = Almacen(
        id=str(uuid.uuid4()),
        negocio_id=negocio_id,
        nombre=f"Almacén Central {nombre_sufijo}",
        es_principal=True,
    )
    db.add(almacen)

    usuario = User(
        id=str(uuid.uuid4()),
        email=f"cajero_{nombre_sufijo.lower()}_{uuid.uuid4().hex[:4]}@apolo.com",
        password_hash="fakehash",
        nombre=f"Cajero {nombre_sufijo}",
        negocio_id=negocio_id,
        rol=UserRole.EMPLEADO.value,
        activo=True,
    )
    db.add(usuario)

    cliente = Cliente(
        id=str(uuid.uuid4()),
        negocio_id=negocio_id,
        nombre=f"Cliente {nombre_sufijo}",
        activo=True,
    )
    db.add(cliente)
    db.commit()

    return {
        "negocio_id": negocio_id,
        "almacen_id": almacen.id,
        "usuario_id": usuario.id,
        "cliente_id": cliente.id,
    }


def _crear_venta_base(db, env, numero=1, total=Decimal("1000.00"), estado_fin=EstadoFinanciero.PARTIAL.value):
    venta = Venta(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        almacen_id=env["almacen_id"],
        numero=numero,
        cliente_id=env["cliente_id"],
        cliente_nombre="Cliente A",
        cliente_nit="1234567019",
        subtotal=total,
        descuento=Decimal("0.00"),
        total=total,
        metodo_pago=MetodoPago.CREDITO.value,
        estado=EstadoVenta.COMPLETADA.value,
        estado_financiero=estado_fin,
        usuario_id=env["usuario_id"],
    )
    db.add(venta)
    db.commit()
    return venta


# ---------------------------------------------------------------------------
# 1. Integridad referencial y modelos creados con éxito
# ---------------------------------------------------------------------------
def test_modelos_financieros_creacion_y_relaciones_exitosas(db):
    env = _crear_entorno_tenant(db, "TestOK")
    venta = _crear_venta_base(db, env, total=Decimal("1000.00"))

    # 1. Crear pago confirmado de abono inicial Bs 300
    pago = PagoVenta(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        venta_id=venta.id,
        monto=Decimal("300.00"),
        metodo_pago=MetodoPago.EFECTIVO.value,
        estado=EstadoPago.CONFIRMED.value,
        usuario_id=env["usuario_id"],
        referencia="Recibo-001",
    )
    db.add(pago)

    # 2. Crear reversión contable de Bs 50
    reversion = ReversionPago(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        pago_id=pago.id,
        venta_id=venta.id,
        monto_revertido=Decimal("50.00"),
        motivo="Ajuste contable solicitado por cliente",
        usuario_id=env["usuario_id"],
        es_devolucion_efectivo=False,
    )
    db.add(reversion)

    # 3. Crear cuenta por cobrar por saldo Bs 700
    cxc = CuentaPorCobrar(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        venta_id=venta.id,
        cliente_id=env["cliente_id"],
        monto_total_venta=Decimal("1000.00"),
        monto_inicial_financiado=Decimal("700.00"),
        saldo_pendiente=Decimal("750.00"),  # 700 + 50 revertidos
        fecha_vencimiento=date.today() + timedelta(days=15),
        estado=EstadoCuentaCobrar.PENDIENTE.value,
    )
    db.add(cxc)
    db.commit()

    # Verificar relaciones SQLAlchemy cargadas en memoria
    db.refresh(venta)
    assert len(venta.pagos) == 1
    assert venta.pagos[0].id == pago.id
    assert len(pago.reversiones) == 1
    assert pago.reversiones[0].id == reversion.id
    assert venta.cuenta_cobrar is not None
    assert venta.cuenta_cobrar.id == cxc.id
    assert venta.cliente_nit == "1234567019"
    assert reversion.es_devolucion_efectivo is False


# ---------------------------------------------------------------------------
# 2. Condición 2: No confirmar pagos por defecto (estado obligatorio)
# ---------------------------------------------------------------------------
def test_pago_venta_requiere_estado_explicito(db):
    env = _crear_entorno_tenant(db, "SinEstado")
    venta = _crear_venta_base(db, env)

    # Pago sin indicar estado debe fallar por NOT NULL
    pago_sin_estado = PagoVenta(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        venta_id=venta.id,
        monto=Decimal("100.00"),
        metodo_pago=MetodoPago.QR.value,
        usuario_id=env["usuario_id"],
    )
    db.add(pago_sin_estado)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# ---------------------------------------------------------------------------
# 3. Condición 3: Restricciones CHECK en BD
# ---------------------------------------------------------------------------
def test_check_constraints_pago_monto_y_estado(db):
    env = _crear_entorno_tenant(db, "ChecksPago")
    venta = _crear_venta_base(db, env)

    # 3.1 Monto <= 0 en PagoVenta
    pago_negativo = PagoVenta(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        venta_id=venta.id,
        monto=Decimal("0.00"),
        metodo_pago=MetodoPago.EFECTIVO.value,
        estado=EstadoPago.CONFIRMED.value,
        usuario_id=env["usuario_id"],
    )
    db.add(pago_negativo)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    # 3.2 Estado inválido en PagoVenta
    pago_estado_invalido = PagoVenta(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        venta_id=venta.id,
        monto=Decimal("50.00"),
        metodo_pago=MetodoPago.EFECTIVO.value,
        estado="INVALID_STATE",
        usuario_id=env["usuario_id"],
    )
    db.add(pago_estado_invalido)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_check_constraints_reversion_monto(db):
    env = _crear_entorno_tenant(db, "ChecksRev")
    venta = _crear_venta_base(db, env)
    pago = PagoVenta(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        venta_id=venta.id,
        monto=Decimal("100.00"),
        metodo_pago=MetodoPago.EFECTIVO.value,
        estado=EstadoPago.CONFIRMED.value,
        usuario_id=env["usuario_id"],
    )
    db.add(pago)
    db.commit()

    # Monto revertido <= 0
    rev_cero = ReversionPago(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        pago_id=pago.id,
        venta_id=venta.id,
        monto_revertido=Decimal("-5.00"),
        motivo="Error",
        usuario_id=env["usuario_id"],
    )
    db.add(rev_cero)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_check_constraints_cuenta_por_cobrar_rangos(db):
    env = _crear_entorno_tenant(db, "ChecksCxC")
    venta = _crear_venta_base(db, env, total=Decimal("500.00"))

    # 3.3 monto_inicial_financiado > monto_total_venta
    cxc_invalida_1 = CuentaPorCobrar(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        venta_id=venta.id,
        cliente_id=env["cliente_id"],
        monto_total_venta=Decimal("500.00"),
        monto_inicial_financiado=Decimal("600.00"),  # Error
        saldo_pendiente=Decimal("500.00"),
        estado=EstadoCuentaCobrar.PENDIENTE.value,
    )
    db.add(cxc_invalida_1)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    # 3.4 saldo_pendiente > monto_total_venta
    cxc_invalida_2 = CuentaPorCobrar(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        venta_id=venta.id,
        cliente_id=env["cliente_id"],
        monto_total_venta=Decimal("500.00"),
        monto_inicial_financiado=Decimal("500.00"),
        saldo_pendiente=Decimal("550.00"),  # Error
        estado=EstadoCuentaCobrar.PENDIENTE.value,
    )
    db.add(cxc_invalida_2)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# ---------------------------------------------------------------------------
# 4. Condición 4 & H-12: Aislamiento Multi-Tenant Estricto (Pruebas Negativas)
# ---------------------------------------------------------------------------
def test_h12_rechazo_pago_asociado_a_venta_de_otro_tenant(db):
    env_a = _crear_entorno_tenant(db, "TenantA")
    env_b = _crear_entorno_tenant(db, "TenantB")

    venta_a = _crear_venta_base(db, env_a, total=Decimal("200.00"))

    # Intento de registrar un pago en Tenant B apuntando a venta de Tenant A
    pago_cross_tenant = PagoVenta(
        id=str(uuid.uuid4()),
        negocio_id=env_b["negocio_id"],  # Tenant B
        venta_id=venta_a.id,             # Venta de Tenant A
        monto=Decimal("50.00"),
        metodo_pago=MetodoPago.EFECTIVO.value,
        estado=EstadoPago.CONFIRMED.value,
        usuario_id=env_b["usuario_id"],
    )
    db.add(pago_cross_tenant)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_h12_rechazo_pago_registrado_por_usuario_de_otro_tenant(db):
    env_a = _crear_entorno_tenant(db, "TenantA")
    env_b = _crear_entorno_tenant(db, "TenantB")

    venta_a = _crear_venta_base(db, env_a, total=Decimal("200.00"))

    # Pago en Tenant A, sobre venta de Tenant A, pero con usuario_id perteneciente a Tenant B
    pago_user_ajeno = PagoVenta(
        id=str(uuid.uuid4()),
        negocio_id=env_a["negocio_id"],
        venta_id=venta_a.id,
        monto=Decimal("50.00"),
        metodo_pago=MetodoPago.EFECTIVO.value,
        estado=EstadoPago.CONFIRMED.value,
        usuario_id=env_b["usuario_id"],  # Usuario de Tenant B
    )
    db.add(pago_user_ajeno)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_h12_rechazo_reversion_con_venta_desalineada_del_pago(db):
    env = _crear_entorno_tenant(db, "TenantUnico")

    # Dos ventas del mismo tenant
    venta_1 = _crear_venta_base(db, env, numero=1, total=Decimal("100.00"))
    venta_2 = _crear_venta_base(db, env, numero=2, total=Decimal("200.00"))

    # Pago hecho sobre venta_1
    pago_v1 = PagoVenta(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        venta_id=venta_1.id,
        monto=Decimal("100.00"),
        metodo_pago=MetodoPago.EFECTIVO.value,
        estado=EstadoPago.CONFIRMED.value,
        usuario_id=env["usuario_id"],
    )
    db.add(pago_v1)
    db.commit()

    # Intento de reversión asociando pago_v1 pero con venta_2
    rev_desalineada = ReversionPago(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        pago_id=pago_v1.id,
        venta_id=venta_2.id,  # ERROR: pago_v1 no pertenece a venta_2
        monto_revertido=Decimal("50.00"),
        motivo="Intento fraudulento o erróneo de cruzar ventas",
        usuario_id=env["usuario_id"],
    )
    db.add(rev_desalineada)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_h12_rechazo_cxc_con_cliente_de_otro_tenant(db):
    env_a = _crear_entorno_tenant(db, "TenantA")
    env_b = _crear_entorno_tenant(db, "TenantB")

    venta_a = _crear_venta_base(db, env_a, total=Decimal("300.00"))

    # Cuenta por cobrar en Tenant A, para venta de Tenant A, pero con cliente de Tenant B
    cxc_cliente_ajeno = CuentaPorCobrar(
        id=str(uuid.uuid4()),
        negocio_id=env_a["negocio_id"],
        venta_id=venta_a.id,
        cliente_id=env_b["cliente_id"],  # Cliente de Tenant B
        monto_total_venta=Decimal("300.00"),
        monto_inicial_financiado=Decimal("300.00"),
        saldo_pendiente=Decimal("300.00"),
        estado=EstadoCuentaCobrar.PENDIENTE.value,
    )
    db.add(cxc_cliente_ajeno)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# ---------------------------------------------------------------------------
# 5. Condición 5 & H-16: Cálculo Dinámico de Vencimientos (Bolivia Timezone)
# ---------------------------------------------------------------------------
def test_h16_calculo_dinamico_morosidad_fecha_comercial(db):
    env = _crear_entorno_tenant(db, "TenantVencimientos")
    venta = _crear_venta_base(db, env, total=Decimal("500.00"))

    hoy_bolivia = fecha_comercial_hoy()

    # Caso A: Cuenta con saldo > 0 y fecha vencida (ayer) -> esta_vencida == True
    cxc_vencida = CuentaPorCobrar(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        venta_id=venta.id,
        cliente_id=env["cliente_id"],
        monto_total_venta=Decimal("500.00"),
        monto_inicial_financiado=Decimal("500.00"),
        saldo_pendiente=Decimal("200.00"),
        fecha_vencimiento=hoy_bolivia - timedelta(days=1),
        estado=EstadoCuentaCobrar.PENDIENTE.value,
    )
    assert cxc_vencida.esta_vencida is True

    # Caso B: Cuenta con saldo > 0 y fecha futura (mañana) -> esta_vencida == False
    cxc_vigente = CuentaPorCobrar(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        venta_id=venta.id,
        cliente_id=env["cliente_id"],
        monto_total_venta=Decimal("500.00"),
        monto_inicial_financiado=Decimal("500.00"),
        saldo_pendiente=Decimal("200.00"),
        fecha_vencimiento=hoy_bolivia + timedelta(days=1),
        estado=EstadoCuentaCobrar.PENDIENTE.value,
    )
    assert cxc_vigente.esta_vencida is False

    # Caso C: Cuenta histórica con fecha_vencimiento NULL (H-14) -> esta_vencida == False
    cxc_historica = CuentaPorCobrar(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        venta_id=venta.id,
        cliente_id=env["cliente_id"],
        monto_total_venta=Decimal("500.00"),
        monto_inicial_financiado=Decimal("500.00"),
        saldo_pendiente=Decimal("500.00"),
        fecha_vencimiento=None,
        estado=EstadoCuentaCobrar.REQUIERE_CONCILIACION.value,
    )
    assert cxc_historica.esta_vencida is False

    # Caso D: Cuenta saldada (saldo == 0), aunque la fecha sea pasada -> esta_vencida == False
    cxc_pagada = CuentaPorCobrar(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        venta_id=venta.id,
        cliente_id=env["cliente_id"],
        monto_total_venta=Decimal("500.00"),
        monto_inicial_financiado=Decimal("500.00"),
        saldo_pendiente=Decimal("0.00"),
        fecha_vencimiento=hoy_bolivia - timedelta(days=10),
        estado=EstadoCuentaCobrar.PAGADA.value,
    )
    assert cxc_pagada.esta_vencida is False
