"""
Tests unitarios e integrativos para SaleCancellationService (Bloque 2.4 — Cancelación Integral de Ventas).

Cubre los 15 escenarios obligatorios del dictamen técnico + Observación A (concurrencia y sobrepago de pagos QR pendientes):
1. Cancelación de venta sin pagos.
2. Cancelación de venta pagada en efectivo (con y sin devolución física de efectivo).
3. Cancelación de venta pagada por QR confirmado (reversión contable sin salida física de caja).
4. Cancelación de venta con pagos múltiples (genera N reversiones compensatorias independientes).
5. Cancelación de venta a crédito puro (anulación de cartera, saldo a 0, historial conservado).
6. Cancelación de venta a crédito con abonos parciales (reversión del abono y anulación de cartera).
7. Cancelación con reversiones parciales anteriores (solo revierte el saldo pendiente reversible).
8. Cancelación con pagos PENDING (los pagos PENDING pasan a FAILED y quedan bloqueados).
9. Rechazo de doble cancelación (error al intentar cancelar venta ya CANCELADA).
10. Rechazo de cancelación entre negocios (aislamiento multi-tenant / IDOR).
11. Devolución correcta de lotes y existencias (reingreso FEFO con tipo ENTRADA y motivo DEVOLUCION_VENTA).
12. Rollback integral cuando falla la devolución de inventario.
13. Rollback integral cuando falla una reversión financiera.
14. Conservación del historial financiero inmutable (append-only, cero eliminaciones).
15. Coherencia integral entre saldo, cuenta y estado financiero (CANCELADA y NOT_APPLICABLE).
16. Observación A: Control transaccional de pagos pendientes concurrentes contra sobrepago y bloqueo post-cancelación.
"""

