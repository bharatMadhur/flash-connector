from types import SimpleNamespace

from app.models import JobStatus
from app.services.usage_costs import build_usage_summary


class _DummyScalarResult:
    def __init__(self, items):
        self._items = list(items)

    def all(self):
        return list(self._items)


class _DummyDB:
    def __init__(self, jobs):
        self._jobs = list(jobs)

    def scalars(self, *_args, **_kwargs):
        return _DummyScalarResult(self._jobs)


def _job(
    *,
    status: JobStatus,
    billing_mode: str,
    estimated_cost_usd: float | None,
    usage_json: dict | None,
    subtenant_code: str | None,
    provider_used: str | None,
):
    return SimpleNamespace(
        status=status,
        billing_mode=billing_mode,
        estimated_cost_usd=estimated_cost_usd,
        usage_json=usage_json,
        subtenant_code=subtenant_code,
        provider_used=provider_used,
    )


def test_build_usage_summary_empty() -> None:
    db = _DummyDB([])
    summary = build_usage_summary(db, tenant_id="tenant_1")
    assert summary.jobs_total == 0
    assert summary.jobs_completed == 0
    assert summary.estimated_cost_usd == 0.0
    assert summary.by_billing_mode == []
    assert summary.by_subtenant == []
    assert summary.by_provider == []


def test_build_usage_summary_splits_by_mode_and_subtenant() -> None:
    db = _DummyDB(
        [
            _job(
                status=JobStatus.completed,
                billing_mode="byok",
                estimated_cost_usd=1.2,
                usage_json={"input_tokens": 10, "output_tokens": 5},
                subtenant_code="ACME",
                provider_used="openai",
            ),
            _job(
                status=JobStatus.failed,
                billing_mode="flash_credits",
                estimated_cost_usd=None,
                usage_json=None,
                subtenant_code="ACME",
                provider_used="azure_openai",
            ),
            _job(
                status=JobStatus.completed,
                billing_mode="flash_credits",
                estimated_cost_usd=0.3,
                usage_json={"prompt_tokens": 4, "completion_tokens": 2},
                subtenant_code=None,
                provider_used="azure_openai",
            ),
            _job(
                status=JobStatus.canceled,
                billing_mode="byok",
                estimated_cost_usd=0.0,
                usage_json={"input_tokens": 1, "output_tokens": 1},
                subtenant_code="BETA",
                provider_used="openai",
            ),
        ]
    )

    summary = build_usage_summary(db, tenant_id="tenant_1")

    assert summary.jobs_total == 4
    assert summary.jobs_completed == 2
    assert summary.jobs_failed == 1
    assert summary.jobs_canceled == 1
    assert summary.estimated_cost_usd == 1.5
    assert summary.byok_cost_usd == 1.5
    assert summary.input_tokens == 15
    assert summary.output_tokens == 8
    assert summary.total_tokens == 23

    assert [row.key for row in summary.by_billing_mode] == ["byok"]
    assert summary.by_billing_mode[0].estimated_cost_usd == 1.5

    assert [row.label for row in summary.by_subtenant] == ["ACME", "(none)", "BETA"]
    assert summary.by_subtenant[0].jobs_total == 2
    assert summary.by_subtenant[1].estimated_cost_usd == 0.3
