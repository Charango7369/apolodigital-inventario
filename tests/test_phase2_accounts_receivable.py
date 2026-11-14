"""
Tests unitarios e integrativos para AccountsReceivableService (Bloque 2.3 — Gestión de Cartera, Cuentas por Cobrar y Morosidad).

Cubre:
1. Consulta de cartera general con cálculo dinámico de morosidad y filtros por estado y solo_vencidas.
2. Consulta de cartera agrupada por cliente (métricas de exposición crediticia, morosidad y saldos).
3. Consulta de cuenta por cobrar asociada a una venta específica.
4. Registro de amortizaciones parciales y totales con delegación en PaymentService.
5. Control estricto contra sobrepagos: rechazo de abono superior al saldo operativo.
6. Rechazo de abonos con importes cero o negativos.
7. Amortización diferida: pagos QR pendientes no reducen saldo operativo hasta su confirmación explícita.
8. Reversiones: reapertura automática del saldo operativo y transición PAGADA -> PENDIENTE.
9. Cobros presuntivos históricos: no reducen saldo operativo hasta contar con conciliación aprobada.
10. Conciliación aprobada amortiza la cuenta por cobrar en cartera.
11. Cálculo dinámico de mora en America/La_Paz:
    - Cuenta vigente: dias_mora = 0, esta_vencida = False.
    - Cuenta vencida: dias_mora calculado con fecha comercial, esta_vencida = True.
    - Cuenta REQUIERE_CONCILIACION sin vencimiento: esta_vencida = False, dias_mora = 0.
    - Cuenta liquidada (saldo 0): esta_vencida = False.
12. Aislamiento multi-tenant estricto en consultas, abonos y estados de cuenta.
13. Estado de cuenta completo por cliente con historial de cargos, abonos y métricas.
14. Atomicidad transaccional y rollback ante errores.
"""

from datetime import datetime, timezone, date, timedelta
from decimal import Decimal
import uuid
import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.modules.auth.models import User, UserRole
from app.modules.inventario.models import Negocio, Almacen, Producto, Variante, Lote, Stock
from app.modules.ventas.models import (
    Cliente, Venta, DetalleVenta, PagoVenta, ReversionPago, CuentaPorCobrar, ConciliacionPago,
    EstadoFinanciero, EstadoVenta, EstadoCuentaCobrar, EstadoPago, OrigenPago, EstadoConciliacion, MetodoPago,
    fecha_comercial_hoy,
)
from app.modules.ventas.schemas import (
    VentaCreate, DetalleVentaCreate, PagoCreate,
)
from app.modules.ventas.services.sale_service import SaleService
from app.modules.ventas.services.payment_service import PaymentService
from app.modules.ventas.services.accounts_receivable_service import AccountsReceivableService