from datetime import datetime, timezone, date, timedelta
from decimal import Decimal
import uuid
import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.modules.auth.models import User, UserRole
from app.modules.inventario.models import Negocio, Almacen, Producto, Variante, Lote, Stock, MovimientoStock
from app.modules.inventario.service import crear_movimiento
from app.modules.ventas.models import (
    Cliente, Venta, DetalleVenta, PagoVenta, ReversionPago, CuentaPorCobrar,
    EstadoFinanciero, EstadoVenta, EstadoCuentaCobrar, EstadoPago, MetodoPago,
)
from app.modules.ventas.schemas import (
    VentaCreate, DetalleVentaCreate, PagoCreate,
)
from app.modules.ventas.services.sale_service import SaleService
from app.modules.ventas.services.payment_service import PaymentService
from app.modules.ventas.services.accounts_receivable_service import AccountsReceivableService
from app.modules.ventas.services.sale_cancellation_service import SaleCancellationService


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
def cancel_env(db_session):
    """
    Configura un entorno de pruebas completo para cancelación de ventas:
    - Negocio "Apolo Hardware"
    - Usuario ADMIN y Usuario CAJERO
    - Almacén Central
    - Cliente registrado
    - Productos y Lotes con stock disponible
    """
    db = db_session
    neg_id = str(uuid.uuid4())
    usr_admin_id = str(uuid.uuid4())
    usr_cajero_id = str(uuid.uuid4())
    alm_id = str(uuid.uuid4())

    negocio = Negocio(
        id=neg_id,
        nombre="Apolo Hardware",
        propietario="Dueño Apolo",
        moneda="BOB",
        activo=True,
    )
    db.add(negocio)

    admin = User(
        id=usr_admin_id,
        email="admin_canc@apolo.lat",
        password_hash="hash",
        nombre="Administrador General",
        negocio_id=neg_id,
        rol=UserRole.ADMIN.value,
        activo=True,
    )
    cajero = User(
        id=usr_cajero_id,
        email="cajero_canc@apolo.lat",
        password_hash="hash",
        nombre="Cajero Turno",
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

    cliente = Cliente(
        id=str(uuid.uuid4()),
        negocio_id=neg_id,
        nombre="Empresa Constructora Siglo XXI",
        telefono="77889900",
        nit="900800700",
        activo=True,
    )
    db.add(cliente)

    prod = Producto(
        id=str(uuid.uuid4()),
        negocio_id=neg_id,
        nombre="Taladro Percutor Industrial",
        unidad_medida="unidad",
        activo=True,
    )
    db.add(prod)
    db.flush()

    var = Variante(
        id=str(uuid.uuid4()),
        producto_id=prod.id,
        sku="TAL-PERC-01",
        precio_venta=Decimal("500.00"),
        precio_costo=Decimal("350.00"),
        atributos={"potencia": "850W"},
        activa=True,
    )
    db.add(var)
    db.flush()

    # Lote 1 con 10 unidades
    lote1 = Lote(
        id=str(uuid.uuid4()),
        variante_id=var.id,
        almacen_id=alm_id,
        codigo_lote="LOT-001",
        fecha_ingreso=datetime.now(timezone.utc),
        cantidad_inicial=Decimal("10"),
        cantidad_actual=Decimal("10"),
        costo_unitario=Decimal("350.00"),
        activo=True,
    )
    # Lote 2 con 10 unidades
    lote2 = Lote(
        id=str(uuid.uuid4()),
        variante_id=var.id,
        almacen_id=alm_id,
        codigo_lote="LOT-002",
        fecha_ingreso=datetime.now(timezone.utc) + timedelta(minutes=5),
        cantidad_inicial=Decimal("10"),
        cantidad_actual=Decimal("10"),
        costo_unitario=Decimal("360.00"),
        activo=True,
    )
    db.add_all([lote1, lote2])
    db.flush()

    stock = Stock(
        id=str(uuid.uuid4()),
        variante_id=var.id,
        almacen_id=alm_id,
        cantidad_actual=Decimal("20.000"),
    )
    db.add(stock)
    db.commit()

    return {
        "negocio_id": neg_id,
        "admin_id": usr_admin_id,
        "cajero_id": usr_cajero_id,
        "almacen_id": alm_id,
        "cliente_id": cliente.id,
        "producto_id": prod.id,
        "variante_id": var.id,
        "lote1_id": lote1.id,
        "lote2_id": lote2.id,
    }


# ===========================================================================
# 1. Cancelación de venta sin pagos
# ===========================================================================
def test_01_cancelacion_venta_sin_pagos(cancel_env, db_session):
    db = db_session
    env = cancel_env

    # Venta completada sin pagos inmediatos (ej. orden pendiente o crédito sin anticipo)
    data = VentaCreate(
        almacen_id=env["almacen_id"],
        cliente_id=env["cliente_id"],
        detalles=[
            DetalleVentaCreate(
                variante_id=env["variante_id"],
                cantidad=Decimal("2.000"),
                precio_unitario=Decimal("500.00"),
            )
        ],
        es_credito=True,
        pagos=[],
        completar=True,
    )
    venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["cajero_id"], data)
    assert venta.estado == EstadoVenta.COMPLETADA.value

    # Stock disminuyó a 18
    stock = db.query(Stock).filter(Stock.variante_id == env["variante_id"]).first()
    assert stock.cantidad_actual == Decimal("18.000")

    # Cancelar la venta
    res = SaleCancellationService.cancelar_venta(
        db, env["negocio_id"], venta.id, env["admin_id"], motivo="Error de digitación por el cliente"
    )

    db.refresh(venta)
    assert venta.estado == EstadoVenta.CANCELADA.value
    assert venta.estado_financiero == EstadoFinanciero.NOT_APPLICABLE.value
    assert venta.cancelled_at is not None
    assert res["reversiones_generadas"] == 0
    assert res["movimientos_inventario_reingresados"] >= 1

    # Stock reingresado a 20
    db.refresh(stock)
    assert stock.cantidad_actual == Decimal("20.000")


