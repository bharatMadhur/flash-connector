from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import pricing


class _DummyScalarResult:
    def __init__(self, items):
        self._items = items

    def all(self):
        return list(self._items)


class _DummyDB:
    def __init__(self, items):
        self._items = items

    def scalars(self, *_args, **_kwargs):
        return _DummyScalarResult(self._items)


def _rate(
    *,
    rate_id: str,
    provider_slug: str,
    pattern: str,
    input_per_1m_usd: float,
    output_per_1m_usd: float,
    cached_input_per_1m_usd: float | None = None,
    is_active: bool = True,
):
    return SimpleNamespace(
        id=rate_id,
        provider_slug=provider_slug,
        model_pattern=pattern,
        input_per_1m_usd=input_per_1m_usd,
        output_per_1m_usd=output_per_1m_usd,
        cached_input_per_1m_usd=cached_input_per_1m_usd,
        is_active=is_active,
    )


def test_extract_token_usage_prefers_input_and_cached_details() -> None:
    usage = pricing.extract_token_usage(
        {
            "input_tokens": 120,
            "output_tokens": 30,
            "input_tokens_details": {"cached_tokens": 20},
        }
    )
    assert usage.input_tokens == 120
    assert usage.output_tokens == 30
    assert usage.cached_input_tokens == 20


def test_extract_token_usage_caps_cached_tokens() -> None:
    usage = pricing.extract_token_usage(
        {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 100},
        }
    )
    assert usage.input_tokens == 10
    assert usage.output_tokens == 5
    assert usage.cached_input_tokens == 10


def test_resolve_pricing_rate_exact_match_beats_wildcard() -> None:
    db = _DummyDB(
        [
            _rate(
                rate_id="r1",
                provider_slug="openai",
                pattern="gpt-5-*",
                input_per_1m_usd=0.1,
                output_per_1m_usd=0.4,
            ),
            _rate(
                rate_id="r2",
                provider_slug="openai",
                pattern="gpt-5-nano",
                input_per_1m_usd=0.05,
                output_per_1m_usd=0.2,
            ),
        ]
    )
    match = pricing.resolve_pricing_rate(
        db,
        tenant_id="tenant_1",
        provider_slug="openai",
        model="gpt-5-nano",
    )
    assert match is not None
    assert match.id == "r2"


def test_resolve_pricing_rate_uses_more_specific_wildcard() -> None:
    db = _DummyDB(
        [
            _rate(
                rate_id="r1",
                provider_slug="openai",
                pattern="gpt-*",
                input_per_1m_usd=0.1,
                output_per_1m_usd=0.4,
            ),
            _rate(
                rate_id="r2",
                provider_slug="openai",
                pattern="gpt-5-*",
                input_per_1m_usd=0.05,
                output_per_1m_usd=0.2,
            ),
        ]
    )
    match = pricing.resolve_pricing_rate(
        db,
        tenant_id="tenant_1",
        provider_slug="openai",
        model="gpt-5-nano",
    )
    assert match is not None
    assert match.id == "r2"


def test_estimate_job_cost_usd_with_cached_tokens_uses_builtin_rates() -> None:
    cost, details = pricing.estimate_job_cost_usd(
        db=object(),
        tenant_id="tenant_1",
        provider_slug="openai",
        model="gpt-5-nano",
        usage_json={
            "input_tokens": 1000,
            "output_tokens": 500,
            "input_tokens_details": {"cached_tokens": 200},
        },
    )
    assert cost is not None
    assert details is not None
    assert details["pricing_source"] == "builtin_estimate"
    assert details["pricing_rate_id"] is None
    assert cost == pytest.approx(0.000241, rel=1e-9)


def test_estimate_job_cost_uses_builtin_when_tenant_rate_missing() -> None:
    cost, details = pricing.estimate_job_cost_usd(
        db=object(),
        tenant_id="tenant_1",
        provider_slug="openai",
        model="gpt-5-nano-2026-01-01",
        usage_json={
            "input_tokens": 1000,
            "output_tokens": 500,
            "input_tokens_details": {"cached_tokens": 200},
        },
    )
    assert cost is not None
    assert details is not None
    assert details["pricing_source"] == "builtin_estimate"
    assert details["pricing_model_pattern"] == "gpt-5-nano*"
    assert details["pricing_rate_id"] is None
    assert cost == pytest.approx(0.000241, rel=1e-9)
