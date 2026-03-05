from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.core.security import generate_portal_token
from app.services import portal as portal_service


class _DummyScalarResult:
    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)


class _DummyDB:
    def __init__(self, items):
        self._items = items
        self.committed = False

    def scalars(self, *_args, **_kwargs):
        return _DummyScalarResult(self._items)

    def add(self, *_args, **_kwargs):
        return None

    def commit(self):
        self.committed = True

    def refresh(self, *_args, **_kwargs):
        return None


def test_link_permissions_always_include_view_jobs() -> None:
    link = SimpleNamespace(permissions_json={"permissions": ["export_training"]})
    permissions = portal_service.link_permissions(link)
    assert "view_jobs" in permissions
    assert "export_training" in permissions


def test_resolve_portal_token_success_updates_last_used_at() -> None:
    raw_token, token_prefix, token_hash = generate_portal_token()
    link = SimpleNamespace(
        id="link_1",
        token_prefix=token_prefix,
        token_hash=token_hash,
        is_revoked=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        last_used_at=None,
    )
    db = _DummyDB([link])
    result = portal_service.resolve_portal_token(db, raw_token)
    assert result.ok is True
    assert result.link is link
    assert link.last_used_at is not None
    assert db.committed is True


def test_resolve_portal_token_rejects_expired_link() -> None:
    raw_token, token_prefix, token_hash = generate_portal_token()
    link = SimpleNamespace(
        id="link_2",
        token_prefix=token_prefix,
        token_hash=token_hash,
        is_revoked=False,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
        last_used_at=None,
    )
    db = _DummyDB([link])
    result = portal_service.resolve_portal_token(db, raw_token)
    assert result.ok is False
    assert "expired" in result.reason.lower()
