"""
Tests del módulo inventario — capa de modelos y base de datos.
Usan SQLite en memoria vía el fixture db de conftest.py.
"""
from decimal import Decimal
from app.modules.inventario.models import (
    Negocio, Categoria, Proveedor, Almacen,
    Producto, Variante, Stock, MovimientoStock,
)


# ---------------------------------------------------------------------------
# Fixtures de datos reutilizables dentro de este archivo
# ---------------------------------------------------------------------------
def crear_negocio(db) -> Negocio:
    negocio = Negocio(nombre="Tienda Don Edwin", propietario="Edwin", moneda="BOB")
    db.add(negocio)
    db.commit()
    db.refresh(negocio)
    return negocio


def crear_almacen(db, negocio: Negocio) -> Almacen:
    almacen = Almacen(negocio_id=negocio.id, nombre="Almacén principal", es_principal=True)
    db.add(almacen)
    db.commit()
    db.refresh(almacen)
    return almacen


def crear_producto_simple(db, negocio: Negocio) -> tuple[Producto, Variante]:
    """Crea un producto simple con su variante por defecto."""
    producto = Producto(
        negocio_id=negocio.id,
        nombre="Arroba de azúcar",
        unidad_medida="kg",
        tiene_variantes=False,
        es_servicio=False,
    )
    db.add(producto)
    db.flush()  # necesitamos el id para la variante

    variante = Variante(
        producto_id=producto.id,
        sku="AZUCAR-25KG",
        atributos={},
        precio_venta=Decimal("85.00"),
        precio_costo=Decimal("70.00"),
    )
    db.add(variante)
    db.commit()
    db.refresh(producto)
    db.refresh(variante)
    return producto, variante


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_crear_negocio(db):
    negocio = crear_negocio(db)
    assert negocio.id is not None
    assert negocio.nombre == "Tienda Don Edwin"
    assert negocio.moneda == "BOB"
    assert negocio.activo is True


def test_crear_almacen(db):
    negocio = crear_negocio(db)
    almacen = crear_almacen(db, negocio)
    assert almacen.id is not None
    assert almacen.es_principal is True
    assert almacen.negocio_id == negocio.id


def test_crear_producto_con_variante(db):
    negocio = crear_negocio(db)
    producto, variante = crear_producto_simple(db, negocio)

    assert producto.nombre == "Arroba de azúcar"
    assert producto.tiene_variantes is False
    assert variante.sku == "AZUCAR-25KG"
    assert variante.precio_venta == Decimal("85.00")
    assert variante.atributos == {}


def test_relacion_producto_variantes(db):
    negocio = crear_negocio(db)
    producto, variante = crear_producto_simple(db, negocio)

    # Recarga desde DB para probar la relación
    producto_db = db.get(Producto, producto.id)
    assert len(producto_db.variantes) == 1
    assert producto_db.variantes[0].sku == "AZUCAR-25KG"


def test_stock_inicial_en_cero(db):
    negocio = crear_negocio(db)
    almacen = crear_almacen(db, negocio)
    _, variante = crear_producto_simple(db, negocio)

    stock = Stock(
        variante_id=variante.id,
        almacen_id=almacen.id,
        cantidad_actual=Decimal("0"),
        cantidad_minima=Decimal("5"),
    )
    db.add(stock)
    db.commit()
    db.refresh(stock)

    assert stock.cantidad_actual == Decimal("0")
    assert stock.cantidad_minima == Decimal("5")


def test_movimiento_entrada_actualiza_stock(db):
    negocio = crear_negocio(db)
    almacen = crear_almacen(db, negocio)
    _, variante = crear_producto_simple(db, negocio)

    # Crear stock en cero
    stock = Stock(variante_id=variante.id, almacen_id=almacen.id, cantidad_actual=Decimal("0"))
    db.add(stock)
    db.flush()

    # Registrar movimiento de entrada
    movimiento = MovimientoStock(
        variante_id=variante.id,
        almacen_id=almacen.id,
        tipo="ENTRADA_COMPRA",
        cantidad=Decimal("50"),
        costo_unitario=Decimal("70.00"),
        motivo="Compra inicial proveedor Apolo",
    )
    db.add(movimiento)

    # Actualizar stock manualmente (luego esto lo hace service.py)
    stock.cantidad_actual += movimiento.cantidad
    db.commit()
    db.refresh(stock)

    assert stock.cantidad_actual == Decimal("50")
    assert movimiento.tipo == "ENTRADA_COMPRA"


def test_alerta_stock_bajo(db):
    negocio = crear_negocio(db)
    almacen = crear_almacen(db, negocio)
    _, variante = crear_producto_simple(db, negocio)

    # Stock actual (3) menor que mínimo (5) → debe aparecer en alertas
    stock = Stock(
        variante_id=variante.id,
        almacen_id=almacen.id,
        cantidad_actual=Decimal("3"),
        cantidad_minima=Decimal("5"),
    )
    db.add(stock)
    db.commit()

    # Consulta de alertas: cantidad_actual <= cantidad_minima
    alertas = (
        db.query(Stock)
        .filter(Stock.cantidad_actual <= Stock.cantidad_minima)
        .all()
    )
    assert len(alertas) == 1
    assert alertas[0].variante_id == variante.id


def test_producto_con_variantes_multiples(db):
    negocio = crear_negocio(db)
    producto = Producto(
        negocio_id=negocio.id,
        nombre="Polera Apolo",
        tiene_variantes=True,
    )
    db.add(producto)
    db.flush()

    tallas = [("S", "90.00"), ("M", "90.00"), ("L", "95.00")]
    for talla, precio in tallas:
        v = Variante(
            producto_id=producto.id,
            sku=f"POLERA-{talla}",
            atributos={"talla": talla, "color": "azul"},
            precio_venta=Decimal(precio),
        )
        db.add(v)
    db.commit()
    db.refresh(producto)

    assert len(producto.variantes) == 3
    skus = [v.sku for v in producto.variantes]
    assert "POLERA-M" in skus


def test_unicidad_stock_variante_almacen(db):
    """No puede haber dos registros de stock para la misma variante+almacén."""
    import pytest
    from sqlalchemy.exc import IntegrityError

    negocio = crear_negocio(db)
    almacen = crear_almacen(db, negocio)
    _, variante = crear_producto_simple(db, negocio)

    db.add(Stock(variante_id=variante.id, almacen_id=almacen.id, cantidad_actual=Decimal("10")))
    db.commit()

    db.add(Stock(variante_id=variante.id, almacen_id=almacen.id, cantidad_actual=Decimal("5")))
    with pytest.raises(IntegrityError):
        db.commit()