# ===========================================================================
# 2. Cancelación de venta pagada en efectivo (con y sin devolución física)
# ===========================================================================
def test_02_cancelacion_venta_efectivo_con_devolucion_fisica(cancel_env, db_session):
    db = db_session
    env = cancel_env

    data = VentaCreate(
        almacen_id=env["almacen_id"],
        cliente_id=env["cliente_id"],
        detalles=[
            DetalleVentaCreate(
                variante_id=env["variante_id"],
                cantidad=Decimal("1.000"),
                precio_unitario=Decimal("500.00"),
            )
        ],
        metodo_pago="EFECTIVO",
        monto_recibido=Decimal("500.00"),
        completar=True,
    )
    venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["cajero_id"], data)
    pago = venta.pagos[0]
    assert pago.estado == EstadoPago.CONFIRMED.value

    # Cancelar venta indicando que se devolvió efectivo físicamente al cliente
    res = SaleCancellationService.cancelar_venta(
        db, env["negocio_id"], venta.id, env["admin_id"],
        motivo="Cliente desistió y se le devolvió el dinero en caja",
        devolver_efectivo=True
    )

    assert res["reversiones_generadas"] == 1
    assert res["es_devolucion_efectivo"] is True
    assert res["total_revertido"] == Decimal("500.00")

    db.refresh(venta)
    assert venta.estado == EstadoVenta.CANCELADA.value
    assert venta.estado_financiero == EstadoFinanciero.NOT_APPLICABLE.value

    # Inmutabilidad: PagoVenta sigue intacto
    db.refresh(pago)
    assert pago.monto == Decimal("500.00")
    assert pago.estado == EstadoPago.CONFIRMED.value
    assert pago.total_revertido == Decimal("500.00")
    assert len(pago.reversiones) == 1
    assert pago.reversiones[0].es_devolucion_efectivo is True


# ===========================================================================
# 3. Cancelación de venta pagada por QR confirmado (sin salida física de caja)
# ===========================================================================
def test_03_cancelacion_venta_qr_confirmado_sin_salida_fisica(cancel_env, db_session):
    db = db_session
    env = cancel_env

    data = VentaCreate(
        almacen_id=env["almacen_id"],
        cliente_id=env["cliente_id"],
        detalles=[
            DetalleVentaCreate(
                variante_id=env["variante_id"],
                cantidad=Decimal("1.000"),
                precio_unitario=Decimal("500.00"),
            )
        ],
        pagos=[
            PagoCreate(monto=Decimal("500.00"), metodo_pago="QR", estado="CONFIRMED", referencia="QR-TRANS-99")
        ],
        completar=True,
    )
    venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["cajero_id"], data)

    # Cancelar solicitando devolver_efectivo=True; como el pago es QR, NO debe marcarse como salida de efectivo
    res = SaleCancellationService.cancelar_venta(
        db, env["negocio_id"], venta.id, env["admin_id"],
        motivo="Reversión bancaria por anulación de compra QR",
        devolver_efectivo=True
    )

    assert res["reversiones_generadas"] == 1
    assert res["es_devolucion_efectivo"] is False  # Regla H-15: QR no es efectivo de caja

    db.refresh(venta)
    pago = venta.pagos[0]
    assert len(pago.reversiones) == 1
    assert pago.reversiones[0].es_devolucion_efectivo is False
    assert pago.reversiones[0].monto_revertido == Decimal("500.00")


# ===========================================================================
# 4. Cancelación de venta con pagos múltiples
# ===========================================================================
def test_04_cancelacion_venta_pagos_multiples(cancel_env, db_session):
    db = db_session
    env = cancel_env

    # Venta de Bs 1.000 con 3 pagos: 300 Efectivo, 400 QR, 300 Tarjeta
    data = VentaCreate(
        almacen_id=env["almacen_id"],
        cliente_id=env["cliente_id"],
        detalles=[
            DetalleVentaCreate(
                variante_id=env["variante_id"],
                cantidad=Decimal("2.000"),
                precio_unitario=Decimal("500.00"),
            )
        ],
        pagos=[
            PagoCreate(monto=Decimal("300.00"), metodo_pago="EFECTIVO", estado="CONFIRMED", referencia="EF-01"),
            PagoCreate(monto=Decimal("400.00"), metodo_pago="QR", estado="CONFIRMED", referencia="QR-01"),
            PagoCreate(monto=Decimal("300.00"), metodo_pago="TARJETA", estado="CONFIRMED", referencia="POS-01"),
        ],
        completar=True,
    )
    venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["cajero_id"], data)
    assert len(venta.pagos) == 3

    res = SaleCancellationService.cancelar_venta(
        db, env["negocio_id"], venta.id, env["admin_id"],
        motivo="Cancelación de venta múltiple",
        devolver_efectivo=True
    )

    # 3 reversiones compensatorias independientes
    assert res["reversiones_generadas"] == 3
    assert res["total_revertido"] == Decimal("1000.00")

    db.refresh(venta)
    assert venta.estado == EstadoVenta.CANCELADA.value
    assert venta.estado_financiero == EstadoFinanciero.NOT_APPLICABLE.value

    # Verificar que el pago en efectivo sí tuvo salida física y los otros dos no
    for p in venta.pagos:
        assert len(p.reversiones) == 1
        rev = p.reversiones[0]
        if p.metodo_pago == "EFECTIVO":
            assert rev.es_devolucion_efectivo is True
            assert rev.monto_revertido == Decimal("300.00")
        elif p.metodo_pago == "QR":
            assert rev.es_devolucion_efectivo is False
            assert rev.monto_revertido == Decimal("400.00")
        elif p.metodo_pago == "TARJETA":
            assert rev.es_devolucion_efectivo is False
            assert rev.monto_revertido == Decimal("300.00")


