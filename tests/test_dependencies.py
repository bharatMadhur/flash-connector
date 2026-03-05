from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import dependencies


class _DummyDB:
    def __init__(self, user):
        self._user = user

    def get(self, _model, user_id):
        if self._user is None:
            return None
        return self._user if self._user.id == user_id else None


def _session_payload() -> dict[str, str]:
    return {
        "user_id": "user_1",
        "principal_tenant_id": "tenant_root",
        "active_tenant_id": "tenant_child",
        "tenant_id": "tenant_root",
        "role": "owner",
        "email": "session@example.com",
        "display_name": "Session User",
    }


def test_get_optional_session_user_clears_stale_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dependencies, "is_same_or_descendant", lambda *_args, **_kwargs: True)
    request = SimpleNamespace(session=_session_payload())
    result = dependencies.get_optional_session_user(request, _DummyDB(None))
    assert result is None
    assert request.session == {}


def test_get_optional_session_user_rejects_invalid_active_tenant(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(
        id="user_1",
        tenant_id="tenant_root",
        role=SimpleNamespace(value="admin"),
        email="db@example.com",
        display_name="DB User",
    )
    monkeypatch.setattr(dependencies, "is_same_or_descendant", lambda *_args, **_kwargs: False)
    request = SimpleNamespace(session=_session_payload())
    result = dependencies.get_optional_session_user(request, _DummyDB(user))
    assert result is None
    assert request.session == {}


def test_get_session_user_returns_validated_user(monkeypatch: pytest.MonkeyPatch) -> None:
    user = SimpleNamespace(
        id="user_1",
        tenant_id="tenant_root",
        role=SimpleNamespace(value="dev"),
        email="db@example.com",
        display_name="DB User",
    )
    monkeypatch.setattr(dependencies, "is_same_or_descendant", lambda *_args, **_kwargs: True)
    request = SimpleNamespace(session=_session_payload())
    session_user = dependencies.get_session_user(request, _DummyDB(user))
    assert session_user.user_id == "user_1"
    assert session_user.tenant_id == "tenant_child"
    assert session_user.principal_tenant_id == "tenant_root"
    assert session_user.role == "dev"
    assert session_user.email == "db@example.com"


def test_get_session_user_raises_session_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dependencies, "is_same_or_descendant", lambda *_args, **_kwargs: True)
    request = SimpleNamespace(session=_session_payload())
    with pytest.raises(HTTPException) as exc:
        dependencies.get_session_user(request, _DummyDB(None))
    assert exc.value.status_code == 401
    assert exc.value.detail == "Session is invalid"
