def test_root(client):
    r = client.get("/")
    assert r.status_code == 200
    data = r.json()
    assert data["app"] == "ApoloDigital Inventarios"
    assert data["status"] == "ok"


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    # En tests la DB es SQLite en memoria, puede estar degraded
    assert data["status"] in ("ok", "degraded")
    assert "db" in data
    assert "environment" in data