# ===========================================================================
# 5. Cancelación de venta a crédito puro
# ===========================================================================
def test_05_cancelacion_venta_credito_puro(cancel_env, db_session):
    db = db_session
    env = cancel_env

    data = VentaCreate(
        almacen_id=env["almacen_id"],
        cliente_id=env["cliente_id"],
        detalles=[
            DetalleVentaCreate(
                variante_id=env["variante_id"],
                cantidad=Decimal("1.000"),
                precio_unitario=Decimal("500.00"),
            )
        ],
        es_credito=True,
        fecha_vencimiento=date.today() + timedelta(days=15),
        pagos=[],
        completar=True,
    )
    venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["cajero_id"], data)
    cxc = venta.cuenta_cobrar
    assert cxc is not None
    assert cxc.estado == EstadoCuentaCobrar.PENDIENTE.value
    assert cxc.saldo_pendiente == Decimal("500.00")

    res = SaleCancellationService.cancelar_venta(
        db, env["negocio_id"], venta.id, env["admin_id"], motivo="Anulación de crédito aprobado"
    )

    assert res["cuenta_cobrar_cancelada"] is True
    assert res["reversiones_generadas"] == 0

    db.refresh(cxc)
    assert cxc.estado == EstadoCuentaCobrar.CANCELADA.value
    assert cxc.saldo_pendiente == Decimal("0.00")
    assert "[CANCELADA" in cxc.notas

    # No debe aparecer como cartera activa exigible
    cartera_activa = AccountsReceivableService.consultar_cartera(db, env["negocio_id"])
    ids_en_cartera = [c["id"] for c in cartera_activa]
    assert cxc.id not in ids_en_cartera


# ===========================================================================
# 6. Cancelación con abonos parciales
# ===========================================================================
def test_06_cancelacion_con_abonos_parciales(cancel_env, db_session):
    db = db_session
    env = cancel_env

    # Venta a crédito de Bs 500
    data = VentaCreate(
        almacen_id=env["almacen_id"],
        cliente_id=env["cliente_id"],
        detalles=[
            DetalleVentaCreate(
                variante_id=env["variante_id"],
                cantidad=Decimal("1.000"),
                precio_unitario=Decimal("500.00"),
            )
        ],
        es_credito=True,
        pagos=[],
        completar=True,
    )
    venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["cajero_id"], data)
    cxc = venta.cuenta_cobrar

    # Se registra un abono de Bs 200 en efectivo
    abono = AccountsReceivableService.registrar_abono(
        db, env["negocio_id"], cxc.id, env["cajero_id"],
        monto=Decimal("200.00"), metodo_pago="EFECTIVO"
    )
    assert abono["saldo_operativo_nuevo"] == Decimal("300.00")

    # Cancelar la venta
    res = SaleCancellationService.cancelar_venta(
        db, env["negocio_id"], venta.id, env["admin_id"],
        motivo="Cliente devuelve producto a crédito y se le reintegra el abono",
        devolver_efectivo=True
    )

    assert res["reversiones_generadas"] == 1
    assert res["total_revertido"] == Decimal("200.00")
    assert res["cuenta_cobrar_cancelada"] is True

    db.refresh(cxc)
    assert cxc.estado == EstadoCuentaCobrar.CANCELADA.value
    assert cxc.saldo_pendiente == Decimal("0.00")


