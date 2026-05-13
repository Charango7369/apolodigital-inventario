import requests, getpass, sys

API = "https://apolodigital-inventario-production.up.railway.app/api/v1"
ADMIN_EMAIL = "edwinjrivero@yahoo.com"
EMPLEADO_EMAIL = "empleada_test@apolo.lat"
EMPLEADO_PASSWORD = "Test1234!"

ADMIN_PASSWORD = getpass.getpass(f"Password de {ADMIN_EMAIL}: ")

def login(email, password):
    r = requests.post(f"{API}/auth/login", data={"username": email, "password": password})
    if not r.ok:
        print(f"  X Login fallo: {r.status_code} {r.text}"); sys.exit(1)
    return r.json()["access_token"]

emp = login(EMPLEADO_EMAIL, EMPLEADO_PASSWORD)
H = {"Authorization": f"Bearer {emp}"}

print("\n" + "="*70)
print("BATERIA DE PRUEBAS - EMPLEADO contra endpoints sensibles")
print("="*70)

# (metodo, ruta, payload, debe_dar_403, descripcion)
tests = [
    # === Lo que YA protegimos esta noche ===
    ("POST", "/lotes", {"variante_id":"x","almacen_id":"x","cantidad":1,"costo_unitario":1}, True,
     "Entrada compra (POST /lotes)"),
    ("POST", "/movimientos", {"variante_id":"x","almacen_id":"x","tipo":"ENTRADA_COMPRA","cantidad":1}, True,
     "Movimiento manual (POST /movimientos)"),
    ("PUT",  "/stock/x", {"cantidad_minima":5}, True,
     "Editar stock directo (PUT /stock)"),

    # === Endpoints admin-only preexistentes ===
    ("POST",   "/almacenes", {"nombre":"x"}, True, "Crear almacen"),
    ("DELETE", "/categorias/x", None, True, "Eliminar categoria"),
    ("DELETE", "/proveedores/x", None, True, "Eliminar proveedor"),
    ("POST",   "/auth/register", {"email":"x@x.com","password":"xxxxxxxx","nombre":"x"}, True,
     "Crear usuario"),

    # === Posibles huecos (no estan protegidos en codigo, deberian estarlo) ===
    ("POST",   "/productos", {"nombre":"x","categoria_id":"x"}, True, "Crear producto"),
    ("DELETE", "/productos/x", None, True, "Eliminar producto"),
    ("POST",   "/categorias", {"nombre":"x"}, True, "Crear categoria"),
    ("POST",   "/proveedores", {"nombre":"x"}, True, "Crear proveedor"),

    # === Lo que SI puede hacer el empleado (lectura) ===
    ("GET", "/productos", None, False, "Listar productos"),
    ("GET", "/almacenes", None, False, "Listar almacenes"),
    ("GET", "/lotes/proximos-vencer", None, False, "Ver vencimientos proximos"),
    ("GET", "/stock/alertas", None, False, "Ver alertas de stock"),
]

protegidos, huecos, lecturas_ok, lecturas_fail = [], [], [], []

for method, path, payload, expect_403, desc in tests:
    if method == "GET":
        r = requests.get(f"{API}{path}", headers=H)
    elif method == "POST":
        r = requests.post(f"{API}{path}", headers=H, json=payload)
    elif method == "PUT":
        r = requests.put(f"{API}{path}", headers=H, json=payload)
    elif method == "DELETE":
        r = requests.delete(f"{API}{path}", headers=H)

    s = r.status_code
    if expect_403:
        if s == 403:
            mark = "OK"
            protegidos.append(desc)
        else:
            mark = "HUECO"
            huecos.append(f"{desc} -> {s}")
    else:
        if s in (200, 201):
            mark = "OK"
            lecturas_ok.append(desc)
        else:
            mark = "FAIL"
            lecturas_fail.append(f"{desc} -> {s}")

    print(f"  [{mark:5}] {method:6} {path:35} status={s}  {desc}")

print("\n" + "="*70)
print("RESUMEN")
print("="*70)
print(f"  Protegidos correctamente: {len(protegidos)}")
print(f"  Huecos de seguridad:      {len(huecos)}")
for h in huecos: print(f"    - {h}")
print(f"  Lecturas permitidas OK:   {len(lecturas_ok)}")
print(f"  Lecturas que fallaron:    {len(lecturas_fail)}")
for f in lecturas_fail: print(f"    - {f}")
