from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.pool import NullPool
from app.config import get_settings

settings = get_settings()

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
# NullPool es importante en Railway: evita conexiones colgadas entre requests.
# Para SQLite local en tests se cambia a StaticPool automáticamente (ver abajo).

_engine_kwargs: dict = {
    "echo": settings.debug,          # loguea SQL cuando DEBUG=true
    "pool_pre_ping": True,            # verifica conexión antes de usarla
}

if settings.database_url.startswith("sqlite"):
    # SQLite solo se usa en tests locales — no en Railway
    from sqlalchemy.pool import StaticPool
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
    _engine_kwargs["poolclass"] = StaticPool
else:
    # PostgreSQL en Railway
    _engine_kwargs["poolclass"] = NullPool

engine = create_engine(settings.database_url, **_engine_kwargs)

# Activa WAL mode si es SQLite (solo tests)
if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_conn, _):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,   # evita lazy-load sorpresivo después del commit
)


# ---------------------------------------------------------------------------
# Base declarativa — todos los modelos heredan de aquí
# ---------------------------------------------------------------------------
class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Dependencia FastAPI
# ---------------------------------------------------------------------------
def get_db():
    """
    Generador que entrega una sesión de DB por request y la cierra al terminar.

    Uso en un endpoint:
        from app.database import get_db
        from sqlalchemy.orm import Session

        @router.get("/algo")
        def mi_endpoint(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Healthcheck de conexión — usado por main.py al arrancar
# ---------------------------------------------------------------------------
def check_db_connection() -> bool:
    """
    Verifica que la base de datos sea alcanzable.
    Retorna True si la conexión es exitosa, False si no.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        print(f"[DB] Fallo de conexión: {exc}")
        return False