# ===========================================================================
# 7. Cancelación con reversiones parciales anteriores
# ===========================================================================
def test_07_cancelacion_con_reversiones_parciales_anteriores(cancel_env, db_session):
    db = db_session
    env = cancel_env

    data = VentaCreate(
        almacen_id=env["almacen_id"],
        cliente_id=env["cliente_id"],
        detalles=[
            DetalleVentaCreate(
                variante_id=env["variante_id"],
                cantidad=Decimal("1.000"),
                precio_unitario=Decimal("500.00"),
            )
        ],
        metodo_pago="EFECTIVO",
        monto_recibido=Decimal("500.00"),
        completar=True,
    )
    venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["cajero_id"], data)
    pago = venta.pagos[0]

    # Pre-reversión parcial de Bs 150
    PaymentService.revertir_pago(
        db, env["negocio_id"], pago.id, env["admin_id"],
        monto_a_revertir=Decimal("150.00"), motivo="Ajuste previo por descuento comercial"
    )
    db.refresh(pago)
    assert pago.total_revertido == Decimal("150.00")
    assert pago.saldo_reversible == Decimal("350.00")

    # Cancelar la venta completa
    res = SaleCancellationService.cancelar_venta(
        db, env["negocio_id"], venta.id, env["admin_id"],
        motivo="Anulación final de la venta",
        devolver_efectivo=True
    )

    # Solo debe revertir el saldo restante (Bs 350)
    assert res["total_revertido"] == Decimal("350.00")
    assert res["reversiones_generadas"] == 1

    db.refresh(pago)
    assert pago.total_revertido == Decimal("500.00")
    assert pago.saldo_reversible == Decimal("0.00")
    assert len(pago.reversiones) == 2


# ===========================================================================
# 8. Cancelación con pagos PENDING
# ===========================================================================
def test_08_cancelacion_con_pagos_pending(cancel_env, db_session):
    db = db_session
    env = cancel_env

    # Venta con pago QR que nace PENDING
    data = VentaCreate(
        almacen_id=env["almacen_id"],
        cliente_id=env["cliente_id"],
        detalles=[
            DetalleVentaCreate(
                variante_id=env["variante_id"],
                cantidad=Decimal("1.000"),
                precio_unitario=Decimal("500.00"),
            )
        ],
        pagos=[
            PagoCreate(monto=Decimal("500.00"), metodo_pago="QR", estado="PENDING", referencia="QR-ESPERA-01")
        ],
        completar=True,
    )
    venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["cajero_id"], data)
    pago_pending = venta.pagos[0]
    assert pago_pending.estado == EstadoPago.PENDING.value

    # Cancelar la venta
    res = SaleCancellationService.cancelar_venta(
        db, env["negocio_id"], venta.id, env["admin_id"], motivo="Cliente no concretó el QR"
    )

    assert res["pagos_pendientes_cancelados"] == 1
    assert res["reversiones_generadas"] == 0  # No genera reversión de plata no cobrada

    db.refresh(pago_pending)
    assert pago_pending.estado == EstadoPago.FAILED.value
    assert "CANCELADO: Venta anulada" in pago_pending.referencia

    # Intento de confirmación posterior DEBE fallar
    with pytest.raises(ValueError, match="No se puede confirmar un pago en estado fallido"):
        PaymentService.confirmar_pago(db, env["negocio_id"], pago_pending.id, env["admin_id"])


# ===========================================================================
# 9. Rechazo de doble cancelación
# ===========================================================================
def test_09_rechazo_doble_cancelacion(cancel_env, db_session):
    db = db_session
    env = cancel_env

    data = VentaCreate(
        almacen_id=env["almacen_id"],
        detalles=[
            DetalleVentaCreate(
                variante_id=env["variante_id"],
                cantidad=Decimal("1.000"),
                precio_unitario=Decimal("500.00"),
            )
        ],
        metodo_pago="EFECTIVO",
        monto_recibido=Decimal("500.00"),
        completar=True,
    )
    venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["cajero_id"], data)

    # Primera cancelación
    SaleCancellationService.cancelar_venta(
        db, env["negocio_id"], venta.id, env["admin_id"], motivo="Cancelación inicial"
    )

    # Segunda cancelación idéntica DEBE fallar
    with pytest.raises(ValueError, match="La venta ya se encuentra cancelada"):
        SaleCancellationService.cancelar_venta(
            db, env["negocio_id"], venta.id, env["admin_id"], motivo="Intento de re-cancelar"
        )


