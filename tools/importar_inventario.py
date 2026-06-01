#!/usr/bin/env python3
"""
Importador masivo de inventario para ApoloDigital.
Lee un Excel del cliente y crea categorías, productos y lotes vía API.

USO:
  1. Editar la sección CONFIG con tu URL, email y archivo
  2. Adaptar la función mapear_fila() a las columnas REALES del Excel del cliente
  3. Correr con DRY_RUN=True primero para validar parsing
  4. Cuando se vea bien, cambiar a DRY_RUN=False y ejecutar el alta real

Dependencias: pip install requests pandas openpyxl
"""
import getpass, sys, time
from datetime import datetime, date
from pathlib import Path
import requests
import pandas as pd

# ============================================================================
# CONFIG ──── EDITAR ESTO
# ============================================================================
API_URL = "https://apolodigital-inventario-production.up.railway.app/api/v1"
ADMIN_EMAIL = "edwinjrivero@yahoo.com"

ARCHIVO = "test_import.xlsx"   # ruta al archivo del cliente
HOJA = 0                                  # nombre o índice de la hoja
ALMACEN_NOMBRE = "Principal"              # almacén destino (debe existir)
UNIDAD_MEDIDA_DEFAULT = "unidad"          # "unidad", "kg", "litro", etc.

DRY_RUN = False   # ← cambiá a False solo después de validar el parsing

# ============================================================================
# MAPEO ──── ADAPTAR A LAS COLUMNAS REALES DEL EXCEL DEL CLIENTE
# ============================================================================
def mapear_fila(row):
    """
    Recibe una fila del Excel, devuelve dict normalizado.
    Cambiá los nombres de columnas según lo que tenga el archivo real.
    """
    return {
        "categoria":         _str(row.get("CATEGORIA"))         or "Sin categoría",
        "nombre":            _str(row.get("PRODUCTO"))          or _str(row.get("NOMBRE")) or "",
        "cantidad":          _num(row.get("CANTIDAD"))          or _num(row.get("STOCK")) or 0,
        "costo_unitario":    _num(row.get("COSTO"))             or _num(row.get("COSTO_UNITARIO")) or 0,
        "precio_venta":      _num(row.get("PRECIO_VENTA"))      or _num(row.get("PRECIO")) or 0,
        "codigo_lote":       _str(row.get("LOTE"))              or _str(row.get("CODIGO_LOTE")),
        "fecha_vencimiento": _fecha(row.get("VENCIMIENTO"))     or _fecha(row.get("FECHA_VENCIMIENTO")),
        "codigo_barras":     _str(row.get("CODIGO_BARRAS"))     or _str(row.get("BARCODE")),
        "proveedor":         _str(row.get("PROVEEDOR")),
        "unidad_medida":     _str(row.get("UNIDAD"))            or UNIDAD_MEDIDA_DEFAULT,
    }

def _str(v):
    if v is None or pd.isna(v): return None
    s = str(v).strip()
    return s or None

def _num(v):
    if v is None or pd.isna(v): return 0
    try: return float(v)
    except: return 0

def _fecha(v):
    if v is None or pd.isna(v): return None
    if isinstance(v, (datetime, date)): return v.strftime("%Y-%m-%d")
    try: return pd.to_datetime(v).strftime("%Y-%m-%d")
    except: return None

