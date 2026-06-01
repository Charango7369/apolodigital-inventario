#!/usr/bin/env python3
"""
Carga rapida interactiva de inventario - ApoloDigital
Para sesiones presenciales: Edwin tipea, la dueña/empleada dicta del estante.

Atajos:
  - Categoria: numero, texto parcial, o Enter (usa la ultima)
  - Proveedor: Enter omite, o Enter usa el ultimo si ya elegiste uno
  - Cantidad 0 = solo crear producto, sin lote
  - Ctrl+C en cualquier momento para salir
"""
import getpass, sys
from datetime import datetime
import requests

API_URL = "https://apolodigital-inventario-production.up.railway.app/api/v1"

def login():
    print("="*70)
    print("ApoloDigital - Carga rapida interactiva")
    print("="*70)
    email = input("Email del admin: ").strip()
    pw = getpass.getpass("Password: ")
    r = requests.post(f"{API_URL}/auth/login", data={"username": email, "password": pw})
    if not r.ok:
        print(f"\n[X] Login fallido: {r.text}"); sys.exit(1)
    return r.json()["access_token"]

def api_get(path, h):
    r = requests.get(f"{API_URL}{path}", headers=h); r.raise_for_status()
    return r.json()

def api_post(path, data, h):
    r = requests.post(f"{API_URL}{path}", headers=h, json=data)
    if not r.ok: raise RuntimeError(f"{r.status_code}: {r.text[:200]}")
    return r.json()

def ask(prompt, default=None, required=False):
    if default is not None:
        prompt += f" [{default}]"
    while True:
        val = input(prompt + ": ").strip()
        if not val and default is not None: return default
        if not val and not required: return None
        if val: return val
        print("  Campo requerido.")

def ask_num(prompt, default=None, required=True):
    while True:
        s = f"{default}" if default is not None else None
        val = ask(prompt, default=s, required=required)
        if val is None: return None
        try: return float(val)
        except ValueError: print("  Numero invalido.")

def ask_fecha(prompt):
    while True:
        val = input(prompt + " (YYYY-MM-DD, Enter omite): ").strip()
        if not val: return None
        try:
            datetime.strptime(val, "%Y-%m-%d")
            return val
        except ValueError:
            print("  Formato: YYYY-MM-DD, ej. 2027-06-30")

def ask_categoria(categorias, last):
    hint = f" (Enter = '{last['nombre']}')" if last else ""
    while True:
        val = input(f"Categoria (numero o texto){hint}: ").strip()
        if not val:
            if last: return last
            print("  Requerido (no hay categoria previa)."); continue
        if val.isdigit():
            i = int(val) - 1
            if 0 <= i < len(categorias): return categorias[i]
            print(f"  Fuera de rango 1-{len(categorias)}"); continue
        matches = [c for c in categorias if val.lower() in c["nombre"].lower()]
        if not matches:
            print(f"  Sin coincidencias para '{val}'"); continue
        if len(matches) == 1:
            print(f"  -> {matches[0]['nombre']}")
            return matches[0]
        print(f"  {len(matches)} coincidencias:")
        for i, c in enumerate(matches[:8]):
            print(f"    {i+1}. {c['nombre']}")
        sub = input("  Numero: ").strip()
        if sub.isdigit():
            i = int(sub) - 1
            if 0 <= i < len(matches): return matches[i]

def ask_proveedor(proveedores, last, h):
    hint = f" (Enter = '{last['nombre']}')" if last else " (Enter omite)"
    val = input(f"Proveedor{hint}: ").strip()
    if not val:
        return last  # puede ser None
    for p in proveedores:
        if p["nombre"].lower() == val.lower(): return p
    # Crear nuevo
    print(f"  Creando proveedor nuevo '{val}'...")
    p = api_post("/proveedores", {"nombre": val}, h)
    proveedores.append(p)
    return p