# ===========================================================================
# 10. Rechazo de cancelación entre negocios (Multi-tenant IDOR)
# ===========================================================================
def test_10_rechazo_cancelacion_entre_negocios_multi_tenant(cancel_env, db_session):
    db = db_session
    env = cancel_env

    data = VentaCreate(
        almacen_id=env["almacen_id"],
        detalles=[
            DetalleVentaCreate(
                variante_id=env["variante_id"],
                cantidad=Decimal("1.000"),
                precio_unitario=Decimal("500.00"),
            )
        ],
        metodo_pago="EFECTIVO",
        monto_recibido=Decimal("500.00"),
        completar=True,
    )
    venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["cajero_id"], data)

    # Tenant usurpador
    negocio_intruso_id = str(uuid.uuid4())
    admin_intruso_id = str(uuid.uuid4())

    with pytest.raises(ValueError, match="Venta no encontrada o no pertenece a su negocio"):
        SaleCancellationService.cancelar_venta(
            db, negocio_intruso_id, venta.id, admin_intruso_id, motivo="Intento no autorizado"
        )


# ===========================================================================
# 11. Devolución correcta de lotes y existencias
# ===========================================================================
def test_11_devolucion_correcta_lotes_y_existencias(cancel_env, db_session):
    db = db_session
    env = cancel_env

    lote1 = db.query(Lote).filter(Lote.id == env["lote1_id"]).first()
    lote2 = db.query(Lote).filter(Lote.id == env["lote2_id"]).first()
    stock = db.query(Stock).filter(Stock.variante_id == env["variante_id"]).first()

    assert lote1.cantidad_actual == Decimal("10")
    assert lote2.cantidad_actual == Decimal("10")
    assert stock.cantidad_actual == Decimal("20.000")

    # Venta de 15 unidades: FEFO consume 10 de Lote 1 y 5 de Lote 2
    data = VentaCreate(
        almacen_id=env["almacen_id"],
        detalles=[
            DetalleVentaCreate(
                variante_id=env["variante_id"],
                cantidad=Decimal("15.000"),
                precio_unitario=Decimal("500.00"),
            )
        ],
        metodo_pago="EFECTIVO",
        monto_recibido=Decimal("7500.00"),
        completar=True,
    )
    venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["cajero_id"], data)

    db.refresh(lote1)
    db.refresh(lote2)
    db.refresh(stock)
    assert lote1.cantidad_actual == Decimal("0")
    assert lote2.cantidad_actual == Decimal("5")
    assert stock.cantidad_actual == Decimal("5.000")

    # Cancelar la venta
    SaleCancellationService.cancelar_venta(
        db, env["negocio_id"], venta.id, env["admin_id"], motivo="Devolución completa de mercancía"
    )

    db.refresh(lote1)
    db.refresh(lote2)
    db.refresh(stock)

    # Lotes restaurados exactamente a su cantidad original
    assert lote1.cantidad_actual == Decimal("10")
    assert lote2.cantidad_actual == Decimal("10")
    assert stock.cantidad_actual == Decimal("20.000")

    # Verificar que los movimientos de reingreso tengan tipo ENTRADA y motivo DEVOLUCION_VENTA
    movs = db.query(MovimientoStock).filter(
        MovimientoStock.referencia_id == str(venta.id),
        MovimientoStock.tipo == "ENTRADA"
    ).all()
    assert len(movs) == 2
    for m in movs:
        assert m.tipo == "ENTRADA"
        assert "DEVOLUCION_VENTA" in m.motivo


# ===========================================================================
# 12. Rollback integral cuando falla la devolución de inventario
# ===========================================================================
def test_12_rollback_cuando_falla_devolucion_inventario(cancel_env, db_session, monkeypatch):
    db = db_session
    env = cancel_env

    data = VentaCreate(
        almacen_id=env["almacen_id"],
        detalles=[
            DetalleVentaCreate(
                variante_id=env["variante_id"],
                cantidad=Decimal("1.000"),
                precio_unitario=Decimal("500.00"),
            )
        ],
        metodo_pago="EFECTIVO",
        monto_recibido=Decimal("500.00"),
        completar=True,
    )
    venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["cajero_id"], data)

    # Simular fallo en crear_movimiento
    def mock_crear_movimiento(*args, **kwargs):
        raise RuntimeError("Fallo forzado en el subsistema de inventario")

    monkeypatch.setattr("app.modules.ventas.services.sale_cancellation_service.crear_movimiento", mock_crear_movimiento)

    with pytest.raises(RuntimeError, match="Fallo forzado en el subsistema de inventario"):
        SaleCancellationService.cancelar_venta(
            db, env["negocio_id"], venta.id, env["admin_id"], motivo="Test rollback"
        )

    # Estado de la venta y los pagos debe permanecer intacto
    db.refresh(venta)
    assert venta.estado == EstadoVenta.COMPLETADA.value
    assert venta.estado_financiero == EstadoFinanciero.PAID.value
    pago = venta.pagos[0]
    db.refresh(pago)
    assert pago.total_revertido == Decimal("0.00")
    assert len(pago.reversiones) == 0


