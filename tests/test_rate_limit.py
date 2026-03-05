from app.services import rate_limit


def test_enforce_limits_fallback_when_redis_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(rate_limit, "get_redis", lambda: (_ for _ in ()).throw(RuntimeError("redis down")))
    rate_limit._fallback_rate_counts.clear()
    rate_limit._fallback_quota_counts.clear()

    allowed_1, reason_1 = rate_limit.enforce_limits("key-a", per_min_limit=2, monthly_quota=100)
    allowed_2, reason_2 = rate_limit.enforce_limits("key-a", per_min_limit=2, monthly_quota=100)
    blocked_3, reason_3 = rate_limit.enforce_limits("key-a", per_min_limit=2, monthly_quota=100)

    assert allowed_1 is True and reason_1 is None
    assert allowed_2 is True and reason_2 is None
    assert blocked_3 is False and reason_3 == "Rate limit exceeded"


def test_enforce_limits_fallback_monthly_quota(monkeypatch) -> None:
    monkeypatch.setattr(rate_limit, "get_redis", lambda: (_ for _ in ()).throw(RuntimeError("redis down")))
    rate_limit._fallback_rate_counts.clear()
    rate_limit._fallback_quota_counts.clear()

    allowed_1, reason_1 = rate_limit.enforce_limits("key-b", per_min_limit=100, monthly_quota=1)
    blocked_2, reason_2 = rate_limit.enforce_limits("key-b", per_min_limit=100, monthly_quota=1)

    assert allowed_1 is True and reason_1 is None
    assert blocked_2 is False and reason_2 == "Monthly quota exceeded"
