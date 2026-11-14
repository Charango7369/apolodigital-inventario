# FASE 3.0 — PRE-RELEASE PACKAGING Y CONSOLIDACIÓN DE GIT
## ApoloDigital Inventarios — Plan Maestro de Consolidación, Versionado y Gobernanza de Código

* **Fecha de Emisión:** 13 de noviembre de 2026  
* **Fase:** 3.0 (Pre-Release Packaging y Consolidación de Git)  
* **Rol:** Arquitecto de Software Senior, Release Manager, DBA PostgreSQL Senior y Auditor DevOps  
* **Principio Rector:** *"Release First, Deploy Later"* (Gobernanza, trazabilidad y reproducibilidad antes de cualquier interacción con producción)  
* **Estado:** Plan Maestro Aprobado para Ejecución  

---

## 1. AUDITORÍA FORENSE DEL ESTADO ACTUAL DE GIT

Se ejecutó una inspección exhaustiva del árbol de trabajo (*working tree*), ramas activas, archivos ignorados y diferencias pendientes en ambos repositorios.

### 1.1 Repositorio Backend (`D:\apolodigital-inventario`)
* **Rama Activa:** `main` (sincronizada en commit base `8e991a139c804265945607c90431e089c634229b`).
* **Archivos Modificados Rastreables (18 archivos, +1,533 / -130 líneas):**
  - Configuración e Infra: `alembic.ini`, `alembic/env.py`, `docker-compose.yml`, `tools/cargar_inventario.py`, `tools/importar_inventario.py`, `test_roles.py`, `test_roles_full.py`.
  - Módulos de Dominio: `app/modules/auth/models.py`, `app/modules/auth/router.py`, `app/modules/auth/service.py`, `app/modules/inventario/models.py`, `app/modules/inventario/schemas.py`.
  - Módulo Financiero POS: `app/modules/ventas/models.py` (534 líneas añadidas), `app/modules/ventas/router.py` (514 líneas añadidas), `app/modules/ventas/schemas.py` (265 líneas añadidas), `app/modules/ventas/service.py` (64 líneas modificadas).
  - Testing y Raíz: `tests/conftest.py`, `dependencies.py`.
* **Archivos Nuevos no Rastreables (*Untracked*, 22 archivos en 4 dominios):**
  - Migraciones Alembic: `f1a2b3c4d5e6...`, `f2a3b4c5d6e7...`, `f3a4b5c6d7e8...`.
  - Dominio e Idempotencia: `app/modules/ventas/idempotency.py`, `app/modules/ventas/services/__init__.py`, `accounts_receivable_service.py`, `payment_service.py`, `sale_cancellation_service.py`, `sale_service.py`.
  - Batería de Pruebas: 13 suites en `tests/` (`test_phase0_fixes.py` hasta `test_ventas_atomicidad.py`).
* **Auditoría de `.gitignore`:**
  - Reglas activas protegen adecuadamente: `.env`, `.env.local`, `.venv/`, `.pytest_cache/`, `__pycache__/`, dumps de base de datos (`*.sql`, `backup_*.sql`) y scripts forenses de debug (`forensic_*`, `infiltracion.py`, `reset_admin.py`).
* **Hallazgos de Sanitización Previa al Commit:**
  - **`dependencies.py` en la raíz:** Archivo histórico vacío que contiene un comentario con bypass de autenticación para desarrollo local (`get_current_user` con id fijo). **Acción:** No debe ser versionado con dicho bypass. Debe ser restaurado a su estado limpio original (`git checkout dependencies.py`).
  - **`alembic.ini`:** La línea `sqlalchemy.url` tiene `postgresql://postgres:admin123@apolo_inventario_db:5432/apolo_inventario`. En producción, `env.py` inyecta dinámicamente `settings.database_url`. Se debe mantener como fallback genérico de desarrollo local.

### 1.2 Repositorio Frontend (`D:\apolodigital-frontend`)
* **Rama Activa:** `main` (sincronizada en commit base `50dc10a1f87b230534c6bbe17b387ac25d9c1d23`).
* **Archivos Modificados (7 archivos, +774 / -109 líneas):**
  - Servicios de Dominio: `src/services/api.js` (233 líneas añadidas), `src/services/offlineDB.js`, `src/services/syncService.js`, `src/services/ticketService.js`.
  - Componentes e Interfaces: `src/pages/POS.jsx` (425 líneas modificadas), `src/pages/Ventas.jsx` (104 líneas añadidas), `src/components/TicketPreview.jsx` (36 líneas añadidas).