# ===========================================================================
# 13. Rollback integral cuando falla una reversión financiera
# ===========================================================================
def test_13_rollback_cuando_falla_reversion_financiera(cancel_env, db_session, monkeypatch):
    db = db_session
    env = cancel_env

    data = VentaCreate(
        almacen_id=env["almacen_id"],
        detalles=[
            DetalleVentaCreate(
                variante_id=env["variante_id"],
                cantidad=Decimal("1.000"),
                precio_unitario=Decimal("500.00"),
            )
        ],
        metodo_pago="EFECTIVO",
        monto_recibido=Decimal("500.00"),
        completar=True,
    )
    venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["cajero_id"], data)

    # Simular fallo en PaymentService.revertir_pago
    def mock_revertir_pago(*args, **kwargs):
        raise RuntimeError("Fallo forzado en pasarela financiera de reversión")

    monkeypatch.setattr(PaymentService, "revertir_pago", mock_revertir_pago)

    with pytest.raises(RuntimeError, match="Fallo forzado en pasarela financiera de reversión"):
        SaleCancellationService.cancelar_venta(
            db, env["negocio_id"], venta.id, env["admin_id"], motivo="Test rollback reversión"
        )

    # Estado permanece completado y stock no se reingresó
    db.refresh(venta)
    assert venta.estado == EstadoVenta.COMPLETADA.value
    stock = db.query(Stock).filter(Stock.variante_id == env["variante_id"]).first()
    assert stock.cantidad_actual == Decimal("19.000")


# ===========================================================================
# 14. Conservación del historial financiero inmutable
# ===========================================================================
def test_14_conservacion_historial_financiero_inmutable(cancel_env, db_session):
    db = db_session
    env = cancel_env

    data = VentaCreate(
        almacen_id=env["almacen_id"],
        cliente_id=env["cliente_id"],
        detalles=[
            DetalleVentaCreate(
                variante_id=env["variante_id"],
                cantidad=Decimal("1.000"),
                precio_unitario=Decimal("500.00"),
            )
        ],
        metodo_pago="EFECTIVO",
        monto_recibido=Decimal("500.00"),
        completar=True,
    )
    venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["cajero_id"], data)
    pago_id = venta.pagos[0].id

    SaleCancellationService.cancelar_venta(
        db, env["negocio_id"], venta.id, env["admin_id"], motivo="Auditoría inmutable"
    )

    # El registro de pago original sigue existiendo (append-only)
    pago_db = db.query(PagoVenta).filter(PagoVenta.id == pago_id).first()
    assert pago_db is not None
    assert pago_db.monto == Decimal("500.00")

    # Se conserva la trazabilidad a través de consultar_auditoria_cancelacion
    auditoria = SaleCancellationService.consultar_auditoria_cancelacion(db, env["negocio_id"], venta.id)
    assert auditoria["venta_id"] == venta.id
    assert len(auditoria["reversiones"]) == 1
    assert len(auditoria["movimientos_reingreso"]) == 1


