# CLAUDE.md — apolodigital-inventario

> Contexto de proyecto para Claude Code. Leer antes de cualquier acción.

---

## Descripción del sistema

SaaS de inventario y punto de venta (POS) multi-tenant para pequeños y medianos comercios en Bolivia.
Desarrollado por **ApoloDigital** — Apolo, Franz Tamayo, La Paz, Bolivia.

- **Backend:** FastAPI + SQLAlchemy + Alembic + PostgreSQL
- **Frontend:** React + Vite + Tailwind CSS (repo separado: `apolodigital-frontend`)
- **Deploy backend:** Railway (GitHub push → autodeploy)
- **Deploy frontend:** Cloudflare Pages (GitHub push → autodeploy)
- **Dominio:** apolodigital.lat (DNS en Cloudflare)
- **SUPERADMIN:** admin@apolodigital.lat

---

## Entorno de desarrollo

- **OS:** Windows 11 con Git Bash (MINGW64) — **nunca asumir PowerShell**
- **Editor:** VS Code
- **Python:** 3.12 (pinned en Dockerfile con `python:3.12-slim`)
- **Deploy:** `git push origin main` → Railway ejecuta automáticamente
- **Tests locales:** pytest con SQLite in-memory via `conftest.py`
- **GitHub:** `Charango7369/apolodigital-inventario`

---

## Estructura del proyecto

```
apolodigital-inventario/
├── app/
│   ├── main.py                  # FastAPI app, CORS, routers
│   ├── config.py                # Variables de entorno, DATABASE_URL
│   ├── database.py              # Engine SQLAlchemy, SessionLocal
│   ├── models.py                # Modelos SQLAlchemy (todos en un archivo)
│   └── modules/
│       ├── auth/                # JWT, login, usuarios
│       ├── inventario/          # productos, variantes, stock, movimientos
│       ├── ventas/              # POS, tickets, completar venta
│       ├── admin/               # CRUD negocios, almacenes, categorías, proveedores
│       └── superadmin/          # Panel multi-tenant (solo admin@apolodigital.lat)
├── alembic/
│   └── versions/                # Migraciones — NUNCA editar manualmente
├── tests/
├── Dockerfile                   # python:3.12-slim (Nixpacks descartado)
├── railway.toml                 # Config declarativa Railway
├── alembic.ini                  # Debe estar en git (git add -f si es necesario)
└── requirements.txt
```

---

## Schema de base de datos (8 tablas)

| Tabla | Descripción |
|-------|-------------|
| `negocios` | Raíz del tenant. Cada negocio es un tenant aislado |
| `categorias` | Categorías de productos por negocio |
| `proveedores` | Proveedores por negocio |
| `almacenes` | Ubicaciones físicas por negocio. Se crea "Principal" automáticamente |
| `productos` | Producto base con campo `tiene_variantes` (Opción D) |
| `variantes` | Mínimo una variante por producto (default si `tiene_variantes=False`) |
| `stock` | Clave compuesta `variante_id × almacen_id`. Nunca se elimina, solo se ajusta |
| `movimientos_stock` | Log inmutable de entradas/salidas. Nunca UPDATE, solo INSERT |

### Reglas críticas del schema

- `movimientos_stock` es un **audit log inmutable** — solo INSERT, jamás UPDATE ni DELETE
- `stock` se actualiza sumando/restando desde `movimientos_stock`, no directamente
- Al crear un negocio nuevo, se debe crear automáticamente el almacén "Principal"
- `precio_costo` vive en `variantes`, no en `productos` — propagar siempre al guardar
- UUID explícitos en inserts SQL directos (`gen_random_uuid()` falla en Railway Query Console)
- La tabla `stock` requiere `actualizado_at` explícito en INSERTs (NOT NULL sin default)

---

## Arquitectura: Opción D (híbrido variante-default)

Todos los productos tienen al menos una variante. Si `tiene_variantes=False`, hay una variante default transparente al usuario.

```
producto (tiene_variantes=False)
  └── variante_default (talla=None, color=None)
        └── stock (almacen_principal)

producto (tiene_variantes=True)
  ├── variante "Talla S"
  ├── variante "Talla M"
  └── variante "Talla L"
        └── stock por almacen
```

---

## Comandos frecuentes

```bash
# Correr backend local
uvicorn app.main:app --reload

# Crear migración nueva
alembic revision --autogenerate -m "descripción del cambio"

# Aplicar migraciones
alembic upgrade head

# Tests
pytest tests/ -v

# Deploy (desde Git Bash en Windows)
git add .
git commit -m "descripción"
git push origin main
```

