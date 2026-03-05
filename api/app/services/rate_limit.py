from datetime import UTC, datetime, timedelta
from threading import Lock

from app.core.config import get_settings
from app.core.redis_client import get_redis


_fallback_lock = Lock()
_fallback_rate_counts: dict[str, int] = {}
_fallback_quota_counts: dict[str, int] = {}



def _seconds_until_next_month(now: datetime) -> int:
    if now.month == 12:
        next_month = datetime(now.year + 1, 1, 1, tzinfo=UTC)
    else:
        next_month = datetime(now.year, now.month + 1, 1, tzinfo=UTC)
    return int((next_month - now).total_seconds())


def _enforce_limits_fallback(api_key_id: str, per_min_limit: int, monthly_quota: int) -> tuple[bool, str | None]:
    now = datetime.now(UTC)
    minute_bucket = now.strftime("%Y%m%d%H%M")
    month_bucket = now.strftime("%Y%m")

    rate_key = f"ratelimit:{api_key_id}:{minute_bucket}"
    quota_key = f"quota:{api_key_id}:{month_bucket}"

    with _fallback_lock:
        _fallback_rate_counts[rate_key] = _fallback_rate_counts.get(rate_key, 0) + 1
        _fallback_quota_counts[quota_key] = _fallback_quota_counts.get(quota_key, 0) + 1
        rate_count = _fallback_rate_counts[rate_key]
        quota_count = _fallback_quota_counts[quota_key]

    if rate_count > per_min_limit:
        return False, "Rate limit exceeded"
    if quota_count > monthly_quota:
        return False, "Monthly quota exceeded"
    return True, None



def enforce_limits(api_key_id: str, per_min_limit: int, monthly_quota: int) -> tuple[bool, str | None]:
    try:
        redis = get_redis()
        now = datetime.now(UTC)
        minute_bucket = now.strftime("%Y%m%d%H%M")
        month_bucket = now.strftime("%Y%m")

        rate_key = f"ratelimit:{api_key_id}:{minute_bucket}"
        quota_key = f"quota:{api_key_id}:{month_bucket}"

        pipe = redis.pipeline(transaction=True)
        pipe.incr(rate_key)
        pipe.expire(rate_key, 120)
        pipe.incr(quota_key)
        pipe.expire(quota_key, _seconds_until_next_month(now))
        rate_count, _, quota_count, _ = pipe.execute()

        if rate_count > per_min_limit:
            return False, "Rate limit exceeded"
        if quota_count > monthly_quota:
            return False, "Monthly quota exceeded"
        return True, None
    except Exception:  # noqa: BLE001
        settings = get_settings()
        if settings.environment in {"development", "test"} and settings.rate_limit_allow_in_memory_fallback_nonprod:
            return _enforce_limits_fallback(api_key_id, per_min_limit, monthly_quota)
        return False, "Rate limit service unavailable"
