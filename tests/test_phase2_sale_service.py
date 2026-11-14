"""
Tests unitarios e integrativos para SaleService (Bloque 2.1 — Schemas y SaleService).

Verifica:
1. Venta de contado pura: PAID, 1 PagoVenta(CONFIRMED), stock descontado vía FEFO.
2. Venta multi-pago (efectivo + QR confirmado): PAID, 2 PagoVenta(CONFIRMED).
3. Venta con pago QR en estado PENDING: PARTIAL, el pago pendiente no cancela la obligación.
4. Venta a crédito puro: CREDIT, 1 CuentaPorCobrar con saldo=total, 0 pagos.
5. Venta a crédito con abono inicial: PARTIAL, 1 PagoVenta(CONFIRMED), 1 CuentaPorCobrar con saldo=total-abono.
6. Venta a crédito sin cliente registrado: Rechazo con ValueError.
7. Sobrepago silencioso (pagos > total): Rechazo con ValueError.
8. Importes cero o negativos en pagos o detalles: Rechazo con ValueError.
9. Fallo FEFO con rollback: Si una línea no tiene stock, ninguna línea descuenta y no se crea venta.
10. Aislamiento multi-tenant: Rechazo al intentar asociar cliente o almacén de otro negocio.
11. Preservación estructurada de cliente_nit en columna formal.
12. Compatibilidad con la función POS legada crear_venta.
"""

from datetime import datetime, timezone, date
from decimal import Decimal
import uuid
import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.modules.auth.models import User, UserRole
from app.modules.inventario.models import Negocio, Almacen, Producto, Variante, Lote, Stock
from app.modules.ventas.models import Cliente, Venta, DetalleVenta, PagoVenta, CuentaPorCobrar, EstadoFinanciero, EstadoVenta
from app.modules.ventas.schemas import (
    VentaCreate, DetalleVentaCreate, PagoCreate,
)
from app.modules.ventas.services.sale_service import SaleService
from app.modules.ventas.service import crear_venta as crear_venta_legada


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
def setup_pos_env(db_session):
    """Configura negocio, almacén, usuario y productos con stock para pruebas POS."""
    db = db_session
    neg_id = str(uuid.uuid4())
    usr_id = str(uuid.uuid4())
    alm_id = str(uuid.uuid4())

    negocio = Negocio(
        id=neg_id,
        nombre="Comercial Apolo",
        propietario="Admin",
        moneda="BOB",
        activo=True,
    )
    db.add(negocio)

    usuario = User(
        id=usr_id,
        email="cajero@apolo.lat",
        password_hash="hash",
        nombre="Cajero Principal",
        negocio_id=neg_id,
        rol=UserRole.ADMIN.value,
        activo=True,
    )
    db.add(usuario)

    almacen = Almacen(
        id=alm_id,
        negocio_id=neg_id,
        nombre="Almacén Central",
        es_principal=True,
    )
    db.add(almacen)

    cliente = Cliente(
        id=str(uuid.uuid4()),
        negocio_id=neg_id,
        nombre="Juan Pérez",
        telefono="70012345",
        nit="1234567012",
        activo=True,
    )
    db.add(cliente)

    # Producto A: 100 unidades a Bs 50
    prod_a = Producto(
        id=str(uuid.uuid4()),
        negocio_id=neg_id,
        nombre="Leche Pil 1L",
        unidad_medida="unidad",
        activo=True,
    )
    db.add(prod_a)
    db.flush()

    var_a = Variante(
        id=str(uuid.uuid4()),
        producto_id=prod_a.id,
        atributos={},
        precio_venta=Decimal("50.00"),
        precio_costo=Decimal("35.00"),
        activa=True,
    )
    db.add(var_a)
    db.flush()

    lote_a = Lote(
        id=str(uuid.uuid4()),
        variante_id=var_a.id,
        almacen_id=alm_id,
        fecha_ingreso=datetime.now(timezone.utc),
        cantidad_inicial=Decimal("100"),
        cantidad_actual=Decimal("100"),
        costo_unitario=Decimal("35.00"),
        activo=True,
    )
    db.add(lote_a)

    stock_a = Stock(
        id=str(uuid.uuid4()),
        variante_id=var_a.id,
        almacen_id=alm_id,
        cantidad_actual=Decimal("100"),
    )
    db.add(stock_a)

    # Producto B: 5 unidades a Bs 20
    prod_b = Producto(
        id=str(uuid.uuid4()),
        negocio_id=neg_id,
        nombre="Galletas Oreo",
        unidad_medida="paquete",
        activo=True,
    )
    db.add(prod_b)
    db.flush()

    var_b = Variante(
        id=str(uuid.uuid4()),
        producto_id=prod_b.id,
        atributos={},
        precio_venta=Decimal("20.00"),
        precio_costo=Decimal("12.00"),
        activa=True,
    )
    db.add(var_b)
    db.flush()

    lote_b = Lote(
        id=str(uuid.uuid4()),
        variante_id=var_b.id,
        almacen_id=alm_id,
        fecha_ingreso=datetime.now(timezone.utc),
        cantidad_inicial=Decimal("5"),
        cantidad_actual=Decimal("5"),
        costo_unitario=Decimal("12.00"),
        activo=True,
    )
    db.add(lote_b)

    stock_b = Stock(
        id=str(uuid.uuid4()),
        variante_id=var_b.id,
        almacen_id=alm_id,
        cantidad_actual=Decimal("5"),
    )
    db.add(stock_b)

    db.commit()

    return {
        "negocio_id": neg_id,
        "usuario_id": usr_id,
        "almacen_id": alm_id,
        "cliente": cliente,
        "var_a": var_a,
        "var_b": var_b,
        "stock_a": stock_a,
        "stock_b": stock_b,
    }