---

## Dockerfile (no usar Nixpacks)

```dockerfile
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y gcc libpq-dev && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT"]
```

**Importante:** El CMD corre migraciones ANTES de arrancar uvicorn. No separar estos pasos.

---

## Variables de entorno (Railway)

| Variable | Descripción |
|----------|-------------|
| `DATABASE_URL` | Usar `DATABASE_PUBLIC_URL` de Railway (postgres.railway.internal falla) |
| `SECRET_KEY` | JWT secret |
| `PORT` | Railway la inyecta automáticamente |
| `ENVIRONMENT` | `production` o `development` |

**Gotcha conocido:** Railway entrega `postgres://` — el código debe normalizar a `postgresql://` para SQLAlchemy.

---

## Multi-tenancy

- Cada `negocio` es un tenant completamente aislado
- El SUPERADMIN (`admin@apolodigital.lat`) puede ver y gestionar todos los negocios
- El panel SUPERADMIN usa "Option A" (asistido por equipo ApoloDigital, no self-service) — diseño por contexto boliviano con pagos offline
- Los endpoints de negocio filtran siempre por `negocio_id` del token JWT

---

## POS (Punto de Venta)

- Flujo: agregar productos al carrito → procesar pago → generar ticket → imprimir ticket térmico
- El endpoint `completar` requiere body JSON con `metodo_pago`
- Los productos en carrito usan `variante_id`, no `producto_id`
- El POS autoselecciona el almacén con persistencia en `localStorage`
- Soporte de código de barras: escáner HID + input manual + fallback offline
- Ventas completadas se registran como COMPLETADA desde el inicio — no hacer doble `completarVenta()`

---

## PWA / Offline

- Service Worker cachea assets estáticos
- `NetworkOnly` para `/api/ventas` (pedidos no se cachean)
- `NetworkFirst` para `/api/productos` (sirve caché si el backend tarda > 5s)
- IndexedDB para ventas offline pendientes de sync
- Archivos PWA (`manifest.json`, `sw.js`) deben estar en `public/`, no en `src/`

---

## Errores conocidos y soluciones

| Error | Solución |
|-------|----------|
| `pip not found` en Railway deploy | Usar Dockerfile, no Nixpacks |
| `postgres.railway.internal` falla | Usar `DATABASE_PUBLIC_URL` |
| `pydantic-core` incompatible | Pinear `python:3.12-slim` (no 3.13) |
| `alembic.ini` no en git | `git add -f alembic.ini` |
| `gen_random_uuid()` falla en Railway Console | Usar UUIDs explícitos como strings |
| Migraciones corren pero uvicorn no arranca | CMD debe ser `sh -c "alembic upgrade head && uvicorn ..."` |
| `precio_costo` se borra al editar producto | Siempre propagar desde producto a variante default al guardar |
| POS dice "Selecciona un almacén" | El almacén Principal no existe — verificar auto-creación al crear negocio |

---

## Estado actual del sistema (mayo 2026)

| Módulo | Estado |
|--------|--------|
| PWA offline + sync | ✅ |
| POS con tickets térmicos | ✅ |
| Código de barras HID + input + offline | ✅ |
| Productos + precio_costo | ✅ |
| Categorías | ✅ |
| Almacenes CRUD + autoselección POS | ✅ |
| Movimientos de inventario | ✅ |
| Proveedores | ✅ |
| Ventas + reportes | ✅ |
| Panel SUPERADMIN multi-tenant | ✅ |
| Alertas de stock bajo | ⏳ Pendiente (espera datos reales de venta) |
| Snapshot de precios en ventas | ⏳ Pendiente |
| Clientes registrados | ⏳ Pendiente |
| Variantes con tallas/colores (`es_default`) | ⏳ Pendiente |

---

## Convenciones de código

- Rutas API: `/api/v1/<módulo>/<recurso>`
- Todos los endpoints protegidos usan `Depends(get_current_user)`
- Schemas Pydantic separados: `*Create`, `*Update`, `*Response`
- Servicios en `service.py`, rutas en `router.py`, modelos en `models.py`
- No usar `async` en funciones que no lo necesiten (Railway no requiere asyncpg)
- Commits en español, descriptivos: `fix:`, `feat:`, `refactor:`

---

*ApoloDigital — Apolo, Franz Tamayo, La Paz, Bolivia 🇧🇴*