def main():
    token = login()
    h = {"Authorization": f"Bearer {token}"}

    print("\nCargando contexto...")
    categorias = sorted(api_get("/categorias", h), key=lambda c: c["nombre"])
    almacenes = api_get("/almacenes", h)
    proveedores = api_get("/proveedores", h)

    if not categorias:
        print("[X] No hay categorias en el sistema. Cargalas primero."); sys.exit(1)
    if not almacenes:
        print("[X] No hay almacenes en el sistema. Cargalos primero."); sys.exit(1)

    print(f"  {len(categorias)} categorias | {len(almacenes)} almacenes | {len(proveedores)} proveedores\n")
    print("CATEGORIAS:")
    for i, c in enumerate(categorias):
        print(f"  {i+1:3}. {c['nombre']}")

    if len(almacenes) == 1:
        almacen = almacenes[0]
        print(f"\nAlmacen destino: {almacen['nombre']}")
    else:
        print("\nALMACENES:")
        for i, a in enumerate(almacenes):
            print(f"  {i+1}. {a['nombre']}")
        almacen = almacenes[int(input("Numero de almacen: ").strip()) - 1]

    print("\n" + "="*70)
    print("CARGA - Ctrl+C para salir limpiamente")
    print("="*70)

    cargados, fallos = [], []
    last_cat, last_prov = None, None
    n = 0

    try:
        while True:
            n += 1
            print(f"\n--- Producto #{n} (cargados: {len(cargados)}) ---")
            try:
                nombre = ask("Nombre del producto", required=True)
                categoria = ask_categoria(categorias, last_cat); last_cat = categoria
                costo = ask_num("Costo unitario (Bs., Enter si no recuerda)", required=False)
                precio = ask_num("Precio de venta (Bs.)", required=True)
                cantidad = ask_num("Cantidad inicial", default=0, required=False) or 0
                codigo_lote = ask("Codigo de lote (Enter omite)", required=False) if cantidad > 0 else None
                fecha_venc = ask_fecha("Fecha de vencimiento") if cantidad > 0 else None
                proveedor = ask_proveedor(proveedores, last_prov, h); last_prov = proveedor

                print(f"\n  Resumen:")
                print(f"    Producto:   {nombre}")
                print(f"    Categoria:  {categoria['nombre']}")
                costo_str = f"Bs. {costo:.2f}" if costo is not None else "no registrado"
                print(f"    Costo/Pcio: {costo_str} / Bs. {precio:.2f}")
                print(f"    Cantidad:   {cantidad}")
                if codigo_lote: print(f"    Lote:       {codigo_lote}")
                if fecha_venc:  print(f"    Vence:      {fecha_venc}")
                if proveedor:   print(f"    Proveedor:  {proveedor['nombre']}")
                if input("\n  Guardar? (Enter=si, n=no): ").strip().lower() == 'n':
                    print("  Omitido."); continue

                # Crear producto
                pd = {"nombre": nombre, "categoria_id": categoria["id"],
                      "unidad_medida": "unidad", "precio_venta": precio}
                if proveedor: pd["proveedor_id"] = proveedor["id"]
                prod = api_post("/productos", pd, h)

                # Variante default
                full = api_get(f"/productos/{prod['id']}", h)
                variantes = full.get("variantes", [])
                if not variantes:
                    raise RuntimeError("producto sin variante default")
                variante_id = variantes[0]["id"]

                # Lote si hay stock
                if cantidad > 0:
                    ld = {"variante_id": variante_id, "almacen_id": almacen["id"],
                          "cantidad": cantidad}
                    if costo is not None: ld["costo_unitario"] = costo
                    if codigo_lote: ld["codigo_lote"] = codigo_lote
                    if fecha_venc:  ld["fecha_vencimiento"] = fecha_venc
                    api_post("/lotes", ld, h)

                cargados.append(nombre)
                print(f"  [OK] #{len(cargados)}: {nombre}")
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"  [X] {e}")
                fallos.append((nombre if 'nombre' in locals() else '?', str(e)))
                if input("  Seguir? (Enter=si, n=cortar): ").strip().lower() == 'n':
                    break
    except KeyboardInterrupt:
        print("\n\nCierre manual.")

    print("\n" + "="*70)
    print(f"SESION TERMINADA")
    print(f"  Cargados: {len(cargados)}")
    if fallos:
        print(f"  Fallos:   {len(fallos)}")
        for n, e in fallos[:10]:
            print(f"    - {n}: {e[:80]}")
    print("="*70)

if __name__ == "__main__":
    main()
