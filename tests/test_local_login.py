from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.core.db import get_db
from app.main import app
from app.routers import web as web_router


def _override_db():
    yield object()


def _mock_settings(*, local_auth_enabled: bool, username: str = "test", password: str = "test"):
    return SimpleNamespace(
        local_auth_enabled=local_auth_enabled,
        local_auth_username=username,
        local_auth_password=password,
        oidc_issuer_url="",
        oidc_enabled=lambda: False,
    )


def test_login_page_shows_local_test_login(monkeypatch) -> None:
    monkeypatch.setattr(web_router, "get_settings", lambda: _mock_settings(local_auth_enabled=True))
    client = TestClient(app)
    response = client.get("/login")
    assert response.status_code == 200
    assert "Local Test Login" in response.text


def test_local_login_sets_session_and_redirects(monkeypatch) -> None:
    monkeypatch.setattr(web_router, "get_settings", lambda: _mock_settings(local_auth_enabled=True))
    monkeypatch.setattr(
        web_router,
        "_ensure_local_auth_principal",
        lambda _db: (
            SimpleNamespace(id="u1", role=SimpleNamespace(value="owner"), email="test@local.dev", display_name="Test"),
            SimpleNamespace(id="t1"),
        ),
    )
    app.dependency_overrides[get_db] = _override_db
    client = TestClient(app)
    try:
        response = client.post(
            "/login/local",
            data={"username": "test", "password": "test"},
            follow_redirects=False,
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
