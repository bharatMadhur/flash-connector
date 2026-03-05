import asyncio

from fastapi import HTTPException
from starlette.requests import Request

from app.dependencies import csrf_protect


def _build_request(
    *,
    method: str = "POST",
    path: str = "/logout",
    headers: list[tuple[bytes, bytes]] | None = None,
    session: dict | None = None,
) -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
        "query_string": b"",
        "session": session or {},
    }

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, _receive)


def test_csrf_rejects_missing_token_for_authenticated_session() -> None:
    request = _build_request(
        session={"user_id": "u1", "csrf_token": "known-token"},
    )
    try:
        asyncio.run(csrf_protect(request))
        assert False, "Expected HTTPException for missing CSRF token"
    except HTTPException as exc:
        assert exc.status_code == 403


def test_csrf_accepts_header_token_for_authenticated_session() -> None:
    request = _build_request(
        headers=[(b"x-csrf-token", b"known-token")],
        session={"user_id": "u1", "csrf_token": "known-token"},
    )
    asyncio.run(csrf_protect(request))