# ===========================================================================
# 15. Coherencia entre saldo, cuenta y estado financiero
# ===========================================================================
def test_15_coherencia_saldo_cuenta_y_estado_financiero(cancel_env, db_session):
    db = db_session
    env = cancel_env

    data = VentaCreate(
        almacen_id=env["almacen_id"],
        cliente_id=env["cliente_id"],
        detalles=[
            DetalleVentaCreate(
                variante_id=env["variante_id"],
                cantidad=Decimal("1.000"),
                precio_unitario=Decimal("500.00"),
            )
        ],
        es_credito=True,
        pagos=[],
        completar=True,
    )
    venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["cajero_id"], data)
    cxc_id = venta.cuenta_cobrar.id

    SaleCancellationService.cancelar_venta(
        db, env["negocio_id"], venta.id, env["admin_id"], motivo="Validación de coherencia"
    )

    db.refresh(venta)
    assert venta.estado == EstadoVenta.CANCELADA.value
    assert venta.estado_financiero == EstadoFinanciero.NOT_APPLICABLE.value

    # Consultar detalle de la cuenta por cobrar vía servicio
    cuenta_detalle = AccountsReceivableService.consultar_cuenta_venta(db, env["negocio_id"], venta.id)
    assert cuenta_detalle is not None
    assert cuenta_detalle["estado"] == EstadoCuentaCobrar.CANCELADA.value
    assert cuenta_detalle["saldo_operativo"] == Decimal("0.00")
    assert cuenta_detalle["saldo_pendiente"] == Decimal("0.00")
    assert cuenta_detalle["esta_vencida"] is False
    assert cuenta_detalle["dias_mora"] == 0


# ===========================================================================
# 16. Observación A: Control transaccional de pagos pendientes concurrentes
# ===========================================================================
def test_16_observacion_a_concurrencia_pagos_pending_y_sobrepago(cancel_env, db_session):
    db = db_session
    env = cancel_env

    # Venta de Bs 1.000 a crédito
    data = VentaCreate(
        almacen_id=env["almacen_id"],
        cliente_id=env["cliente_id"],
        detalles=[
            DetalleVentaCreate(
                variante_id=env["variante_id"],
                cantidad=Decimal("2.000"),
                precio_unitario=Decimal("500.00"),
            )
        ],
        es_credito=True,
        pagos=[],
        completar=True,
    )
    venta = SaleService.crear_venta_financiera(db, env["negocio_id"], env["cajero_id"], data)
    assert venta.total == Decimal("1000.00")

    # Registro de dos pagos QR pendientes por Bs 700 cada uno (simulando dos clientes escaneando)
    pago_qr1 = PaymentService.registrar_pago(
        db, env["negocio_id"], venta.id, env["cajero_id"],
        monto=Decimal("700.00"), metodo_pago="QR", referencia="QR-CONC-1"
    )
    pago_qr2 = PaymentService.registrar_pago(
        db, env["negocio_id"], venta.id, env["cajero_id"],
        monto=Decimal("700.00"), metodo_pago="QR", referencia="QR-CONC-2"
    )
    assert pago_qr1.estado == EstadoPago.PENDING.value
    assert pago_qr2.estado == EstadoPago.PENDING.value

    # Confirmar el primer pago QR por Bs 700 (saldo pendiente restante era 1.000 -> pasa a 300)
    pago_confirmado1 = PaymentService.confirmar_pago(
        db, env["negocio_id"], pago_qr1.id, env["admin_id"], referencia_confirmacion="BANCO-001"
    )
    assert pago_confirmado1.estado == EstadoPago.CONFIRMED.value

    # Intentar confirmar el segundo pago QR por Bs 700:
    # Como el saldo operativo restante es Bs 300, DEBE ser rechazado transaccionalmente por sobrepago (Observación A)
    with pytest.raises(ValueError, match="supera el saldo operativo pendiente"):
        PaymentService.confirmar_pago(
            db, env["negocio_id"], pago_qr2.id, env["admin_id"], referencia_confirmacion="BANCO-002"
        )

    # Ahora cancelamos la venta
    SaleCancellationService.cancelar_venta(
        db, env["negocio_id"], venta.id, env["admin_id"], motivo="Cancelación tras intento fallido de sobrepago"
    )

    db.refresh(pago_qr1)
    db.refresh(pago_qr2)
    db.refresh(venta)

    # El pago 1 confirmado fue revertido
    assert pago_qr1.total_revertido == Decimal("700.00")
    # El pago 2 pendiente pasó a FAILED
    assert pago_qr2.estado == EstadoPago.FAILED.value
    # La venta está CANCELADA y NOT_APPLICABLE
    assert venta.estado == EstadoVenta.CANCELADA.value
    assert venta.estado_financiero == EstadoFinanciero.NOT_APPLICABLE.value

    # Intentar confirmar el pago 2 post-cancelación falla
    with pytest.raises(ValueError, match="No se puede confirmar un pago en estado fallido"):
        PaymentService.confirmar_pago(
            db, env["negocio_id"], pago_qr2.id, env["admin_id"]
        )
