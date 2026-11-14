import pytest
import uuid
from decimal import Decimal
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import sqlite3
from sqlalchemy import event
from app.main import application as app
from app.database import Base, get_db

# Importa modelos para que Base.metadata los registre
import app.modules.inventario.models as _  # noqa: F401
import app.modules.ventas.models as _  # noqa: F401
import app.modules.auth.models as _  # noqa: F401
from app.modules.inventario.models import Negocio, Almacen, Producto, Variante

TEST_DATABASE_URL = "sqlite://"

engine_test = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

# CONDICIÓN 4: Habilitar efectivamente claves foráneas en SQLite durante los tests
@event.listens_for(engine_test, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionTest = sessionmaker(bind=engine_test, autocommit=False, autoflush=False)


@pytest.fixture(scope="function")
def db():
    Base.metadata.create_all(bind=engine_test)
    session = SessionTest()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine_test)


@pytest.fixture(scope="function")
def client(db):
    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def negocio_id(db):
    negocio = Negocio(
        id=str(uuid.uuid4()),
        nombre="Tienda Test",
        propietario="Admin Test",
        moneda="BOB",
        activo=True,
    )
    db.add(negocio)
    db.commit()
    db.refresh(negocio)
    return negocio.id


@pytest.fixture
def almacen_principal(db, negocio_id):
    almacen = Almacen(
        id=str(uuid.uuid4()),
        negocio_id=negocio_id,
        nombre="Almacén Principal",
        es_principal=True,
    )
    db.add(almacen)
    db.commit()
    db.refresh(almacen)
    return almacen


@pytest.fixture
def producto_simple(db, negocio_id):
    producto = Producto(
        id=str(uuid.uuid4()),
        negocio_id=negocio_id,
        nombre="Producto Simple Test",
        tiene_variantes=False,
        es_servicio=False,
        controla_vencimiento=False,
        activo=True,
    )
    db.add(producto)
    db.flush()

    variante = Variante(
        id=str(uuid.uuid4()),
        producto_id=producto.id,
        sku="SKU-SIMPLE-01",
        atributos={},
        precio_venta=Decimal("50.00"),
        precio_costo=Decimal("30.00"),
        activa=True,
    )
    db.add(variante)
    db.commit()
    db.refresh(producto)
    return producto


@pytest.fixture
def producto_con_variantes_de_talla(db, negocio_id):
    producto = Producto(
        id=str(uuid.uuid4()),
        negocio_id=negocio_id,
        nombre="Camiseta Test",
        tiene_variantes=True,
        es_servicio=False,
        controla_vencimiento=False,
        activo=True,
    )
    db.add(producto)
    db.flush()

    for talla in ["S", "M", "L"]:
        var = Variante(
            id=str(uuid.uuid4()),
            producto_id=producto.id,
            sku=f"CAM-{talla}",
            atributos={"talla": talla},
            precio_venta=Decimal("80.00"),
            precio_costo=Decimal("40.00"),
            activa=True,
        )
        db.add(var)
    db.commit()
    db.refresh(producto)
    return producto


@pytest.fixture
def producto_que_controla_vencimiento(db, negocio_id):
    producto = Producto(
        id=str(uuid.uuid4()),
        negocio_id=negocio_id,
        nombre="Leche Pasteurizada",
        tiene_variantes=False,
        es_servicio=False,
        controla_vencimiento=True,
        activo=True,
    )
    db.add(producto)
    db.flush()

    variante = Variante(
        id=str(uuid.uuid4()),
        producto_id=producto.id,
        sku="LECHE-1L",
        atributos={},
        precio_venta=Decimal("7.00"),
        precio_costo=Decimal("5.00"),
        activa=True,
    )
    db.add(variante)
    db.commit()
    db.refresh(producto)
    return producto