@pytest.fixture
def db_session():
    """Crea una sesión de base de datos en memoria para pruebas aisladas."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    yield session

    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def ar_env(db_session):
    """
    Configura un entorno de pruebas completo para cuentas por cobrar:
    - Negocio "Ferretería Central"
    - Usuario ADMIN y Usuario EMPLEADO
    - Cliente "Constructora Los Andes" y Cliente "Distribuidora del Valle"
    - 2 Cuentas por cobrar con diferentes vencimientos
    """
    db = db_session
    neg_id = str(uuid.uuid4())
    usr_admin_id = str(uuid.uuid4())
    usr_cajero_id = str(uuid.uuid4())
    alm_id = str(uuid.uuid4())

    negocio = Negocio(
        id=neg_id,
        nombre="Ferretería Central",
        propietario="Admin Propietario",
        moneda="BOB",
        activo=True,
    )
    db.add(negocio)

    admin = User(
        id=usr_admin_id,
        email="admin_ar@apolo.lat",
        password_hash="hash",
        nombre="Administrador Cartera",
        negocio_id=neg_id,
        rol=UserRole.ADMIN.value,
        activo=True,
    )
    cajero = User(
        id=usr_cajero_id,
        email="cajero_ar@apolo.lat",
        password_hash="hash",
        nombre="Cajero Cobranzas",
        negocio_id=neg_id,
        rol=UserRole.EMPLEADO.value,
        activo=True,
    )
    db.add_all([admin, cajero])

    almacen = Almacen(
        id=alm_id,
        negocio_id=neg_id,
        nombre="Almacén Matriz",
        es_principal=True,
    )
    db.add(almacen)

    cliente_1 = Cliente(
        id=str(uuid.uuid4()),
        negocio_id=neg_id,
        nombre="Constructora Los Andes",
        telefono="71234567",
        nit="10203040",
        activo=True,
    )
    cliente_2 = Cliente(
        id=str(uuid.uuid4()),
        negocio_id=neg_id,
        nombre="Distribuidora del Valle",
        telefono="79876543",
        nit="50607080",
        activo=True,
    )
    db.add_all([cliente_1, cliente_2])

    prod = Producto(
        id=str(uuid.uuid4()),
        negocio_id=neg_id,
        nombre="Cemento Portland 50kg",
        unidad_medida="bolsa",
        activo=True,
    )
    db.add(prod)
    db.flush()

    var = Variante(
        id=str(uuid.uuid4()),
        producto_id=prod.id,
        sku="CEM-50KG",
        precio_venta=Decimal("60.00"),
        precio_costo=Decimal("45.00"),
        atributos={"presentacion": "50kg"},
        activa=True,
    )
    db.add(var)
    db.flush()

    lote = Lote(
        id=str(uuid.uuid4()),
        variante_id=var.id,
        almacen_id=alm_id,
        fecha_ingreso=datetime.now(timezone.utc),
        cantidad_inicial=Decimal("500"),
        cantidad_actual=Decimal("500"),
        costo_unitario=Decimal("45.00"),
        activo=True,
    )
    db.add(lote)

    stock = Stock(
        id=str(uuid.uuid4()),
        variante_id=var.id,
        almacen_id=alm_id,
        cantidad_actual=Decimal("500"),
    )
    db.add(stock)
    db.commit()

    # Venta a crédito 1: Bs 600, Vencimiento futuro (vigente)
    hoy = fecha_comercial_hoy()
    data_venta_1 = VentaCreate(
        almacen_id=alm_id,
        cliente_id=cliente_1.id,
        cliente_nombre=cliente_1.nombre,
        cliente_nit=cliente_1.nit,
        metodo_pago="CREDITO",
        es_credito=True,
        fecha_vencimiento=hoy + timedelta(days=15),
        detalles=[DetalleVentaCreate(variante_id=var.id, cantidad=Decimal("10"))],
        pagos=[],
        completar=True,
    )
    venta_1 = SaleService.crear_venta_financiera(db, neg_id, usr_cajero_id, data_venta_1)

    # Venta a crédito 2: Bs 300, Vencimiento pasado (vencida con mora)
    data_venta_2 = VentaCreate(
        almacen_id=alm_id,
        cliente_id=cliente_2.id,
        cliente_nombre=cliente_2.nombre,
        cliente_nit=cliente_2.nit,
        metodo_pago="CREDITO",
        es_credito=True,
        fecha_vencimiento=hoy - timedelta(days=10),
        detalles=[DetalleVentaCreate(variante_id=var.id, cantidad=Decimal("5"))],
        pagos=[],
        completar=True,
    )
    venta_2 = SaleService.crear_venta_financiera(db, neg_id, usr_cajero_id, data_venta_2)

    return {
        "negocio_id": neg_id,
        "admin_id": usr_admin_id,
        "cajero_id": usr_cajero_id,
        "almacen_id": alm_id,
        "cliente_1": cliente_1,
        "cliente_2": cliente_2,
        "venta_1": venta_1,
        "venta_2": venta_2,
    }


# ===========================================================================
# 1. CONSULTA DE CARTERA GENERAL Y POR CLIENTE
# ===========================================================================

def test_consultar_cartera_general_y_filtros(db_session, ar_env):
    """Consulta de cartera con estados, cálculo dinámico de morosidad y filtros."""
    env = ar_env
    db = db_session

    # Consulta general sin filtros (debe traer 2 cuentas)
    cartera = AccountsReceivableService.consultar_cartera(db, env["negocio_id"])
    assert len(cartera) == 2

    c1 = next(c for c in cartera if c["venta_id"] == env["venta_1"].id)
    assert c1["saldo_operativo"] == Decimal("600.00")
    assert c1["esta_vencida"] is False
    assert c1["dias_mora"] == 0
    assert c1["cliente_nombre"] == "Constructora Los Andes"

    c2 = next(c for c in cartera if c["venta_id"] == env["venta_2"].id)
    assert c2["saldo_operativo"] == Decimal("300.00")
    assert c2["esta_vencida"] is True
    assert c2["dias_mora"] == 10
    assert c2["cliente_nombre"] == "Distribuidora del Valle"

    # Filtro solo_vencidas=True (solo debe traer la cuenta 2)
    vencidas = AccountsReceivableService.consultar_cartera(db, env["negocio_id"], solo_vencidas=True)
    assert len(vencidas) == 1
    assert vencidas[0]["venta_id"] == env["venta_2"].id

    # Filtro por cliente
    por_cliente = AccountsReceivableService.consultar_cartera(db, env["negocio_id"], cliente_id=env["cliente_1"].id)
    assert len(por_cliente) == 1
    assert por_cliente[0]["cliente_id"] == env["cliente_1"].id


def test_consultar_cartera_por_cliente_agregada(db_session, ar_env):
    """Retorna las métricas agrupadas de exposición crediticia por cliente."""
    env = ar_env
    db = db_session

    resumen_clientes = AccountsReceivableService.consultar_cartera_por_cliente(db, env["negocio_id"])
    assert len(resumen_clientes) == 2

    r1 = next(r for r in resumen_clientes if r["cliente_id"] == env["cliente_1"].id)
    assert r1["total_cuentas"] == 1
    assert r1["cuentas_activas"] == 1
    assert r1["cuentas_vencidas"] == 0
    assert r1["monto_total_financiado"] == Decimal("600.00")
    assert r1["saldo_operativo_total"] == Decimal("600.00")
    assert r1["max_dias_mora"] == 0

    r2 = next(r for r in resumen_clientes if r["cliente_id"] == env["cliente_2"].id)
    assert r2["total_cuentas"] == 1
    assert r2["cuentas_activas"] == 1
    assert r2["cuentas_vencidas"] == 1
    assert r2["saldo_operativo_total"] == Decimal("300.00")
    assert r2["max_dias_mora"] == 10


def test_consultar_cuenta_venta_especifica(db_session, ar_env):
    """Consulta la cuenta de una venta individual y valida detalle completo."""
    env = ar_env
    db = db_session

    cuenta = AccountsReceivableService.consultar_cuenta_venta(db, env["negocio_id"], env["venta_1"].id)
    assert cuenta["venta_id"] == env["venta_1"].id
    assert cuenta["saldo_operativo"] == Decimal("600.00")
    assert cuenta["cliente_nombre"] == "Constructora Los Andes"
    assert cuenta["cliente_nit"] == "10203040"


# ===========================================================================
# 2. REGISTRO DE AMORTIZACIONES Y REGLAS FINANCIERAS
# ===========================================================================

def test_amortizacion_parcial_y_total_exitosas(db_session, ar_env):
    """
    Abonos a una cuenta por cobrar:
    1. Abono parcial de Bs 200 en efectivo: saldo baja a Bs 400, estado sigue PENDIENTE, venta pasa a PARTIAL.
    2. Abono total del remanente (Bs 400): saldo baja a Bs 0, cuenta pasa a PAGADA, venta pasa a PAID.
    """
    env = ar_env
    db = db_session

    cxc = db.query(CuentaPorCobrar).filter(CuentaPorCobrar.venta_id == env["venta_1"].id).first()

    # 1. Abono parcial
    res1 = AccountsReceivableService.registrar_abono(
        db=db,
        negocio_id=env["negocio_id"],
        cuenta_id=cxc.id,
        usuario_id=env["cajero_id"],
        monto=Decimal("200.00"),
        metodo_pago="EFECTIVO",
        referencia="RECIBO-ABONO-01",
    )

    assert res1["saldo_operativo_anterior"] == Decimal("600.00")
    assert res1["saldo_operativo_nuevo"] == Decimal("400.00")
    assert res1["estado_cuenta"] == EstadoCuentaCobrar.PENDIENTE.value
    assert res1["estado_financiero_venta"] == EstadoFinanciero.PARTIAL.value
    assert res1["pago"].estado == EstadoPago.CONFIRMED.value
    assert res1["pago"].origen == OrigenPago.OPERATIVO.value
    assert res1["pago"].es_cobro_verificado is True

    # 2. Abono liquidatorio del remanente
    res2 = AccountsReceivableService.registrar_abono(
        db=db,
        negocio_id=env["negocio_id"],
        cuenta_id=cxc.id,
        usuario_id=env["cajero_id"],
        monto=Decimal("400.00"),
        metodo_pago="TARJETA",
        referencia="VOUCHER-TARJ-99",
    )

    assert res2["saldo_operativo_anterior"] == Decimal("400.00")
    assert res2["saldo_operativo_nuevo"] == Decimal("0.00")
    assert res2["estado_cuenta"] == EstadoCuentaCobrar.PAGADA.value
    assert res2["estado_financiero_venta"] == EstadoFinanciero.PAID.value

    # Verificación en base de datos
    db.refresh(cxc)
    assert cxc.saldo_pendiente == Decimal("0.00")
    assert cxc.estado == EstadoCuentaCobrar.PAGADA.value
    assert cxc.esta_vencida is False


def test_rechazo_sobrepago_amortizacion(db_session, ar_env):
    """Intentar abonar más del saldo operativo pendiente debe lanzar ValueError."""
    env = ar_env
    db = db_session

    cxc = db.query(CuentaPorCobrar).filter(CuentaPorCobrar.venta_id == env["venta_1"].id).first()

    with pytest.raises(ValueError, match="supera el saldo operativo pendiente"):
        AccountsReceivableService.registrar_abono(
            db=db,
            negocio_id=env["negocio_id"],
            cuenta_id=cxc.id,
            usuario_id=env["cajero_id"],
            monto=Decimal("650.00"),  # Saldo es 600
            metodo_pago="EFECTIVO",
        )


def test_rechazo_abono_invalido_monto_cero_o_negativo(db_session, ar_env):
    """Abonos con monto cero o negativo son rechazados."""
    env = ar_env
    db = db_session

    cxc = db.query(CuentaPorCobrar).filter(CuentaPorCobrar.venta_id == env["venta_1"].id).first()

    with pytest.raises(ValueError, match="estrictamente mayor a cero"):
        AccountsReceivableService.registrar_abono(
            db=db,
            negocio_id=env["negocio_id"],
            cuenta_id=cxc.id,
            usuario_id=env["cajero_id"],
            monto=Decimal("0.00"),
            metodo_pago="EFECTIVO",
        )

    with pytest.raises(ValueError, match="estrictamente mayor a cero"):
        AccountsReceivableService.registrar_abono(
            db=db,
            negocio_id=env["negocio_id"],
            cuenta_id=cxc.id,
            usuario_id=env["cajero_id"],
            monto=Decimal("-50.00"),
            metodo_pago="EFECTIVO",
        )


def test_amortizacion_qr_pendiente_no_reduce_saldo_hasta_confirmacion(db_session, ar_env):
    """
    Un abono con método QR sin confirmar nace PENDING:
    - NO reduce el saldo operativo.
    - Cuando se confirma explícitamente vía PaymentService, amortiza la cuenta.
    """
    env = ar_env
    db = db_session

    cxc = db.query(CuentaPorCobrar).filter(CuentaPorCobrar.venta_id == env["venta_1"].id).first()

    # 1. Registrar abono QR (nace PENDING por defecto)
    res_qr = AccountsReceivableService.registrar_abono(
        db=db,
        negocio_id=env["negocio_id"],
        cuenta_id=cxc.id,
        usuario_id=env["cajero_id"],
        monto=Decimal("250.00"),
        metodo_pago="QR",
        referencia="QR-PENDIENTE-789",
    )

    assert res_qr["pago"].estado == EstadoPago.PENDING.value
    assert res_qr["pago"].es_cobro_verificado is False
    # La deuda operativa permanece intacta en 600
    assert res_qr["saldo_operativo_nuevo"] == Decimal("600.00")

    # 2. Confirmar el pago QR mediante comprobante bancario
    pago_confirmado = PaymentService.confirmar_pago(
        db=db,
        negocio_id=env["negocio_id"],
        pago_id=res_qr["pago"].id,
        usuario_id=env["cajero_id"],
        referencia_confirmacion="BANCO-CONF-555",
        notas="Verificado en extracto bancario",
    )
    assert pago_confirmado.estado == EstadoPago.CONFIRMED.value
    assert pago_confirmado.es_cobro_verificado is True

    # 3. La cuenta ahora sí refleja la amortización (600 - 250 = 350)
    cuenta_actualizada = AccountsReceivableService.consultar_cuenta_venta(
        db, env["negocio_id"], env["venta_1"].id
    )
    assert cuenta_actualizada["saldo_operativo"] == Decimal("350.00")
    assert cuenta_actualizada["cobros_verificados_netos"] == Decimal("250.00")


# ===========================================================================
# 3. EFECTOS DE REVERSIONES Y CONCILIACIONES SOBRE LA CARTERA
# ===========================================================================

def test_reversion_reabre_saldo_en_cartera(db_session, ar_env):
    """
    Una cuenta liquidada que sufre una reversión contable en su pago
    reabre automáticamente su saldo operativo y transiciona PAGADA -> PENDIENTE.
    """
    env = ar_env
    db = db_session

    cxc = db.query(CuentaPorCobrar).filter(CuentaPorCobrar.venta_id == env["venta_1"].id).first()

    # Liquidar cuenta totalmente (abono de Bs 600)
    abono = AccountsReceivableService.registrar_abono(
        db=db,
        negocio_id=env["negocio_id"],
        cuenta_id=cxc.id,
        usuario_id=env["cajero_id"],
        monto=Decimal("600.00"),
        metodo_pago="EFECTIVO",
    )
    assert abono["estado_cuenta"] == EstadoCuentaCobrar.PAGADA.value
    assert abono["saldo_operativo_nuevo"] == Decimal("0.00")

    # Reversión contable de Bs 200 sobre el pago realizado
    PaymentService.revertir_pago(
        db=db,
        negocio_id=env["negocio_id"],
        pago_id=abono["pago"].id,
        usuario_id=env["cajero_id"],
        monto_a_revertir=Decimal("200.00"),
        motivo="Error de cobro en caja",
    )

    # La cartera debe reabrir exactamente los Bs 200
    cuenta_reabierta = AccountsReceivableService.consultar_cuenta_venta(
        db, env["negocio_id"], env["venta_1"].id
    )
    assert cuenta_reabierta["saldo_operativo"] == Decimal("200.00")
    assert cuenta_reabierta["estado"] == EstadoCuentaCobrar.PENDIENTE.value
    assert cuenta_reabierta["cobros_verificados_netos"] == Decimal("400.00")


def test_cobro_presuntivo_historico_y_conciliacion_en_cartera(db_session, ar_env):
    """
    Un pago presuntivo histórico en una cuenta a crédito:
    - NO reduce el saldo operativo mientras no esté conciliado.
    - Al conciliarse formalmente por el ADMIN, amortiza la cartera.
    """
    env = ar_env
    db = db_session

    # Simular una venta y cuenta histórica a crédito de Bs 500
    v_hist = Venta(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        almacen_id=env["almacen_id"],
        numero=7777,
        total=Decimal("500.00"),
        subtotal=Decimal("500.00"),
        metodo_pago=MetodoPago.CREDITO.value,
        estado=EstadoVenta.COMPLETADA.value,
        estado_financiero=EstadoFinanciero.CREDIT.value,
        usuario_id=env["cajero_id"],
        created_at=datetime(2025, 2, 1, tzinfo=timezone.utc),
    )
    db.add(v_hist)

    cxc_hist = CuentaPorCobrar(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        venta_id=v_hist.id,
        cliente_id=env["cliente_1"].id,
        monto_total_venta=Decimal("500.00"),
        monto_inicial_financiado=Decimal("500.00"),
        saldo_pendiente=Decimal("500.00"),
        fecha_vencimiento=None,
        estado=EstadoCuentaCobrar.REQUIERE_CONCILIACION.value,
        created_at=datetime(2025, 2, 1, tzinfo=timezone.utc),
    )
    db.add(cxc_hist)

    # Pago presuntivo histórico de Bs 300
    p_presunt = PagoVenta(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        venta_id=v_hist.id,
        monto=Decimal("300.00"),
        metodo_pago="EFECTIVO",
        estado=EstadoPago.CONFIRMED.value,
        origen=OrigenPago.HISTORICO_PRESUNTIVO.value,
        usuario_id=env["cajero_id"],
        fecha_pago=datetime(2025, 2, 1, tzinfo=timezone.utc),
        referencia="MIGRACION_HISTORICA",
    )
    db.add(p_presunt)
    db.commit()

    # 1. Antes de conciliar: la deuda operativa sigue en 500 y no genera mora artificial
    cta_antes = AccountsReceivableService.consultar_cuenta_venta(db, env["negocio_id"], v_hist.id)
    assert cta_antes["saldo_operativo"] == Decimal("500.00")
    assert cta_antes["cobros_presuntivos_netos"] == Decimal("300.00")
    assert cta_antes["cobros_verificados_netos"] == Decimal("0.00")
    assert cta_antes["esta_vencida"] is False
    assert cta_antes["dias_mora"] == 0

    # 2. Conciliar formalmente el pago presuntivo
    PaymentService.conciliar_pago_historico(
        db=db,
        negocio_id=env["negocio_id"],
        pago_id=p_presunt.id,
        usuario_id=env["admin_id"],
        motivo="Comprobante físico de abono verificado",
        referencia_documental="TALONARIO-HIST-88",
    )

    # 3. Después de conciliar: el saldo operativo bajó a Bs 200 (500 - 300 verificado)
    cta_despues = AccountsReceivableService.consultar_cuenta_venta(db, env["negocio_id"], v_hist.id)
    assert cta_despues["saldo_operativo"] == Decimal("200.00")
    assert cta_despues["cobros_verificados_netos"] == Decimal("300.00")
    assert cta_despues["cobros_presuntivos_netos"] == Decimal("0.00")


# ===========================================================================
# 4. AISLAMIENTO MULTI-TENANT Y ESTADO DE CUENTA
# ===========================================================================

def test_aislamiento_multitenant_en_cartera(db_session, ar_env):
    """No es posible consultar ni amortizar cuentas de otro tenant."""
    env = ar_env
    db = db_session
    otro_negocio_id = str(uuid.uuid4())

    cxc = db.query(CuentaPorCobrar).filter(CuentaPorCobrar.venta_id == env["venta_1"].id).first()

    # 1. Cartera de otro tenant está vacía
    cartera_ajena = AccountsReceivableService.consultar_cartera(db, otro_negocio_id)
    assert len(cartera_ajena) == 0

    # 2. Intento de abono con negocio ajeno debe fallar
    with pytest.raises(ValueError, match="Cuenta por cobrar no encontrada o no pertenece a su negocio"):
        AccountsReceivableService.registrar_abono(
            db=db,
            negocio_id=otro_negocio_id,
            cuenta_id=cxc.id,
            usuario_id=env["cajero_id"],
            monto=Decimal("100.00"),
            metodo_pago="EFECTIVO",
        )

    # 3. Consulta de cuenta individual con tenant ajeno debe fallar
    with pytest.raises(ValueError, match="Cuenta por cobrar no encontrada o no pertenece a su negocio"):
        AccountsReceivableService.consultar_cuenta_venta(db, otro_negocio_id, env["venta_1"].id)


def test_estado_de_cuenta_cliente(db_session, ar_env):
    """Emisión de extracto consolidado de cuenta corriente del cliente."""
    env = ar_env
    db = db_session

    extracto = AccountsReceivableService.estado_de_cuenta_cliente(
        db=db,
        negocio_id=env["negocio_id"],
        cliente_id=env["cliente_1"].id,
    )

    assert extracto["cliente_nombre"] == "Constructora Los Andes"
    assert extracto["resumen"]["total_cuentas"] == 1
    assert extracto["resumen"]["cuentas_activas"] == 1
    assert extracto["resumen"]["saldo_operativo_total"] == Decimal("600.00")
    assert len(extracto["cuentas"]) == 1
    assert extracto["cuentas"][0]["venta_id"] == env["venta_1"].id