* **Archivos Untracked:** `0`.
* **Auditoría de `.gitignore`:**
  - Protege de forma hermética: `node_modules/`, `dist/`, `.env`, `.env.production` y scripts de debug (`extraer_movimientos.sh`).

---

## 2. ESTRATEGIA DE COMMITS ATÓMICOS (*CONVENTIONAL COMMITS*)

El empaquetado no debe realizarse mediante un commit monolítico "catch-all". Se establece una secuencia de commits atómicos clasificados por dominio funcional, garantizando *bisectability* (capacidad de depuración con `git bisect`) y reversibilidad quirúrgica.

### 2.1 Backend (`D:\apolodigital-inventario`): 5 Commits Atómicos

```mermaid
flowchart LR
    C1["1. refactor/fix<br/>Core & Auth"] --> C2["2. feat<br/>Migraciones Alembic"]
    C2 --> C3["3. feat<br/>Motor Financiero"]
    C3 --> C4["4. feat<br/>Idempotencia & API"]
    C4 --> C5["5. test<br/>Suites Automatizadas"]
```

#### Commit 1: Core, Autenticación y Compatibilidad Python 3.12+
* **Tipo:** `refactor(core,auth): sanitizar fechas utc y constraints multitenant`
* **Archivos:**
  - `app/modules/auth/models.py`
  - `app/modules/auth/router.py`
  - `app/modules/auth/service.py`
  - `app/modules/inventario/models.py`
  - `app/modules/inventario/schemas.py`
  - `docker-compose.yml`
  - `tools/cargar_inventario.py`
  - `tools/importar_inventario.py`
  - `test_roles.py`
  - `test_roles_full.py`
* **Justificación Técnica:** Reemplaza `datetime.utcnow()` deprecado por `datetime.now(timezone.utc)`, añade la restricción compuesta `UniqueConstraint("negocio_id", "id", name="uq_users_negocio_id")` requerida por las claves foráneas de ventas, agrega el tipo de movimiento `ENTRADA` para cancelaciones y parametriza `API_URL` en scripts auxiliares.

#### Commit 2: Migraciones Alembic y Corrección de Esquema Físico
* **Tipo:** `feat(migrations): registrar migraciones financieras f1-f3 y fix varchar 30`
* **Archivos:**
  - `alembic.ini`
  - `alembic/env.py`
  - `alembic/versions/f1a2b3c4d5e6_fase1_3_financiero_pos_pagos_cxc.py`
  - `alembic/versions/f2a3b4c5d6e7_fase2_2_clasificacion_pagos.py`
  - `alembic/versions/f3a4b5c6d7e8_fase2_5_idempotencia.py`
* **Justificación Técnica:** Incorpora las 3 revisiones DDL financieras garantizando linealidad estricta (`e7` $\to$ `f1` $\to$ `f2` $\to$ `f3`), guardias de seguridad D-02/D-03 contra downgrades destructivos, y la corrección certificada del hallazgo `H-2.7.4-01` (`VARCHAR(30)` en `cuentas_por_cobrar.estado`).

#### Commit 3: Modelos, Esquemas y Servicios del Dominio Financiero
* **Tipo:** `feat(ventas): implementar motor financiero de pagos inmutables, cartera y cancelacion`
* **Archivos:**
  - `app/modules/ventas/models.py`
  - `app/modules/ventas/schemas.py`
  - `app/modules/ventas/service.py`
  - `app/modules/ventas/services/__init__.py`
  - `app/modules/ventas/services/sale_service.py`
  - `app/modules/ventas/services/payment_service.py`
  - `app/modules/ventas/services/sale_cancellation_service.py`
  - `app/modules/ventas/services/accounts_receivable_service.py`
* **Justificación Técnica:** Registra las entidades ORM `PagoVenta`, `ConciliacionPago`, `ReversionPago`, `CuentaPorCobrar`, la modularización en servicios de dominio y las reglas de negocio de inmutabilidad contable, FEFO y cálculo dinámico de mora en zona `America/La_Paz`.

#### Commit 4: Capa de Idempotencia y Contratos de Endpoints REST
* **Tipo:** `feat(idempotency,api): integrar interceptor sha-256 y 17 endpoints pos`
* **Archivos:**
  - `app/modules/ventas/idempotency.py`
  - `app/modules/ventas/router.py`