def test_venta_contado_pura(db_session, setup_pos_env):
    """Venta contado pura: genera PAID, 1 PagoVenta(CONFIRMED), descuenta stock FEFO."""
    env = setup_pos_env
    db = db_session

    data = VentaCreate(
        almacen_id=env["almacen_id"],
        cliente_id=env["cliente"].id,
        cliente_nit="9876543",
        detalles=[
            DetalleVentaCreate(variante_id=env["var_a"].id, cantidad=Decimal("2"))
        ],
        pagos=[
            PagoCreate(monto=Decimal("100.00"), metodo_pago="EFECTIVO", estado="CONFIRMED")
        ],
        completar=True,
    )

    venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["usuario_id"], data)

    assert venta.total == Decimal("100.00")
    assert venta.estado == EstadoVenta.COMPLETADA.value
    assert venta.estado_financiero == EstadoFinanciero.PAID.value
    assert venta.cliente_nit == "9876543"
    assert len(venta.pagos) == 1
    assert venta.pagos[0].monto == Decimal("100.00")
    assert venta.pagos[0].estado == "CONFIRMED"
    assert venta.cuenta_cobrar is None

    # Stock descontado de 100 a 98
    stock = db.query(Stock).filter(Stock.variante_id == env["var_a"].id).first()
    assert stock.cantidad_actual == Decimal("98")


def test_venta_multi_pago_efectivo_qr(db_session, setup_pos_env):
    """Venta multi-pago (efectivo 60 + QR 40 = 100): genera PAID y 2 pagos confirmados."""
    env = setup_pos_env
    db = db_session

    data = VentaCreate(
        almacen_id=env["almacen_id"],
        detalles=[
            DetalleVentaCreate(variante_id=env["var_a"].id, cantidad=Decimal("2"))
        ],
        pagos=[
            PagoCreate(monto=Decimal("60.00"), metodo_pago="EFECTIVO", estado="CONFIRMED"),
            PagoCreate(monto=Decimal("40.00"), metodo_pago="QR", estado="CONFIRMED", referencia="BANCO-QR-99"),
        ],
        completar=True,
    )

    venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["usuario_id"], data)

    assert venta.total == Decimal("100.00")
    assert venta.metodo_pago == "MIXTO"
    assert venta.estado_financiero == EstadoFinanciero.PAID.value
    assert len(venta.pagos) == 2
    assert venta.cuenta_cobrar is None


def test_venta_con_qr_pending_resulta_en_partial(db_session, setup_pos_env):
    """Venta con QR en PENDING: no se computa como dinero cobrado, estado PARTIAL."""
    env = setup_pos_env
    db = db_session

    data = VentaCreate(
        almacen_id=env["almacen_id"],
        cliente_id=env["cliente"].id,
        es_credito=True,  # Permite saldo financiado/diferido
        detalles=[
            DetalleVentaCreate(variante_id=env["var_a"].id, cantidad=Decimal("2"))
        ],
        pagos=[
            PagoCreate(monto=Decimal("50.00"), metodo_pago="EFECTIVO", estado="CONFIRMED"),
            PagoCreate(monto=Decimal("50.00"), metodo_pago="QR", estado="PENDING", referencia="QR-PENDIENTE"),
        ],
        completar=True,
    )

    venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["usuario_id"], data)

    # Solo 50 están confirmados, por tanto es PARTIAL
    assert venta.total == Decimal("100.00")
    assert venta.estado_financiero == EstadoFinanciero.PARTIAL.value


