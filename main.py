from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.database import check_db_connection

settings = get_settings()


# ---------------------------------------------------------------------------
# Lifespan: código que corre al arrancar y al apagar la app
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    print(f"[APP] Iniciando en modo: {settings.environment}")
    if check_db_connection():
        print("[DB] Conexión exitosa a PostgreSQL")
    else:
        print("[DB] ADVERTENCIA: no se pudo conectar a la base de datos")

    yield  # la app corre aquí

    # --- Shutdown ---
    print("[APP] Apagando...")


# ---------------------------------------------------------------------------
# Instancia principal
# ---------------------------------------------------------------------------
app = FastAPI(
    title="ApoloDigital — Sistema de Inventarios",
    description="API para gestión de inventarios, ventas y catálogo de pymes en LATAM.",
    version="0.1.0",
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Routers — se irán agregando módulo a módulo
# ---------------------------------------------------------------------------
# from app.modules.inventario.router import router as inventario_router
# from app.modules.auth.router import router as auth_router
# app.include_router(inventario_router, prefix="/api/v1", tags=["inventario"])
# app.include_router(auth_router,       prefix="/api/v1", tags=["auth"])


# ---------------------------------------------------------------------------
# Endpoints base
# ---------------------------------------------------------------------------
@app.get("/", tags=["root"])
def root():
    return {
        "app": "ApoloDigital Inventarios",
        "version": "0.1.0",
        "status": "ok",
    }


@app.get("/health", tags=["root"])
def health():
    """
    Railway usa este endpoint para verificar que el servicio está vivo.
    Configúralo en railway.toml como healthcheckPath = "/health"
    """
    db_ok = check_db_connection()
    return {
        "status": "ok" if db_ok else "degraded",
        "db": "connected" if db_ok else "unreachable",
        "environment": settings.environment,
    }
