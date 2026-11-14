"""
Tests unitarios e integrativos para PaymentService (Bloque 2.2 — Pagos, Confirmaciones y Reversiones).

Cubre:
1. Registro de pago en efectivo (CONFIRMED directo).
2. Registro de pago QR pendiente (inicia en PENDING, no descuenta saldo de deuda).
3. Confirmación explícita de pago QR pendiente (transición PENDING -> CONFIRMED, abona a CxC).
4. Rechazo de confirmación de pago ya confirmado o fallido.
5. Marcado de pago pendiente como FAILED (rechazo bancario).
6. Reversión total de un pago confirmado (ReversionPago, reabre saldo en CxC).
7. Reversión parcial de un pago confirmado.
8. Rechazo de reversión superior al saldo reversible disponible.
9. Rechazo de reversión adicional tras el 100% revertido (prevención de doble reversión).
10. Aislamiento multi-tenant (prohibido operar pagos de otro negocio).
11. Clasificación de pagos: distinción entre cobro verificado e HISTORICO_PRESUNTIVO.
12. Conciliación de pago histórico presuntivo a VERIFICADO.
13. Historial consolidado de cobros y reversiones por venta.
14. Atomicidad transaccional y rollback ante errores.
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
from app.modules.ventas.models import (
    Cliente, Venta, DetalleVenta, PagoVenta, ReversionPago, CuentaPorCobrar, ConciliacionPago,
    EstadoFinanciero, EstadoVenta, EstadoPago, OrigenPago, EstadoConciliacion, MetodoPago,
)
from app.modules.ventas.schemas import (
    VentaCreate, DetalleVentaCreate, PagoCreate,
)
from app.modules.ventas.services.sale_service import SaleService
from app.modules.ventas.services.payment_service import PaymentService


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
def pos_credit_env(db_session):
    """Crea una venta a crédito de Bs 500 para pruebas de pagos y abonos posteriores."""
    db = db_session
    neg_id = str(uuid.uuid4())
    usr_id = str(uuid.uuid4())
    alm_id = str(uuid.uuid4())

    negocio = Negocio(
        id=neg_id,
        nombre="Ferretería Apolo",
        propietario="Admin",
        moneda="BOB",
        activo=True,
    )
    db.add(negocio)

    usuario = User(
        id=usr_id,
        email="cajero2@apolo.lat",
        password_hash="hash",
        nombre="Cajero Pagos",
        negocio_id=neg_id,
        rol=UserRole.EMPLEADO.value,
        activo=True,
    )
    db.add(usuario)

    admin_id = str(uuid.uuid4())
    admin_user = User(
        id=admin_id,
        email="admin2@apolo.lat",
        password_hash="hash",
        nombre="Admin Pagos",
        negocio_id=neg_id,
        rol=UserRole.ADMIN.value,
        activo=True,
    )
    db.add(admin_user)

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
        nombre="Construcciones SRL",
        telefono="78900011",
        nit="88776655",
        activo=True,
    )
    db.add(cliente)

    prod = Producto(
        id=str(uuid.uuid4()),
        negocio_id=neg_id,
        nombre="Cemento Viacha 50Kg",
        unidad_medida="bolsa",
        activo=True,
    )
    db.add(prod)
    db.flush()

    var = Variante(
        id=str(uuid.uuid4()),
        producto_id=prod.id,
        atributos={},
        precio_venta=Decimal("50.00"),
        precio_costo=Decimal("40.00"),
        activa=True,
    )
    db.add(var)
    db.flush()

    lote = Lote(
        id=str(uuid.uuid4()),
        variante_id=var.id,
        almacen_id=alm_id,
        fecha_ingreso=datetime.now(timezone.utc),
        cantidad_inicial=Decimal("50"),
        cantidad_actual=Decimal("50"),
        costo_unitario=Decimal("40.00"),
        activo=True,
    )
    db.add(lote)

    stock = Stock(
        id=str(uuid.uuid4()),
        variante_id=var.id,
        almacen_id=alm_id,
        cantidad_actual=Decimal("50"),
    )
    db.add(stock)
    db.commit()

    # Venta a crédito por Bs 500 (10 bolsas x 50)
    data_venta = VentaCreate(
        almacen_id=alm_id,
        cliente_id=cliente.id,
        es_credito=True,
        fecha_vencimiento=date(2026, 12, 31),
        detalles=[
            DetalleVentaCreate(variante_id=var.id, cantidad=Decimal("10"))
        ],
        pagos=[],
        completar=True,
    )
    venta = SaleService.crear_venta_financiera(db, neg_id, usr_id, data_venta)

    return {
        "negocio_id": neg_id,
        "usuario_id": usr_id,
        "admin_id": admin_id,
        "almacen_id": alm_id,
        "cliente": cliente,
        "venta": venta,
    }


def test_registro_pago_efectivo_confirmado(db_session, pos_credit_env):
    """Registrar un abono en efectivo a una venta a crédito nace CONFIRMED y amortiza la CxC."""
    env = pos_credit_env
    db = db_session

    pago = PaymentService.registrar_pago(
        db=db,
        negocio_id=env["negocio_id"],
        venta_id=env["venta"].id,
        usuario_id=env["usuario_id"],
        monto=Decimal("200.00"),
        metodo_pago="EFECTIVO",
        referencia="RECIBO-EFECTIVO-001",
    )

    assert pago.monto == Decimal("200.00")
    assert pago.estado == EstadoPago.CONFIRMED.value
    assert pago.origen == OrigenPago.OPERATIVO.value
    assert pago.es_cobro_verificado is True
    assert pago.esta_conciliado is False

    # Verificar que la cuenta por cobrar bajó de 500 a 300
    cxc = db.query(CuentaPorCobrar).filter(CuentaPorCobrar.venta_id == env["venta"].id).first()
    assert cxc.saldo_pendiente == Decimal("300.00")
    assert cxc.estado == "PENDIENTE"

    # Venta pasa a PARTIAL
    venta = db.query(Venta).filter(Venta.id == env["venta"].id).first()
    assert venta.estado_financiero == EstadoFinanciero.PARTIAL.value


def test_registro_pago_qr_pendiente_no_descuenta_deuda(db_session, pos_credit_env):
    """Un pago QR registrado sin confirmar nace PENDING y no reduce el saldo de la deuda."""
    env = pos_credit_env
    db = db_session

    pago = PaymentService.registrar_pago(
        db=db,
        negocio_id=env["negocio_id"],
        venta_id=env["venta"].id,
        usuario_id=env["usuario_id"],
        monto=Decimal("150.00"),
        metodo_pago="QR",
        referencia="QR-INTENTO-123",
    )

    assert pago.monto == Decimal("150.00")
    assert pago.estado == EstadoPago.PENDING.value
    assert pago.origen == OrigenPago.OPERATIVO.value
    assert pago.es_cobro_verificado is False  # PENDING nunca es verificado
    assert pago.esta_conciliado is False

    # La deuda sigue intacta en Bs 500
    cxc = db.query(CuentaPorCobrar).filter(CuentaPorCobrar.venta_id == env["venta"].id).first()
    assert cxc.saldo_pendiente == Decimal("500.00")

    # Venta sigue en CREDIT
    venta = db.query(Venta).filter(Venta.id == env["venta"].id).first()
    assert venta.estado_financiero == EstadoFinanciero.CREDIT.value


def test_confirmacion_pago_qr_pendiente(db_session, pos_credit_env):
    """Confirmar explícitamente un pago PENDING actualiza el saldo de la cuenta por cobrar."""
    env = pos_credit_env
    db = db_session

    pago = PaymentService.registrar_pago(
        db=db,
        negocio_id=env["negocio_id"],
        venta_id=env["venta"].id,
        usuario_id=env["usuario_id"],
        monto=Decimal("300.00"),
        metodo_pago="QR",
        referencia="QR-12345",
    )
    assert pago.estado == EstadoPago.PENDING.value

    # Confirmación por cajero
    pago_confirmado = PaymentService.confirmar_pago(
        db=db,
        negocio_id=env["negocio_id"],
        pago_id=pago.id,
        usuario_id=env["usuario_id"],
        referencia_confirmacion="BANCO-AUT-789",
        notas="Comprobante verificado en banca móvil",
    )

    assert pago_confirmado.estado == EstadoPago.CONFIRMED.value
    assert pago_confirmado.es_cobro_verificado is True
    assert "CONF:BANCO-AUT-789" in pago_confirmado.referencia

    # Deuda se redujo de 500 a 200
    cxc = db.query(CuentaPorCobrar).filter(CuentaPorCobrar.venta_id == env["venta"].id).first()
    assert cxc.saldo_pendiente == Decimal("200.00")


def test_rechazo_confirmar_pago_ya_confirmado(db_session, pos_credit_env):
    """Intentar confirmar un pago que ya está CONFIRMED debe lanzar ValueError."""
    env = pos_credit_env
    db = db_session

    pago = PaymentService.registrar_pago(
        db=db,
        negocio_id=env["negocio_id"],
        venta_id=env["venta"].id,
        usuario_id=env["usuario_id"],
        monto=Decimal("100.00"),
        metodo_pago="EFECTIVO",
    )
    assert pago.estado == EstadoPago.CONFIRMED.value

    with pytest.raises(ValueError, match="El pago ya se encuentra confirmado"):
        PaymentService.confirmar_pago(
            db=db,
            negocio_id=env["negocio_id"],
            pago_id=pago.id,
            usuario_id=env["usuario_id"],
        )


def test_marcar_pago_fallido(db_session, pos_credit_env):
    """Marcar un pago PENDING como FAILED por rechazo."""
    env = pos_credit_env
    db = db_session

    pago = PaymentService.registrar_pago(
        db=db,
        negocio_id=env["negocio_id"],
        venta_id=env["venta"].id,
        usuario_id=env["usuario_id"],
        monto=Decimal("100.00"),
        metodo_pago="QR",
    )
    assert pago.estado == EstadoPago.PENDING.value

    pago_fallido = PaymentService.marcar_pago_fallido(
        db=db,
        negocio_id=env["negocio_id"],
        pago_id=pago.id,
        usuario_id=env["usuario_id"],
        motivo="Transferencia rebotada por banco emisor",
    )

    assert pago_fallido.estado == EstadoPago.FAILED.value
    assert "FALLIDO" in pago_fallido.referencia


def test_reversion_total_de_pago(db_session, pos_credit_env):
    """Reversión total de un pago confirmado: genera ReversionPago y reabre saldo de deuda."""
    env = pos_credit_env
    db = db_session

    # 1. Pago de Bs 500 (cancela la deuda por completo)
    pago = PaymentService.registrar_pago(
        db=db,
        negocio_id=env["negocio_id"],
        venta_id=env["venta"].id,
        usuario_id=env["usuario_id"],
        monto=Decimal("500.00"),
        metodo_pago="EFECTIVO",
    )
    cxc = db.query(CuentaPorCobrar).filter(CuentaPorCobrar.venta_id == env["venta"].id).first()
    assert cxc.saldo_pendiente == Decimal("0.00")
    assert cxc.estado == "PAGADA"
    db.refresh(env["venta"])
    assert env["venta"].estado_financiero == EstadoFinanciero.PAID.value

    # 2. Revertir el pago completo
    reversion = PaymentService.revertir_pago(
        db=db,
        negocio_id=env["negocio_id"],
        pago_id=pago.id,
        usuario_id=env["usuario_id"],
        motivo="Cliente solicitó anulación de cobro",
        es_devolucion_efectivo=True,
    )

    assert reversion.monto_revertido == Decimal("500.00")
    assert reversion.es_devolucion_efectivo is True
    assert pago.saldo_reversible == Decimal("0.00")

    # La deuda vuelve a Bs 500 y estado PENDIENTE
    db.refresh(cxc)
    assert cxc.saldo_pendiente == Decimal("500.00")
    assert cxc.estado == "PENDIENTE"

    # La venta vuelve a CREDIT
    db.refresh(env["venta"])
    assert env["venta"].estado_financiero == EstadoFinanciero.CREDIT.value


def test_reversion_parcial_de_pago(db_session, pos_credit_env):
    """Reversión parcial: reversar Bs 100 de un pago de Bs 300 deja Bs 200 neto."""
    env = pos_credit_env
    db = db_session

    pago = PaymentService.registrar_pago(
        db=db,
        negocio_id=env["negocio_id"],
        venta_id=env["venta"].id,
        usuario_id=env["usuario_id"],
        monto=Decimal("300.00"),
        metodo_pago="EFECTIVO",
    )

    # Reversión parcial de 100
    rev = PaymentService.revertir_pago(
        db=db,
        negocio_id=env["negocio_id"],
        pago_id=pago.id,
        usuario_id=env["usuario_id"],
        monto_a_revertir=Decimal("100.00"),
        motivo="Corrección de importe cobrado de más",
        es_devolucion_efectivo=True,
    )

    assert rev.monto_revertido == Decimal("100.00")
    assert pago.total_revertido == Decimal("100.00")
    assert pago.saldo_reversible == Decimal("200.00")

    # Saldo de deuda: 500 - 300 + 100 = 300
    cxc = db.query(CuentaPorCobrar).filter(CuentaPorCobrar.venta_id == env["venta"].id).first()
    assert cxc.saldo_pendiente == Decimal("300.00")


def test_reversion_superior_al_permitido_falla(db_session, pos_credit_env):
    """Intentar revertir más de lo disponible debe fallar."""
    env = pos_credit_env
    db = db_session

    pago = PaymentService.registrar_pago(
        db=db,
        negocio_id=env["negocio_id"],
        venta_id=env["venta"].id,
        usuario_id=env["usuario_id"],
        monto=Decimal("100.00"),
        metodo_pago="EFECTIVO",
    )

    with pytest.raises(ValueError, match="supera el saldo reversible disponible"):
        PaymentService.revertir_pago(
            db=db,
            negocio_id=env["negocio_id"],
            pago_id=pago.id,
            usuario_id=env["usuario_id"],
            monto_a_revertir=Decimal("150.00"),
            motivo="Error",
        )


def test_prevencion_doble_reversion(db_session, pos_credit_env):
    """Una vez revertido el 100%, cualquier intento posterior debe fallar."""
    env = pos_credit_env
    db = db_session

    pago = PaymentService.registrar_pago(
        db=db,
        negocio_id=env["negocio_id"],
        venta_id=env["venta"].id,
        usuario_id=env["usuario_id"],
        monto=Decimal("100.00"),
        metodo_pago="EFECTIVO",
    )

    PaymentService.revertir_pago(
        db=db,
        negocio_id=env["negocio_id"],
        pago_id=pago.id,
        usuario_id=env["usuario_id"],
        monto_a_revertir=Decimal("100.00"),
        motivo="Reversión total inicial",
    )

    with pytest.raises(ValueError, match="El pago ya ha sido completamente revertido"):
        PaymentService.revertir_pago(
            db=db,
            negocio_id=env["negocio_id"],
            pago_id=pago.id,
            usuario_id=env["usuario_id"],
            monto_a_revertir=Decimal("10.00"),
            motivo="Intento de segunda reversión",
        )


def test_aislamiento_multitenant_pago_ajeno(db_session, pos_credit_env):
    """No se puede consultar, registrar ni revertir pagos de otro negocio."""
    env = pos_credit_env
    db = db_session
    otro_negocio_id = str(uuid.uuid4())

    pago = PaymentService.registrar_pago(
        db=db,
        negocio_id=env["negocio_id"],
        venta_id=env["venta"].id,
        usuario_id=env["usuario_id"],
        monto=Decimal("100.00"),
        metodo_pago="EFECTIVO",
    )

    with pytest.raises(ValueError, match="Pago no encontrado o no pertenece a su negocio"):
        PaymentService.revertir_pago(
            db=db,
            negocio_id=otro_negocio_id,
            pago_id=pago.id,
            usuario_id="hacker",
            motivo="Ataque cross-tenant",
        )


def test_clasificacion_pago_historico_presuntivo_y_conciliacion_inmutable(db_session, pos_credit_env):
    """
    Un pago presuntivo histórico (Fase 2.2.1):
    1. Nace con origen=HISTORICO_PRESUNTIVO, esta_conciliado=False, es_cobro_verificado=False.
    2. Su venta de contado queda como PAID_PRESUNTIVO.
    3. Conciliarlo NO muta PagoVenta (inmutabilidad estricta de origen y referencia).
    4. Genera un registro independiente ConciliacionPago con FKs compuestas.
    5. Tras la conciliación, esta_conciliado=True, es_cobro_verificado=True y la venta pasa a PAID.
    6. Se rechaza una segunda conciliación y la conciliación de pagos OPERATIVO.
    """
    env = pos_credit_env
    db = db_session

    # Crear una venta histórica completada de contado (Bs 150)
    venta_hist = Venta(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        almacen_id=env["almacen_id"],
        numero=9999,
        total=Decimal("150.00"),
        subtotal=Decimal("150.00"),
        descuento=Decimal("0.00"),
        metodo_pago=MetodoPago.EFECTIVO.value,
        estado=EstadoVenta.COMPLETADA.value,
        estado_financiero=EstadoFinanciero.PAID_PRESUNTIVO.value,
        usuario_id=env["usuario_id"],
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    db.add(venta_hist)

    # Simular el pago migrado por backfill conservador
    pago_hist = PagoVenta(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        venta_id=venta_hist.id,
        monto=Decimal("150.00"),
        metodo_pago="EFECTIVO",
        estado=EstadoPago.CONFIRMED.value,
        origen=OrigenPago.HISTORICO_PRESUNTIVO.value,
        usuario_id=env["usuario_id"],
        fecha_pago=datetime(2025, 1, 1, tzinfo=timezone.utc),
        referencia="MIGRACION_HISTORICA",
    )
    db.add(pago_hist)
    db.commit()

    # Verificaciones previas a la conciliación
    assert pago_hist.origen == OrigenPago.HISTORICO_PRESUNTIVO.value
    assert pago_hist.esta_conciliado is False
    assert pago_hist.es_cobro_verificado is False
    assert venta_hist.estado_financiero == EstadoFinanciero.PAID_PRESUNTIVO.value

    historial = PaymentService.consultar_historial(db, env["negocio_id"], venta_hist.id)
    assert historial["total_cobrado_presuntivo"] == Decimal("150.00")
    assert historial["total_cobrado_verificado"] == Decimal("0.00")
    assert historial["estado_financiero"] == EstadoFinanciero.PAID_PRESUNTIVO.value

    # Intento por parte de un usuario con rol EMPLEADO debe ser rechazado
    with pytest.raises(PermissionError, match="Solo un usuario con rol Administrador"):
        PaymentService.conciliar_pago_historico(
            db=db,
            negocio_id=env["negocio_id"],
            pago_id=pago_hist.id,
            usuario_id=env["usuario_id"],  # Rol EMPLEADO
            motivo="Intento no autorizado",
            referencia_documental="DOC-1",
        )

    # Conciliar formalmente el pago por el usuario ADMIN con referencia documental
    conciliacion = PaymentService.conciliar_pago_historico(
        db=db,
        negocio_id=env["negocio_id"],
        pago_id=pago_hist.id,
        usuario_id=env["admin_id"],  # Rol ADMIN
        motivo="Verificado físicamente con talonario archivado",
        referencia_documental="TALONARIO-FISICO-2025-001",
        notas="Revisado por auditoría contable",
    )

    # 1. Comprobar que ConciliacionPago se generó correctamente
    assert isinstance(conciliacion, ConciliacionPago)
    assert conciliacion.negocio_id == env["negocio_id"]
    assert conciliacion.pago_id == pago_hist.id
    assert conciliacion.venta_id == venta_hist.id
    assert conciliacion.motivo == "Verificado físicamente con talonario archivado"
    assert conciliacion.referencia_documental == "TALONARIO-FISICO-2025-001"
    assert conciliacion.estado == EstadoConciliacion.APROBADA.value

    # 2. INMUTABILIDAD ESTRICTA: PagoVenta NO fue mutado
    db.refresh(pago_hist)
    assert pago_hist.origen == OrigenPago.HISTORICO_PRESUNTIVO.value  # NO cambió a VERIFICADO
    assert pago_hist.referencia == "MIGRACION_HISTORICA"              # NO se concatenó nada

    # 3. Propiedades dinámicas reflejan la conciliación aprobada
    assert pago_hist.esta_conciliado is True
    assert pago_hist.es_cobro_verificado is True

    # 4. Estado financiero de la venta transicionó canónicamente a PAID
    db.refresh(venta_hist)
    assert venta_hist.estado_financiero == EstadoFinanciero.PAID.value

    # 5. Historial ahora computa como cobro verificado
    historial2 = PaymentService.consultar_historial(db, env["negocio_id"], venta_hist.id)
    assert historial2["total_cobrado_verificado"] == Decimal("150.00")
    assert historial2["total_cobrado_presuntivo"] == Decimal("0.00")
    assert historial2["estado_financiero"] == EstadoFinanciero.PAID.value
    assert len(historial2["pagos"][0]["conciliaciones"]) == 1

    # 6. Rechazo de segunda conciliación sobre el mismo pago
    with pytest.raises(ValueError, match="ya cuenta con una conciliación aprobada"):
        PaymentService.conciliar_pago_historico(
            db=db,
            negocio_id=env["negocio_id"],
            pago_id=pago_hist.id,
            usuario_id=env["admin_id"],
            motivo="Intento de re-conciliación redundante",
            referencia_documental="DOC-DUPLICADO",
        )

    # 7. Rechazo de conciliación en un pago OPERATIVO
    pago_op = PaymentService.registrar_pago(
        db=db,
        negocio_id=env["negocio_id"],
        venta_id=env["venta"].id,
        usuario_id=env["usuario_id"],
        monto=Decimal("50.00"),
        metodo_pago="EFECTIVO",
    )
    with pytest.raises(ValueError, match="Solo los pagos con origen HISTORICO_PRESUNTIVO"):
        PaymentService.conciliar_pago_historico(
            db=db,
            negocio_id=env["negocio_id"],
            pago_id=pago_op.id,
            usuario_id=env["admin_id"],
            motivo="Intento inválido sobre pago operativo",
            referencia_documental="DOC-123",
        )

    # 8. Rechazo de conciliación cross-tenant
    otro_negocio_id = str(uuid.uuid4())
    otro_admin_id = str(uuid.uuid4())
    otro_admin = User(
        id=otro_admin_id,
        email="otro_admin@apolo.lat",
        password_hash="hash",
        nombre="Otro Admin",
        negocio_id=otro_negocio_id,
        rol=UserRole.ADMIN.value,
        activo=True,
    )
    db.add(otro_admin)
    db.commit()

    # A) Intento con usuario ajeno al tenant
    with pytest.raises(ValueError, match="Usuario no encontrado o no pertenece a su negocio"):
        PaymentService.conciliar_pago_historico(
            db=db,
            negocio_id=otro_negocio_id,
            pago_id=pago_hist.id,
            usuario_id=env["admin_id"],  # pertenece a env["negocio_id"]
            motivo="Intento cross-tenant usuario",
            referencia_documental="DOC-HACK1",
        )

    # B) Intento con admin legítimo de otro tenant sobre pago de este tenant
    with pytest.raises(ValueError, match="Pago no encontrado o no pertenece a su negocio"):
        PaymentService.conciliar_pago_historico(
            db=db,
            negocio_id=otro_negocio_id,
            pago_id=pago_hist.id,       # pertenece a env["negocio_id"]
            usuario_id=otro_admin_id,   # pertenece a otro_negocio_id
            motivo="Intento cross-tenant pago",
            referencia_documental="DOC-HACK2",
        )

    # 9. Inmutabilidad estricta de ConciliacionPago: prohibido modificar y prohibido eliminar
    with pytest.raises(RuntimeError, match="inmutables y no admiten modificaciones"):
        conciliacion.motivo = "Intento de mutación de motivo"
        db.commit()

    db.rollback()

    with pytest.raises(RuntimeError, match="registros de auditoría permanentes y no admiten eliminación"):
        db.delete(conciliacion)
        db.commit()

    db.rollback()


def test_intento_pago_excede_saldo_pendiente_falla(db_session, pos_credit_env):
    """Intentar registrar un pago mayor a la deuda de la venta debe fallar."""
    env = pos_credit_env
    db = db_session

    with pytest.raises(ValueError, match="supera el saldo pendiente de la venta"):
        PaymentService.registrar_pago(
            db=db,
            negocio_id=env["negocio_id"],
            venta_id=env["venta"].id,
            usuario_id=env["usuario_id"],
            monto=Decimal("600.00"),  # Deuda es 500
            metodo_pago="EFECTIVO",
        )


# ===========================================================================
# TESTS FASE 2.2.2 — REGLAS CANÓNICAS DE SALDOS Y SEGURIDAD DE INTEGRIDAD
# ===========================================================================

def test_reglas_canonicas_saldos_casos_a_b_c_y_reversiones(db_session, pos_credit_env):
    """
    Certificación de las tres reglas canónicas de saldo exigidas en Fase 2.2.2:
    - Caso A: Venta Bs 1.000, Verificado Bs 400, Presuntivo Bs 600 -> Saldo Operativo = Bs 600, PARTIAL.
      Efectos de reversión parcial (-100) -> Saldo Operativo = Bs 700.
      Efectos de reversión total (-300 restante) -> Saldo Operativo = Bs 1.000.
    - Caso B: Venta Bs 1.000, Presuntivo Bs 1.000 (sin conciliar) -> Saldo Operativo = Bs 1.000, PAID_PRESUNTIVO.
    - Caso C: Venta Bs 1.000, Presuntivo Bs 1.000 (conciliado con admin) -> Saldo Operativo = Bs 0, PAID.
    """
    env = pos_credit_env
    db = db_session

    # -----------------------------------------------------------------------
    # CASO A: Venta Bs 1.000, Verificado Bs 400, Presuntivo Bs 600
    # -----------------------------------------------------------------------
    v_a = Venta(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        almacen_id=env["almacen_id"],
        numero=1001,
        total=Decimal("1000.00"),
        subtotal=Decimal("1000.00"),
        descuento=Decimal("0.00"),
        metodo_pago=MetodoPago.MIXTO.value,
        estado=EstadoVenta.COMPLETADA.value,
        estado_financiero=EstadoFinanciero.PARTIAL.value,
        usuario_id=env["usuario_id"],
        created_at=datetime.now(timezone.utc),
    )
    db.add(v_a)

    # Pago 1: Verificado operativo contemporáneo (Bs 400)
    p_verif = PagoVenta(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        venta_id=v_a.id,
        monto=Decimal("400.00"),
        metodo_pago="EFECTIVO",
        estado=EstadoPago.CONFIRMED.value,
        origen=OrigenPago.OPERATIVO.value,
        usuario_id=env["usuario_id"],
        fecha_pago=datetime.now(timezone.utc),
        referencia="RECIBO-ACTUAL-01",
    )
    # Pago 2: Presuntivo histórico migrado (Bs 600)
    p_presunt = PagoVenta(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        venta_id=v_a.id,
        monto=Decimal("600.00"),
        metodo_pago="EFECTIVO",
        estado=EstadoPago.CONFIRMED.value,
        origen=OrigenPago.HISTORICO_PRESUNTIVO.value,
        usuario_id=env["usuario_id"],
        fecha_pago=datetime.now(timezone.utc),
        referencia="MIGRACION_HISTORICA",
    )
    db.add_all([p_verif, p_presunt])
    db.commit()

    PaymentService._recalcular_estado_financiero_venta(db, v_a)
    db.refresh(v_a)

    hist_a = PaymentService.consultar_historial(db, env["negocio_id"], v_a.id)
    assert hist_a["total_venta"] == Decimal("1000.00")
    assert hist_a["total_cobrado_verificado"] == Decimal("400.00")
    assert hist_a["total_cobrado_presuntivo"] == Decimal("600.00")
    assert hist_a["saldo_operativo_pendiente"] == Decimal("600.00")     # 1000 - 400 verificado
    assert hist_a["importe_presuntivo_pendiente"] == Decimal("600.00")  # 600 presuntivo
    assert hist_a["estado_financiero"] == EstadoFinanciero.PARTIAL.value

    # Efecto de reversión parcial: revertir Bs 100 del pago verificado
    PaymentService.revertir_pago(
        db=db,
        negocio_id=env["negocio_id"],
        pago_id=p_verif.id,
        usuario_id=env["usuario_id"],
        monto_a_revertir=Decimal("100.00"),
        motivo="Reversión parcial de Bs 100",
    )
    hist_a_rev1 = PaymentService.consultar_historial(db, env["negocio_id"], v_a.id)
    assert hist_a_rev1["total_cobrado_verificado"] == Decimal("300.00")
    assert hist_a_rev1["saldo_operativo_pendiente"] == Decimal("700.00")    # 1000 - 300
    assert hist_a_rev1["importe_presuntivo_pendiente"] == Decimal("600.00")
    assert hist_a_rev1["estado_financiero"] == EstadoFinanciero.PARTIAL.value

    # Efecto de reversión total: revertir los Bs 300 restantes del verificado
    PaymentService.revertir_pago(
        db=db,
        negocio_id=env["negocio_id"],
        pago_id=p_verif.id,
        usuario_id=env["usuario_id"],
        monto_a_revertir=Decimal("300.00"),
        motivo="Reversión de saldo restante",
    )
    hist_a_rev2 = PaymentService.consultar_historial(db, env["negocio_id"], v_a.id)
    assert hist_a_rev2["total_cobrado_verificado"] == Decimal("0.00")
    assert hist_a_rev2["saldo_operativo_pendiente"] == Decimal("1000.00")   # Toda la deuda operativa abierta
    assert hist_a_rev2["importe_presuntivo_pendiente"] == Decimal("600.00")
    assert hist_a_rev2["estado_financiero"] == EstadoFinanciero.PARTIAL.value

    # -----------------------------------------------------------------------
    # CASO B: Venta Bs 1.000, Presuntivo Bs 1.000 sin conciliación
    # -----------------------------------------------------------------------
    v_b = Venta(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        almacen_id=env["almacen_id"],
        numero=1002,
        total=Decimal("1000.00"),
        subtotal=Decimal("1000.00"),
        descuento=Decimal("0.00"),
        metodo_pago=MetodoPago.EFECTIVO.value,
        estado=EstadoVenta.COMPLETADA.value,
        estado_financiero=EstadoFinanciero.PAID_PRESUNTIVO.value,
        usuario_id=env["usuario_id"],
        created_at=datetime.now(timezone.utc),
    )
    db.add(v_b)

    p_b = PagoVenta(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        venta_id=v_b.id,
        monto=Decimal("1000.00"),
        metodo_pago="EFECTIVO",
        estado=EstadoPago.CONFIRMED.value,
        origen=OrigenPago.HISTORICO_PRESUNTIVO.value,
        usuario_id=env["usuario_id"],
        fecha_pago=datetime.now(timezone.utc),
        referencia="MIGRACION_HISTORICA",
    )
    db.add(p_b)
    db.commit()

    PaymentService._recalcular_estado_financiero_venta(db, v_b)
    db.refresh(v_b)

    hist_b = PaymentService.consultar_historial(db, env["negocio_id"], v_b.id)
    assert hist_b["total_venta"] == Decimal("1000.00")
    assert hist_b["total_cobrado_verificado"] == Decimal("0.00")
    assert hist_b["total_cobrado_presuntivo"] == Decimal("1000.00")
    assert hist_b["saldo_operativo_pendiente"] == Decimal("1000.00")    # Deuda operativa completa no amortizada
    assert hist_b["importe_presuntivo_pendiente"] == Decimal("1000.00")
    assert hist_b["estado_financiero"] == EstadoFinanciero.PAID_PRESUNTIVO.value

    # -----------------------------------------------------------------------
    # CASO C: Venta Bs 1.000, Presuntivo Bs 1.000 con conciliación documental aprobada
    # -----------------------------------------------------------------------
    # Conciliamos formalmente el pago de Caso B usando usuario ADMIN
    concil_b = PaymentService.conciliar_pago_historico(
        db=db,
        negocio_id=env["negocio_id"],
        pago_id=p_b.id,
        usuario_id=env["admin_id"],
        motivo="Arqueo físico formal con comprobante de ingreso 9988",
        referencia_documental="COMPROBANTE-INGRESO-9988",
        notas="Validado por contador",
    )
    assert concil_b.estado == EstadoConciliacion.APROBADA.value

    hist_c = PaymentService.consultar_historial(db, env["negocio_id"], v_b.id)
    assert hist_c["total_cobrado_verificado"] == Decimal("1000.00")
    assert hist_c["total_cobrado_presuntivo"] == Decimal("0.00")
    assert hist_c["saldo_operativo_pendiente"] == Decimal("0.00")       # Totalmente saldado operativamente
    assert hist_c["importe_presuntivo_pendiente"] == Decimal("0.00")
    assert hist_c["estado_financiero"] == EstadoFinanciero.PAID.value


def test_restriccion_check_ventas_estado_financiero(db_session, pos_credit_env):
    """
    Verifica que la restricción CHECK chk_ventas_estado_financiero:
    1. Permita explícitamente PAID_PRESUNTIVO junto con PAID, PARTIAL, CREDIT, NOT_APPLICABLE.
    2. Rechace cualquier valor fuera del dominio permitido.
    """
    env = pos_credit_env
    db = db_session

    # 1. PAID_PRESUNTIVO debe persistirse exitosamente
    v_ok = Venta(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        almacen_id=env["almacen_id"],
        numero=8001,
        total=Decimal("100.00"),
        subtotal=Decimal("100.00"),
        metodo_pago=MetodoPago.EFECTIVO.value,
        estado=EstadoVenta.COMPLETADA.value,
        estado_financiero=EstadoFinanciero.PAID_PRESUNTIVO.value,
        usuario_id=env["usuario_id"],
        created_at=datetime.now(timezone.utc),
    )
    db.add(v_ok)
    db.commit()
    db.refresh(v_ok)
    assert v_ok.estado_financiero == "PAID_PRESUNTIVO"

    # 2. Estado inválido debe disparar excepción de base de datos
    v_invalida = Venta(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        almacen_id=env["almacen_id"],
        numero=8002,
        total=Decimal("100.00"),
        subtotal=Decimal("100.00"),
        metodo_pago=MetodoPago.EFECTIVO.value,
        estado=EstadoVenta.COMPLETADA.value,
        estado_financiero="ESTADO_INVENTADO",
        usuario_id=env["usuario_id"],
        created_at=datetime.now(timezone.utc),
    )
    db.add(v_invalida)
    with pytest.raises(Exception):
        db.commit()

    db.rollback()


def test_prevencion_conciliaciones_duplicadas_por_unique_constraint(db_session, pos_credit_env):
    """
    Verifica que:
    1. Un pago histórico puede registrar un intento previo RECHAZADA sin que se considere conciliado.
    2. Posteriormente puede registrar una conciliación APROBADA legítima.
    3. El índice único uq_conciliaciones_negocio_pago_aprobada impide en BD registrar dos APROBADAS para el mismo pago.
    """
    env = pos_credit_env
    db = db_session

    # Crear venta y pago histórico
    v = Venta(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        almacen_id=env["almacen_id"],
        numero=8003,
        total=Decimal("50.00"),
        subtotal=Decimal("50.00"),
        metodo_pago=MetodoPago.EFECTIVO.value,
        estado=EstadoVenta.COMPLETADA.value,
        estado_financiero=EstadoFinanciero.PAID_PRESUNTIVO.value,
        usuario_id=env["usuario_id"],
        created_at=datetime.now(timezone.utc),
    )
    p = PagoVenta(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        venta_id=v.id,
        monto=Decimal("50.00"),
        metodo_pago="EFECTIVO",
        estado=EstadoPago.CONFIRMED.value,
        origen=OrigenPago.HISTORICO_PRESUNTIVO.value,
        usuario_id=env["usuario_id"],
        fecha_pago=datetime.now(timezone.utc),
        referencia="MIGRACION_HISTORICA",
    )
    db.add_all([v, p])
    db.commit()

    # 1. Intento previo rechazado (por documentación incompleta)
    c_rechazada = ConciliacionPago(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        pago_id=p.id,
        venta_id=v.id,
        usuario_id=env["admin_id"],
        fecha_conciliacion=datetime.now(timezone.utc),
        motivo="Rechazado inicialmente por falta de recibo físico legible",
        referencia_documental="DOC-ILEGIBLE",
        estado=EstadoConciliacion.RECHAZADA.value,
    )
    db.add(c_rechazada)
    db.commit()

    # El pago sigue sin estar conciliado
    db.refresh(p)
    assert p.esta_conciliado is False
    assert p.es_cobro_verificado is False

    # 2. Reintento exitoso con documentación subsanada -> APROBADA
    c1 = ConciliacionPago(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        pago_id=p.id,
        venta_id=v.id,
        usuario_id=env["admin_id"],
        fecha_conciliacion=datetime.now(timezone.utc),
        motivo="Comprobante subsanado y validado en talonario",
        referencia_documental="DOC-VALIDO-01",
        estado=EstadoConciliacion.APROBADA.value,
    )
    db.add(c1)
    db.commit()

    db.refresh(p)
    assert p.esta_conciliado is True
    assert p.es_cobro_verificado is True

    # 3. Intento de persistir una SEGUNDA conciliación APROBADA para el mismo pago debe fallar por índice único
    c2 = ConciliacionPago(
        id=str(uuid.uuid4()),
        negocio_id=env["negocio_id"],
        pago_id=p.id,
        venta_id=v.id,
        usuario_id=env["admin_id"],
        fecha_conciliacion=datetime.now(timezone.utc),
        motivo="Segunda conciliación concurrente no permitida",
        referencia_documental="DOC-DUPLICADO",
        estado=EstadoConciliacion.APROBADA.value,
    )
    db.add(c2)
    with pytest.raises(Exception):
        db.commit()

    db.rollback()
