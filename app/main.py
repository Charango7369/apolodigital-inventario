from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.database import check_db_connection

# Importar modelos ANTES de crear la instancia para que SQLAlchemy los registre
import app.modules.inventario.models  # noqa: F401
import app.modules.ventas.models  # noqa: F401  # <-- NUEVO


settings = get_settings()


@asynccontextmanager
async def lifespan(application: FastAPI):
    print(f"[APP] Iniciando en modo: {settings.environment}")
    if check_db_connection():
        print("[DB] Conexión exitosa a PostgreSQL")
    else:
        print("[DB] ADVERTENCIA: no se pudo conectar a la base de datos")
    yield
    print("[APP] Apagando...")


application = FastAPI(
    title="ApoloDigital — Sistema de Inventarios",
    description="API para gestión de inventarios, ventas y catálogo de pymes en LATAM.",
    version="0.1.0",
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
    lifespan=lifespan,
)

application.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
from app.modules.auth.router import router as auth_router
from app.modules.inventario.router import router as inventario_router  # <-- NUEVO
from app.modules.ventas.router import router as ventas_router
from app.modules.admin.router import router as admin_router
from app.modules.reportes.router import router as reportes_router  # NUEVO


application.include_router(auth_router, prefix="/api/v1", tags=["auth"])
application.include_router(inventario_router, prefix="/api/v1", tags=["inventario"])  # <-- NUEVO
application.include_router(ventas_router, prefix="/api/v1", tags=["ventas"])
application.include_router(admin_router, prefix="/api/v1", tags=["admin"])  
application.include_router(reportes_router, prefix="/api/v1/reportes", tags=["reportes"]) #nuevo

@application.get("/", tags=["root"])
def root():
    return {"app": "ApoloDigital Inventarios", "version": "0.1.0", "status": "ok"}


@application.get("/health", tags=["root"])
def health():
    db_ok = check_db_connection()
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "connected" if db_ok else "unreachable",
        "environment": settings.environment,
    }