# ============================================================================
# CLIENTE API
# ============================================================================
class API:
    def __init__(self, url, token):
        self.url = url
        self.h = {"Authorization": f"Bearer {token}"}
        self._cat = {}
        self._prov = {}
        self._alm = {}

    def _get(self, path):
        r = requests.get(f"{self.url}{path}", headers=self.h)
        r.raise_for_status()
        return r.json()

    def _post(self, path, data):
        r = requests.post(f"{self.url}{path}", headers=self.h, json=data)
        if not r.ok:
            raise RuntimeError(f"POST {path} → {r.status_code}: {r.text[:200]}")
        return r.json()

    def cache_inicial(self):
        for a in self._get("/almacenes"):
            self._alm[a["nombre"].lower()] = a
        for c in self._get("/categorias"):
            self._cat[c["nombre"].lower()] = c
        for p in self._get("/proveedores"):
            self._prov[p["nombre"].lower()] = p

    def find_or_create_categoria(self, nombre):
        k = nombre.lower()
        if k in self._cat: return self._cat[k]["id"]
        new = self._post("/categorias", {"nombre": nombre})
        self._cat[k] = new
        return new["id"]

    def find_or_create_proveedor(self, nombre):
        if not nombre: return None
        k = nombre.lower()
        if k in self._prov: return self._prov[k]["id"]
        new = self._post("/proveedores", {"nombre": nombre})
        self._prov[k] = new
        return new["id"]

    def crear_producto(self, nombre, categoria_id, proveedor_id, codigo_barras, unidad, precio_venta):
        data = {"nombre": nombre, "unidad_medida": unidad, "precio_venta": precio_venta}
        if categoria_id: data["categoria_id"] = categoria_id
        if proveedor_id: data["proveedor_id"] = proveedor_id
        if codigo_barras: data["codigo_barras"] = codigo_barras
        return self._post("/productos", data)

    def get_variante_default(self, producto_id):
        """Asume que crear_producto generó una variante por defecto."""
        prod = self._get(f"/productos/{producto_id}")
        variantes = prod.get("variantes", [])
        if not variantes:
            raise RuntimeError(f"producto {producto_id} no tiene variantes")
        return variantes[0]["id"]

    def crear_lote(self, variante_id, almacen_id, cantidad, costo, fecha_venc, codigo_lote):
        data = {
            "variante_id": variante_id,
            "almacen_id": almacen_id,
            "cantidad": cantidad,
        }
        if costo: data["costo_unitario"] = costo
        if fecha_venc: data["fecha_vencimiento"] = fecha_venc
        if codigo_lote: data["codigo_lote"] = codigo_lote
        return self._post("/lotes", data)

# ============================================================================
# MAIN
# ============================================================================
def main():
    pw = getpass.getpass(f"Password de {ADMIN_EMAIL}: ")
    r = requests.post(f"{API_URL}/auth/login",
                      data={"username": ADMIN_EMAIL, "password": pw})
    if not r.ok:
        print(f"X login: {r.text}"); sys.exit(1)
    api = API(API_URL, r.json()["access_token"])
    api.cache_inicial()

    if ALMACEN_NOMBRE.lower() not in api._alm:
        print(f"X almacén '{ALMACEN_NOMBRE}' no existe. Disponibles: {list(api._alm.keys())}")
        sys.exit(1)
    almacen_id = api._alm[ALMACEN_NOMBRE.lower()]["id"]

    if not Path(ARCHIVO).exists():
        print(f"X archivo no encontrado: {ARCHIVO}"); sys.exit(1)

    df = pd.read_excel(ARCHIVO, sheet_name=HOJA)
    print(f"\nLeídas {len(df)} filas de {ARCHIVO}")
    print(f"Columnas detectadas: {list(df.columns)}")

    if DRY_RUN:
        print("\n*** MODO DRY-RUN — no se crea nada en el sistema ***\n")

    exitos, fallos = 0, []
    for i, row in df.iterrows():
        try:
            r = mapear_fila(row)
            if not r["nombre"]:
                continue

            print(f"  [{i+1:4}] {r['nombre'][:40]:40} | cat={r['categoria'][:25]:25} | qty={r['cantidad']:>5} | venc={r['fecha_vencimiento'] or '—'}")

            if DRY_RUN:
                exitos += 1
                continue

            cat_id  = api.find_or_create_categoria(r["categoria"])
            prov_id = api.find_or_create_proveedor(r["proveedor"])

            prod = api.crear_producto(
                nombre=r["nombre"], categoria_id=cat_id, proveedor_id=prov_id,
                codigo_barras=r["codigo_barras"], unidad=r["unidad_medida"],
                precio_venta=r["precio_venta"],
            )
            variante_id = api.get_variante_default(prod["id"])

            if r["cantidad"] > 0:
                api.crear_lote(
                    variante_id=variante_id, almacen_id=almacen_id,
                    cantidad=r["cantidad"], costo=r["costo_unitario"],
                    fecha_venc=r["fecha_vencimiento"], codigo_lote=r["codigo_lote"],
                )
            exitos += 1
            time.sleep(0.1)  # no saturar API
        except Exception as e:
            fallos.append((i+1, r.get("nombre", "?"), str(e)))

    print(f"\n=== RESUMEN ===")
    print(f"  Éxitos: {exitos}")
    print(f"  Fallos: {len(fallos)}")
    for n, name, err in fallos[:15]:
        print(f"    fila {n} ({name[:30]}): {err[:120]}")

if __name__ == "__main__":
    main()
