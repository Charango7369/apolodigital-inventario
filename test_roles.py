import requests, getpass, sys

API = "https://apolodigital-inventario-production.up.railway.app/api/v1"
ADMIN_EMAIL = "edwinjrivero@yahoo.com"
ADMIN_PASSWORD = getpass.getpass(f"Password de {ADMIN_EMAIL}: ")

EMPLEADO_EMAIL = "empleada_test@apolo.lat"
EMPLEADO_PASSWORD = "Test1234!"

def login(email, password):
    r = requests.post(f"{API}/auth/login", data={"username": email, "password": password})
    if not r.ok:
        print(f"  X Login fallo {email}: {r.status_code} {r.text}")
        sys.exit(1)
    return r.json()["access_token"]

print("1. Login como admin...")
admin_token = login(ADMIN_EMAIL, ADMIN_PASSWORD)
print("  OK token admin")

print("2. Crear usuario empleado de prueba...")
r = requests.post(
    f"{API}/auth/register",
    headers={"Authorization": f"Bearer {admin_token}"},
    json={"email": EMPLEADO_EMAIL, "password": EMPLEADO_PASSWORD,
          "nombre": "Empleada de Prueba", "rol": "empleado"},
)
if r.status_code == 400 and "exist" in r.text.lower():
    print("  OK usuario ya existe, seguimos")
elif r.ok:
    print(f"  OK usuario creado: {EMPLEADO_EMAIL}")
else:
    print(f"  X Error: {r.status_code} {r.text}")
    sys.exit(1)

print("3. Login como empleado...")
empleado_token = login(EMPLEADO_EMAIL, EMPLEADO_PASSWORD)
print("  OK token empleado")

print("4. Empleado intenta POST /lotes...")
r = requests.post(
    f"{API}/lotes",
    headers={"Authorization": f"Bearer {empleado_token}"},
    json={"variante_id": "x", "almacen_id": "x", "cantidad": 1, "costo_unitario": 10},
)
print(f"  Status: {r.status_code}")
if r.status_code == 403:
    print("  >>> CORRECTO: empleado bloqueado por rol")
elif r.status_code == 401:
    print("  X Token invalido (problema de auth, no de rol)")
else:
    print(f"  X FALLA DE SEGURIDAD: {r.text}")
