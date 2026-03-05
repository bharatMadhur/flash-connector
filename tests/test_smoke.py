from fastapi.testclient import TestClient

from app.main import app



def test_healthz() -> None:
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "flash-connector"


def test_landing_page() -> None:
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    assert "flash-connector" in response.text


def test_developer_hub_page() -> None:
    client = TestClient(app)
    response = client.get("/developers")
    assert response.status_code == 200
    assert "Developer Hub" in response.text


def test_playground_requires_auth() -> None:
    client = TestClient(app)
    response = client.get("/playground", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