* **Justificación Técnica:** Introduce el interceptor con hashing canónico SHA-256, exclusión de claves en vuelo (`PROCESSING`), replay con cabecera `X-Idempotent-Replay: true` y la exposición controlada de los 17 endpoints financieros certificados.

#### Commit 5: Batería de Pruebas de Regresión y Concurrencia PostgreSQL
* **Tipo:** `test(qa): incorporar suite integral de 131 pruebas unitarias, integracion y concurrencia`
* **Archivos:**
  - `tests/conftest.py`
  - `tests/test_phase0_fixes.py`
  - `tests/test_phase1_models.py`
  - `tests/test_phase1_3_migration.py`
  - `tests/test_phase2_migration.py`
  - `tests/test_phase2_sale_service.py`
  - `tests/test_phase2_payment_service.py`
  - `tests/test_phase2_sale_cancellation.py`
  - `tests/test_phase2_accounts_receivable.py`
  - `tests/test_phase2_endpoints_http.py`
  - `tests/test_phase2_idempotency_atomic.py`
  - `tests/test_phase2_concurrency_postgresql.py`
  - `tests/test_phase2_6_pos_integration_audit.py`
  - `tests/test_ventas_atomicidad.py`
* **Justificación Técnica:** Garantiza que el código versionado preserve la suite de 131 pruebas automatizadas para auditoría y CI/CD continuo.

---

### 2.2 Frontend (`D:\apolodigital-frontend`): 2 Commits Atómicos

#### Commit 1: Capa de Servicios, Persistencia Offline e Idempotencia
* **Tipo:** `feat(services): cliente api con idempotencia x-idempotency-key e indexeddb v2`
* **Archivos:**
  - `src/services/api.js`
  - `src/services/offlineDB.js`
  - `src/services/syncService.js`
  - `src/services/ticketService.js`
* **Justificación Técnica:** Implementa la generación e inyección de UUID en `X-Idempotency-Key`, esquema v2 de IndexedDB, aislamiento de errores 409/422 en la cola de sincronización y formateo de tickets térmicos multimoneda con estados financieros.

#### Commit 2: Interfaz POS, Carrito y Modalidades de Cobro
* **Tipo:** `feat(pos-ui): cobro mixto, credito puro, cancelacion con reembolso y badges financieros`
* **Archivos:**
  - `src/pages/POS.jsx`
  - `src/pages/Ventas.jsx`
  - `src/components/TicketPreview.jsx`
* **Justificación Técnica:** Añade el cálculo dinámico de cambio en efectivo, validación estricta de cliente y vencimiento en ventas a crédito, confirmación de QR, opción de devolución de efectivo en cancelaciones e historial visual de pagos.

---

## 3. VALIDACIÓN PREVIA AL RELEASE

Para garantizar que el empaquetado sea hermético, se establecen las siguientes pruebas de verificación obligatorias que deben ejecutarse **inmediatamente después de aplicar los commits**:

| Componente | Comando de Verificación | Criterio de Éxito No Negociable |
| :--- | :--- | :--- |
| **Backend Tests** | `python -m pytest tests/ -q` | `131 passed, 0 failed, 0 warnings` |
| **Alembic Check** | `python -m alembic heads` | Exactamente `f3a4b5c6d7e8 (head)`, sin cabezas huérfanas |
| **Alembic Current** | `python -m alembic current` | `f3a4b5c6d7e8 (head)` en base de ensayo |
| **Frontend Build** | `npm run build` | Compilación en `< 15s`, `dist/` generado, 0 errores JSX |
| **Git Working Tree** | `git status -s` (en ambos repos) | Salida vacía (*working tree clean*) |
| **Variables de Entorno**| `git check-ignore -v .env` | Archivos `.env` efectivamente ignorados |

---

## 4. ESTRATEGIA DE VERSIONADO FORMAL (SemVer 2.0.0)

### 4.1 Análisis Comparativo de Opciones

