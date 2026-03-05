from datetime import datetime

from pydantic import BaseModel


class UsageBucketOut(BaseModel):
    key: str
    label: str
    jobs_total: int
    jobs_completed: int
    jobs_failed: int
    jobs_canceled: int
    estimated_cost_usd: float
    input_tokens: int
    output_tokens: int
    total_tokens: int


class UsageSummaryOut(BaseModel):
    window_hours: int
    from_at: datetime
    to_at: datetime
    jobs_total: int
    jobs_completed: int
    jobs_failed: int
    jobs_canceled: int
    estimated_cost_usd: float
    byok_cost_usd: float
    input_tokens: int
    output_tokens: int
    total_tokens: int
    by_billing_mode: list[UsageBucketOut]
    by_subtenant: list[UsageBucketOut]
    by_provider: list[UsageBucketOut]
