"""
Test de atomicidad — venta multi-producto donde una línea falla por stock.

Contexto: antes del fix, crear_movimiento() (y las funciones internas
_crear_movimiento_entrada / _crear_movimientos_salida) hacían su propio
db.commit() por cada línea de venta. Si una venta con varias líneas fallaba
a mitad de camino (ej. la segunda línea sin stock), la primera línea ya
había quedado comiteada de forma permanente — inventario descontado sin
una venta válida que lo respalde.

Cómo usar este archivo:
  1. Correlo ANTES de aplicar el diff de atomicidad a inventario/service.py:
     debería FALLAR (confirma que el bug es real y reproducible).
  2. Aplicá el diff (sacar los db.commit() internos de crear_movimiento,
     _crear_movimiento_entrada, _crear_movimientos_salida, crear_lote,
     dar_baja_lote_vencido).
  3. Volvé a correrlo: debería PASAR.
"""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

NEGOCIO_ID = str(uuid.uuid4())
ALMACEN_ID = str(uuid.uuid4())


def _crear_producto_variante(db, nombre: str, precio_venta, stock_inicial: Decimal):
    """
    Crea Producto + Variante, y si stock_inicial > 0 también un Lote con esa
    cantidad más una fila Stock ya sincronizada al mismo valor (simulando el
    estado que dejaría una entrada de compra real vía crear_lote()).

    Devuelve (producto, variante).
    """
    from app.modules.inventario.models import Producto, Variante, Lote, Stock

    producto = Producto(
        id=str(uuid.uuid4()),
        negocio_id=NEGOCIO_ID,
        nombre=nombre,
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
        precio_venta=Decimal(str(precio_venta)),
        precio_costo=Decimal("1"),
        activa=True,
    )
    db.add(variante)
    db.flush()

    if stock_inicial > 0:
        lote = Lote(
            id=str(uuid.uuid4()),
            variante_id=variante.id,
            almacen_id=ALMACEN_ID,
            fecha_ingreso=datetime.now(timezone.utc),
            cantidad_inicial=Decimal(str(stock_inicial)),
            cantidad_actual=Decimal(str(stock_inicial)),
            costo_unitario=Decimal("1"),
            activo=True,
        )
        db.add(lote)

    stock = Stock(
        id=str(uuid.uuid4()),
        variante_id=variante.id,
        almacen_id=ALMACEN_ID,
        cantidad_actual=Decimal(str(stock_inicial)),
    )
    db.add(stock)
    db.flush()

    return producto, variante


@pytest.fixture
def almacen(db):
    from app.modules.inventario.models import Almacen, Negocio

    negocio = Negocio(
        id=NEGOCIO_ID,
        nombre="Negocio Atomicidad",
        propietario="Admin",
        moneda="BOB",
        activo=True,
    )
    db.add(negocio)
    db.flush()

    almacen = Almacen(
        id=ALMACEN_ID,
        negocio_id=NEGOCIO_ID,
        nombre="Almacén de prueba",
        es_principal=True,
    )
    db.add(almacen)
    db.flush()
    return almacen