def test_venta_credito_puro(db_session, setup_pos_env):
    """Venta a crédito puro: genera CREDIT, 1 CuentaPorCobrar con saldo=total, 0 pagos."""
    env = setup_pos_env
    db = db_session
    vencimiento = date(2026, 12, 31)

    data = VentaCreate(
        almacen_id=env["almacen_id"],
        cliente_id=env["cliente"].id,
        es_credito=True,
        fecha_vencimiento=vencimiento,
        detalles=[
            DetalleVentaCreate(variante_id=env["var_a"].id, cantidad=Decimal("2"))
        ],
        pagos=[],
        completar=True,
    )

    venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["usuario_id"], data)

    assert venta.total == Decimal("100.00")
    assert venta.estado_financiero == EstadoFinanciero.CREDIT.value
    assert len(venta.pagos) == 0
    assert venta.cuenta_cobrar is not None
    assert venta.cuenta_cobrar.monto_total_venta == Decimal("100.00")
    assert venta.cuenta_cobrar.monto_inicial_financiado == Decimal("100.00")
    assert venta.cuenta_cobrar.saldo_pendiente == Decimal("100.00")
    assert venta.cuenta_cobrar.fecha_vencimiento == vencimiento
    assert venta.cuenta_cobrar.estado == "PENDIENTE"


def test_venta_credito_con_abono_inicial(db_session, setup_pos_env):
    """Venta a crédito con anticipo: total 100, abono 30 -> saldo financiado 70, estado PARTIAL."""
    env = setup_pos_env
    db = db_session

    data = VentaCreate(
        almacen_id=env["almacen_id"],
        cliente_id=env["cliente"].id,
        es_credito=True,
        detalles=[
            DetalleVentaCreate(variante_id=env["var_a"].id, cantidad=Decimal("2"))
        ],
        pagos=[
            PagoCreate(monto=Decimal("30.00"), metodo_pago="EFECTIVO", estado="CONFIRMED")
        ],
        completar=True,
    )

    venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["usuario_id"], data)

    assert venta.total == Decimal("100.00")
    assert venta.estado_financiero == EstadoFinanciero.PARTIAL.value
    assert len(venta.pagos) == 1
    assert venta.pagos[0].monto == Decimal("30.00")
    assert venta.cuenta_cobrar is not None
    assert venta.cuenta_cobrar.monto_total_venta == Decimal("100.00")
    assert venta.cuenta_cobrar.monto_inicial_financiado == Decimal("70.00")
    assert venta.cuenta_cobrar.saldo_pendiente == Decimal("70.00")


def test_credito_sin_cliente_registrado_falla(db_session, setup_pos_env):
    """Venta con saldo a crédito sin cliente registrado debe fallar con ValueError."""
    env = setup_pos_env
    db = db_session

    data = VentaCreate(
        almacen_id=env["almacen_id"],
        cliente_id=None,
        es_credito=True,
        detalles=[
            DetalleVentaCreate(variante_id=env["var_a"].id, cantidad=Decimal("1"))
        ],
        pagos=[],
        completar=True,
    )

    with pytest.raises(ValueError, match="Para otorgar crédito es obligatorio seleccionar un cliente"):
        SaleService.crear_venta_financiera(db, env["negocio_id"], env["usuario_id"], data)


def test_sobrepago_silencioso_es_rechazado(db_session, setup_pos_env):
    """Si la suma de pagos supera el total, se rechaza inmediatamente."""
    env = setup_pos_env
    db = db_session

    data = VentaCreate(
        almacen_id=env["almacen_id"],
        detalles=[
            DetalleVentaCreate(variante_id=env["var_a"].id, cantidad=Decimal("1"))  # Total 50
        ],
        pagos=[
            PagoCreate(monto=Decimal("60.00"), metodo_pago="EFECTIVO", estado="CONFIRMED")  # 60 > 50
        ],
        completar=True,
    )

    with pytest.raises(ValueError, match="no puede superar el total"):
        SaleService.crear_venta_financiera(db, env["negocio_id"], env["usuario_id"], data)