| Candidato | Evaluación Técnica | Dictamen |
| :--- | :--- | :---: |
| **`v2.7.0`** | Mezcla la numeración de las fases de auditoría interna (2.1 - 2.7) con la versión del software. Viola SemVer al saltar a una versión mayor 2.x sin un historial previo 1.x en producción. | **Descartada** |
| **`v1.0.0`** | Representa el primer producto terminado y comercialmente viable. Sin embargo, marcarlo directamente como GA (*General Availability*) antes de verificar el despliegue en vivo en Railway omite la etapa de Release Candidate. | **Diferida a Fase 3.1** |
| **`v1.0.0-rc1`** | **Elección de Ingeniería Óptima.** Denota formalmente la primera versión candidata a producción (*Release Candidate 1*), congelada, certificada localmente al 100% y lista para ser desplegada en la ventana de producción. | **RECOMENDADA** |

### 4.2 Política de Tags y Ramas
1. **Rama de Release:** `release/v1.0.0` (creada a partir de `main`). Permite aislar el código auditado y realizar el despliegue controlado sin interferencias de desarrollo futuro.
2. **Tag Anotado:** `v1.0.0-rc1` con firma y mensaje de release detallado.
3. **Promoción a GA (`v1.0.0`):** Una vez ejecutado el runbook de la Fase 3.1 en Railway y aprobados los smoke tests en producción, el mismo commit se etiqueta formalmente como `v1.0.0`.

---

## 5. GESTIÓN DE RIESGOS Y CRITERIOS STOP-THE-LINE

| Riesgo Técnico Residual | Probabilidad | Impacto | Medida de Mitigación / Protocolo |
| :--- | :---: | :---: | :--- |
| **R-3.0-01: Inclusión accidental de secretos en el índice de Git** | Baja | Crítico | Auditoría previa con `git status -uall`. El archivo `.env` está en `.gitignore`. No usar jamás `git add .` indiscriminado. |
| **R-3.0-02: Regresión en tests post-commit** | Muy Baja | Alto | Ejecutar `pytest` inmediatamente después del commit 5 para comprobar que no faltó ningún archivo auxiliar. |
| **R-3.0-03: Divergencia con remoto (`origin/main`)** | Baja | Medio | Comprobado: ambas ramas están al día con sus remotos (`up to date`). En caso de conflicto inesperado, abortar inmediatamente sin hacer push. |

### Criterios Stop-the-Line (Parada de Emergencia)
El proceso de consolidación debe abortarse de inmediato si:
- Un archivo `.env`, `.sql` o credencial privada es detectado en el `staged changes` (`git diff --staged`).
- La ejecución de `pytest tests/ -q` reporta menos de 131 pruebas aprobadas o algún fallo.
- El comando `npm run build` falla en el empaquetado de producción.
- `git status` muestra archivos no reconocidos pendientes de categorizar.

---

## 6. CHECKLIST DE SALIDA (EXIT CRITERIA FASE 3.0)

Para declarar formalmente cerrada la Fase 3.0 y autorizar el paso a la Fase 3.1, se deben cumplir todas las condiciones siguientes:

- [ ] Archivo `dependencies.py` en la raíz restaurado a su estado original limpio (sin bypasses).
- [ ] 5 commits atómicos creados en el repositorio backend (`D:\apolodigital-inventario`).
- [ ] 2 commits atómicos creados en el repositorio frontend (`D:\apolodigital-frontend`).
- [ ] Rama `release/v1.0.0` creada y activa en ambos repositorios.
- [ ] Tag anotado `v1.0.0-rc1` generado en ambos repositorios.
- [ ] Ambos árboles de trabajo en estado estrictamente limpio (*working tree clean*).
- [ ] Batería de 131 pruebas ejecutada con 100% de éxito en el estado consolidado.
- [ ] Build de producción de Vite completado sin errores en el estado consolidado.
- [ ] Ninguna operación remota (`git push`, cambios en Railway) ejecutada sin autorización expresa.

---

## 7. DICTAMEN DE LA FASE 3.0

> [!IMPORTANT]
> # DICTAMEN TÉCNICO: APTO PARA CONSOLIDACIÓN Y EMPAQUETADO
> 
> El Plan Maestro de la Fase 3.0 cumple rigurosamente con los estándares de ingeniería de software, arquitectura multi-tenant y auditoría de sistemas transaccionales.
> 
> La aplicación de este plan **mitiga de forma definitiva el riesgo `R-2.7-01`**, dejando el código en un estado 100% trazable, reversible y preparado para el despliegue controlado de la Fase 3.1.
> 
> *Principio de Gobernanza:* Se requiere aprobación expresa para ejecutar la secuencia de comandos Git de consolidación local.