def test_venta_multiproducto_falla_atomicamente(db, almacen):
    """
    Venta con 2 líneas: variante A tiene 10 unidades en stock (se pide 5,
    alcanza), variante B no tiene ningún stock cargado (se pide 100, falla).

    Resultado esperado (código correcto): la venta entera se revierte.
    Ningún stock cambia, no queda ninguna venta a medio completar.
    """
    from app.modules.ventas.service import crear_venta
    from app.modules.ventas.schemas import VentaCreate, DetalleVentaCreate
    from app.modules.inventario.service import get_stock_by_variante
    from app.modules.ventas.models import Venta

    from app.modules.auth.models import User, UserRole
    usuario_id = str(uuid.uuid4())
    user = User(
        id=usuario_id,
        email="atomic@apolo.com",
        password_hash="pwd",
        nombre="User Atomic",
        negocio_id=NEGOCIO_ID,
        rol=UserRole.EMPLEADO.value,
        activo=True,
    )
    db.add(user)
    db.flush()

    _, variante_a = _crear_producto_variante(
        db, "Producto A", precio_venta=100, stock_inicial=Decimal("10")
    )
    _, variante_b = _crear_producto_variante(
        db, "Producto B", precio_venta=50, stock_inicial=Decimal("0")
    )
    db.commit()  # el setup queda persistido antes de intentar la venta

    stock_a_antes = get_stock_by_variante(db, variante_a.id)[0].cantidad_actual

    data = VentaCreate(
        almacen_id=ALMACEN_ID,
        detalles=[
            DetalleVentaCreate(variante_id=variante_a.id, cantidad=Decimal("5")),    # con stock
            DetalleVentaCreate(variante_id=variante_b.id, cantidad=Decimal("100")),  # sin stock
        ],
        completar=True,
    )

    with pytest.raises(ValueError, match="Stock insuficiente"):
        crear_venta(db, NEGOCIO_ID, data, usuario_id)

    # --- DIAGNÓSTICO: estado real ANTES de hacer rollback ---
    # Si el commit() interno de crear_movimiento() se ejecutó para la línea A,
    # esto va a mostrar el lote ya descontado y el movimiento ya creado,
    # ANTES de que el rollback tenga oportunidad de tocar nada.
    from app.modules.inventario.models import Lote, MovimientoStock

    lote_a_pre = db.query(Lote).filter(Lote.variante_id == variante_a.id).first()
    movs_pre = db.query(MovimientoStock).filter(
        MovimientoStock.variante_id == variante_a.id
    ).all()
    print(f"\n[DEBUG] ANTES del rollback -> lote_a.cantidad_actual={lote_a_pre.cantidad_actual}, "
          f"movimientos_a={len(movs_pre)}")

    db.rollback()  # simula lo que get_db() hace ante una excepción en el request real

    lote_a_post = db.query(Lote).filter(Lote.variante_id == variante_a.id).first()
    movs_post = db.query(MovimientoStock).filter(
        MovimientoStock.variante_id == variante_a.id
    ).all()
    print(f"[DEBUG] DESPUES del rollback -> lote_a.cantidad_actual={lote_a_post.cantidad_actual}, "
          f"movimientos_a={len(movs_post)}")

    stock_a_despues = get_stock_by_variante(db, variante_a.id)[0].cantidad_actual
    assert stock_a_despues == stock_a_antes, (
        f"La línea A quedó parcialmente persistida pese al fallo de la línea B: "
        f"antes={stock_a_antes}, despues={stock_a_despues}"
    )

    # Verificación directa contra Lote y MovimientoStock — estas dos tablas NO
    # pasan por _sincronizar_stock(), así que no pueden estar "maquilladas" por
    # el bug de autoflush de Stock. Si esto falla pero el assert de arriba pasó,
    # confirma que había dos bugs cancelándose numéricamente por casualidad.
    from app.modules.inventario.models import Lote, MovimientoStock

    lote_a = db.query(Lote).filter(Lote.variante_id == variante_a.id).first()
    assert lote_a.cantidad_actual == Decimal("10"), (
        f"El lote de A quedó parcialmente descontado tras el rollback: {lote_a.cantidad_actual}"
    )

    movimientos_a = db.query(MovimientoStock).filter(
        MovimientoStock.variante_id == variante_a.id
    ).count()
    assert movimientos_a == 0, (
        f"Quedó un MovimientoStock de la línea A comiteado pese al fallo de la línea B "
        f"({movimientos_a} movimiento(s) encontrado(s))"
    )

    assert db.query(Venta).filter(Venta.negocio_id == NEGOCIO_ID).count() == 0, (
        "Quedó una venta a medio completar en la base tras el rollback"
    )