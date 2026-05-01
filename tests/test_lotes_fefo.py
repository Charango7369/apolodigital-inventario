"""
Tests del módulo inventario — soporte de Lotes y FEFO

Cubre los escenarios críticos:
1. Crear lote: stock se sincroniza
2. FEFO: se consume primero el lote que vence antes
3. FEFO: se reparte entre lotes cuando uno solo no alcanza
4. Override manual con lote_id: descuenta del lote indicado aunque otro venza antes
5. Producto que controla vencimiento: rechazar lote sin fecha
6. Producto que NO controla vencimiento: aceptar lote sin fecha
7. Stock insuficiente entre todos los lotes: lanza ValueError
8. Dar de baja lote vencido: crea MERMA_VENCIMIENTO y lo desactiva
9. Búsqueda por atributo JSON: encontrar variantes con talla específica
10. Stock denormalizado: igual a SUM(lotes activos)
"""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.modules.inventario import service
from app.modules.inventario.models import (
    Lote, Stock, MovimientoStock, Variante, Producto,
)
from app.modules.inventario.schemas import (
    LoteCreate, LoteUpdate, MovimientoCreate, BusquedaAtributosRequest,
    DarBajaLoteRequest,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def setup_basico(db, negocio_id):
    """
    Crea un escenario base con: almacén principal, categoría, proveedor,
    un producto sin variantes y otro con variantes.
    """
    almacen = service.create_almacen(
        db, negocio_id,
        # Suponiendo el schema AlmacenCreate del repo
        type("X", (), {"nombre": "Tienda", "ubicacion": None, "es_principal": True,
                       "model_dump": lambda self: {"nombre": "Tienda", "ubicacion": None, "es_principal": True}})(),
    )
    return {"almacen": almacen}


def _get_default_variante(db, producto):
    """La variante por defecto creada en create_producto."""
    return db.query(Variante).filter(Variante.producto_id == producto.id).first()


# ---------------------------------------------------------------------------
# 1. Crear lote sincroniza stock
# ---------------------------------------------------------------------------
def test_crear_lote_sincroniza_stock(db, negocio_id, almacen_principal, producto_simple):
    variante = _get_default_variante(db, producto_simple)

    lote = service.crear_lote(
        db, negocio_id,
        LoteCreate(
            variante_id=variante.id,
            almacen_id=almacen_principal.id,
            codigo_lote="L001",
            fecha_vencimiento=date.today() + timedelta(days=180),
            cantidad=Decimal("50"),
            costo_unitario=Decimal("12.50"),
        ),
    )

    assert lote.cantidad_actual == Decimal("50")

    stock = db.query(Stock).filter(
        Stock.variante_id == variante.id,
        Stock.almacen_id == almacen_principal.id,
    ).first()
    assert stock.cantidad_actual == Decimal("50")

    # Movimiento ENTRADA_COMPRA fue registrado
    mov = db.query(MovimientoStock).filter(MovimientoStock.lote_id == lote.id).first()
    assert mov is not None
    assert mov.tipo == "ENTRADA_COMPRA"
    assert mov.cantidad == Decimal("50")


# ---------------------------------------------------------------------------
# 2. FEFO: consume el lote que vence antes
# ---------------------------------------------------------------------------
def test_fefo_consume_lote_que_vence_antes(db, negocio_id, almacen_principal, producto_simple):
    variante = _get_default_variante(db, producto_simple)

    # Lote A: vence en 60 días, 30 unidades
    lote_a = service.crear_lote(db, negocio_id, LoteCreate(
        variante_id=variante.id, almacen_id=almacen_principal.id,
        fecha_vencimiento=date.today() + timedelta(days=60),
        cantidad=Decimal("30"), costo_unitario=Decimal("10"),
    ))
    # Lote B: vence en 200 días, 100 unidades
    lote_b = service.crear_lote(db, negocio_id, LoteCreate(
        variante_id=variante.id, almacen_id=almacen_principal.id,
        fecha_vencimiento=date.today() + timedelta(days=200),
        cantidad=Decimal("100"), costo_unitario=Decimal("11"),
    ))

    # Vender 20: debe salir del lote A (el que vence antes)
    movs = service.crear_movimiento(db, negocio_id, MovimientoCreate(
        variante_id=variante.id, almacen_id=almacen_principal.id,
        tipo="SALIDA_VENTA", cantidad=Decimal("20"), referencia_id="VTA-001",
    ))

    assert len(movs) == 1
    assert movs[0].lote_id == lote_a.id
    assert movs[0].cantidad == Decimal("-20")
    assert movs[0].costo_unitario == Decimal("10")  # costo del lote A

    db.refresh(lote_a)
    db.refresh(lote_b)
    assert lote_a.cantidad_actual == Decimal("10")
    assert lote_b.cantidad_actual == Decimal("100")


# ---------------------------------------------------------------------------
# 3. FEFO: se reparte entre dos lotes
# ---------------------------------------------------------------------------
def test_fefo_reparte_entre_lotes(db, negocio_id, almacen_principal, producto_simple):
    variante = _get_default_variante(db, producto_simple)

    lote_a = service.crear_lote(db, negocio_id, LoteCreate(
        variante_id=variante.id, almacen_id=almacen_principal.id,
        fecha_vencimiento=date.today() + timedelta(days=60),
        cantidad=Decimal("30"), costo_unitario=Decimal("10"),
    ))
    lote_b = service.crear_lote(db, negocio_id, LoteCreate(
        variante_id=variante.id, almacen_id=almacen_principal.id,
        fecha_vencimiento=date.today() + timedelta(days=200),
        cantidad=Decimal("100"), costo_unitario=Decimal("11"),
    ))

    # Vender 50: 30 del A + 20 del B
    movs = service.crear_movimiento(db, negocio_id, MovimientoCreate(
        variante_id=variante.id, almacen_id=almacen_principal.id,
        tipo="SALIDA_VENTA", cantidad=Decimal("50"), referencia_id="VTA-002",
    ))

    assert len(movs) == 2
    movs_por_lote = {m.lote_id: m for m in movs}
    assert movs_por_lote[lote_a.id].cantidad == Decimal("-30")
    assert movs_por_lote[lote_b.id].cantidad == Decimal("-20")

    db.refresh(lote_a)
    db.refresh(lote_b)
    assert lote_a.cantidad_actual == Decimal("0")
    assert lote_b.cantidad_actual == Decimal("80")


# ---------------------------------------------------------------------------
# 4. Override manual: ignora FEFO y usa el lote indicado
# ---------------------------------------------------------------------------
def test_override_manual_lote_id(db, negocio_id, almacen_principal, producto_simple):
    variante = _get_default_variante(db, producto_simple)

    lote_a = service.crear_lote(db, negocio_id, LoteCreate(
        variante_id=variante.id, almacen_id=almacen_principal.id,
        fecha_vencimiento=date.today() + timedelta(days=30),  # vence antes
        cantidad=Decimal("50"), costo_unitario=Decimal("10"),
    ))
    lote_b = service.crear_lote(db, negocio_id, LoteCreate(
        variante_id=variante.id, almacen_id=almacen_principal.id,
        fecha_vencimiento=date.today() + timedelta(days=300),
        cantidad=Decimal("50"), costo_unitario=Decimal("12"),
    ))

    # Override: forzar venta desde lote B aunque A vence antes
    movs = service.crear_movimiento(db, negocio_id, MovimientoCreate(
        variante_id=variante.id, almacen_id=almacen_principal.id,
        tipo="SALIDA_VENTA", cantidad=Decimal("10"),
        lote_id=lote_b.id,
        referencia_id="VTA-MANUAL",
    ))

    assert len(movs) == 1
    assert movs[0].lote_id == lote_b.id
    db.refresh(lote_a)
    db.refresh(lote_b)
    assert lote_a.cantidad_actual == Decimal("50")
    assert lote_b.cantidad_actual == Decimal("40")


# ---------------------------------------------------------------------------
# 5. Producto que controla vencimiento: rechaza lote sin fecha
# ---------------------------------------------------------------------------
def test_producto_controla_vencimiento_exige_fecha(
    db, negocio_id, almacen_principal, producto_que_controla_vencimiento,
):
    variante = _get_default_variante(db, producto_que_controla_vencimiento)

    with pytest.raises(ValueError, match="requiere fecha_vencimiento"):
        service.crear_lote(db, negocio_id, LoteCreate(
            variante_id=variante.id, almacen_id=almacen_principal.id,
            fecha_vencimiento=None,  # ← forzar el error
            cantidad=Decimal("10"), costo_unitario=Decimal("5"),
        ))


# ---------------------------------------------------------------------------
# 6. Producto que NO controla vencimiento: acepta lote sin fecha
# ---------------------------------------------------------------------------
def test_producto_sin_vencimiento_acepta_lote_sin_fecha(
    db, negocio_id, almacen_principal, producto_simple,  # producto_simple → controla_vencimiento=False
):
    variante = _get_default_variante(db, producto_simple)

    lote = service.crear_lote(db, negocio_id, LoteCreate(
        variante_id=variante.id, almacen_id=almacen_principal.id,
        fecha_vencimiento=None,
        cantidad=Decimal("10"), costo_unitario=Decimal("5"),
    ))
    assert lote.fecha_vencimiento is None
    assert lote.cantidad_actual == Decimal("10")


# ---------------------------------------------------------------------------
# 7. Stock insuficiente: ValueError
# ---------------------------------------------------------------------------
def test_stock_insuficiente_lanza_error(db, negocio_id, almacen_principal, producto_simple):
    variante = _get_default_variante(db, producto_simple)

    service.crear_lote(db, negocio_id, LoteCreate(
        variante_id=variante.id, almacen_id=almacen_principal.id,
        cantidad=Decimal("10"), costo_unitario=Decimal("10"),
    ))

    with pytest.raises(ValueError, match="Stock insuficiente"):
        service.crear_movimiento(db, negocio_id, MovimientoCreate(
            variante_id=variante.id, almacen_id=almacen_principal.id,
            tipo="SALIDA_VENTA", cantidad=Decimal("50"),
        ))


# ---------------------------------------------------------------------------
# 8. Dar de baja lote vencido: crea MERMA_VENCIMIENTO
# ---------------------------------------------------------------------------
def test_dar_baja_lote_vencido(db, negocio_id, almacen_principal, producto_simple):
    variante = _get_default_variante(db, producto_simple)

    lote = service.crear_lote(db, negocio_id, LoteCreate(
        variante_id=variante.id, almacen_id=almacen_principal.id,
        fecha_vencimiento=date.today() - timedelta(days=1),  # ya vencido
        cantidad=Decimal("20"), costo_unitario=Decimal("8"),
    ))

    movimiento = service.dar_baja_lote_vencido(
        db, negocio_id, lote.id, motivo="Vencido en inspección",
    )

    assert movimiento.tipo == "MERMA_VENCIMIENTO"
    assert movimiento.cantidad == Decimal("-20")
    assert movimiento.lote_id == lote.id

    db.refresh(lote)
    assert lote.cantidad_actual == Decimal("0")
    assert lote.activo is False

    # Stock se sincronizó a 0
    stock = db.query(Stock).filter(
        Stock.variante_id == variante.id,
        Stock.almacen_id == almacen_principal.id,
    ).first()
    assert stock.cantidad_actual == Decimal("0")


# ---------------------------------------------------------------------------
# 9. Búsqueda por atributo JSON
# ---------------------------------------------------------------------------
def test_buscar_por_atributo(db, negocio_id, producto_con_variantes_de_talla):
    """
    producto_con_variantes_de_talla genera 3 variantes con atributos:
        {"talla": "S"}, {"talla": "M"}, {"talla": "L"}
    """
    resultado = service.buscar_variantes_por_atributo(
        db, negocio_id, {"talla": "M"},
    )
    assert len(resultado) == 1
    assert resultado[0].atributos.get("talla") == "M"


# ---------------------------------------------------------------------------
# 10. Invariante: Stock = SUM(lotes activos)
# ---------------------------------------------------------------------------
def test_invariante_stock_igual_suma_lotes(db, negocio_id, almacen_principal, producto_simple):
    variante = _get_default_variante(db, producto_simple)

    service.crear_lote(db, negocio_id, LoteCreate(
        variante_id=variante.id, almacen_id=almacen_principal.id,
        cantidad=Decimal("30"), costo_unitario=Decimal("10"),
    ))
    service.crear_lote(db, negocio_id, LoteCreate(
        variante_id=variante.id, almacen_id=almacen_principal.id,
        cantidad=Decimal("70"), costo_unitario=Decimal("11"),
    ))
    service.crear_movimiento(db, negocio_id, MovimientoCreate(
        variante_id=variante.id, almacen_id=almacen_principal.id,
        tipo="SALIDA_VENTA", cantidad=Decimal("25"),
    ))

    from sqlalchemy import func
    suma_lotes = db.query(func.coalesce(func.sum(Lote.cantidad_actual), 0)).filter(
        Lote.variante_id == variante.id,
        Lote.almacen_id == almacen_principal.id,
        Lote.activo == True,
    ).scalar()

    stock = db.query(Stock).filter(
        Stock.variante_id == variante.id,
        Stock.almacen_id == almacen_principal.id,
    ).first()

    assert Decimal(suma_lotes) == stock.cantidad_actual
    assert stock.cantidad_actual == Decimal("75")  # 30+70-25