def test_importes_cero_o_negativos_rechazados(db_session, setup_pos_env):
    """Pagos con importe 0 o negativo deben fallar."""
    env = setup_pos_env
    db = db_session

    with pytest.raises(Exception):
        # Validador Pydantic rechaza gt=0
        PagoCreate(monto=Decimal("0.00"), metodo_pago="EFECTIVO")

    with pytest.raises(Exception):
        PagoCreate(monto=Decimal("-10.00"), metodo_pago="EFECTIVO")


def test_fallo_fefo_rollback_atomico(db_session, setup_pos_env):
    """Si una línea falla por falta de stock, la transacción sufre rollback integral."""
    env = setup_pos_env
    db = db_session

    stock_a_antes = db.query(Stock).filter(Stock.variante_id == env["var_a"].id).first().cantidad_actual
    stock_b_antes = db.query(Stock).filter(Stock.variante_id == env["var_b"].id).first().cantidad_actual
    ventas_count_antes = db.query(Venta).count()

    data = VentaCreate(
        almacen_id=env["almacen_id"],
        detalles=[
            DetalleVentaCreate(variante_id=env["var_a"].id, cantidad=Decimal("1")),  # Hay 100 (OK)
            DetalleVentaCreate(variante_id=env["var_b"].id, cantidad=Decimal("10")), # Solo hay 5 (FALLA)
        ],
        pagos=[
            PagoCreate(monto=Decimal("250.00"), metodo_pago="EFECTIVO", estado="CONFIRMED")
        ],
        completar=True,
    )

    with pytest.raises(ValueError, match="Stock insuficiente"):
        SaleService.crear_venta_financiera(db, env["negocio_id"], env["usuario_id"], data)

    # Verificar que NADA mutó en la BD (rollback atómico)
    db.expire_all()
    assert db.query(Stock).filter(Stock.variante_id == env["var_a"].id).first().cantidad_actual == stock_a_antes
    assert db.query(Stock).filter(Stock.variante_id == env["var_b"].id).first().cantidad_actual == stock_b_antes
    assert db.query(Venta).count() == ventas_count_antes
    assert db.query(PagoVenta).count() == 0


def test_aislamiento_multitenant_cliente_ajeno(db_session, setup_pos_env):
    """Un tenant no puede asociar clientes de otro tenant."""
    env = setup_pos_env
    db = db_session

    otro_negocio = Negocio(
        id=str(uuid.uuid4()),
        nombre="Tienda Invasora",
        propietario="Otro",
        moneda="BOB",
        activo=True,
    )
    db.add(otro_negocio)

    cliente_ajeno = Cliente(
        id=str(uuid.uuid4()),
        negocio_id=otro_negocio.id,
        nombre="Cliente Espía",
        activo=True,
    )
    db.add(cliente_ajeno)
    db.commit()

    data = VentaCreate(
        almacen_id=env["almacen_id"],
        cliente_id=cliente_ajeno.id,
        es_credito=True,
        detalles=[
            DetalleVentaCreate(variante_id=env["var_a"].id, cantidad=Decimal("1"))
        ],
        pagos=[],
        completar=True,
    )

    with pytest.raises(ValueError, match="Cliente no encontrado, inactivo o no pertenece a su negocio"):
        SaleService.crear_venta_financiera(db, env["negocio_id"], env["usuario_id"], data)


def test_compatibilidad_con_pos_legado(db_session, setup_pos_env):
    """La función POS legada crear_venta sigue funcionando sin interrupción."""
    env = setup_pos_env
    db = db_session

    data = VentaCreate(
        almacen_id=env["almacen_id"],
        cliente_nombre="Cliente Mostrador Legado",
        metodo_pago="EFECTIVO",
        monto_recibido=Decimal("60.00"),
        detalles=[
            DetalleVentaCreate(variante_id=env["var_a"].id, cantidad=Decimal("1"))  # 50.00
        ],
        completar=True,
    )

    venta = crear_venta_legada(db, env["negocio_id"], data, env["usuario_id"])

    assert venta.total == Decimal("50.00")
    assert venta.cambio == Decimal("10.00")
    assert venta.estado == EstadoVenta.COMPLETADA.value
