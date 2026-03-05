from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class SaveTrainingRequest(BaseModel):
    feedback: str | None = None
    edited_ideal_output: str | None = None
    tags: list[str] = Field(default_factory=list)
    save_mode: Literal["full", "redacted"] = "full"
    is_few_shot: bool = False


class TrainingEventOut(BaseModel):
    id: str
    tenant_id: str
    endpoint_id: str
    endpoint_version_id: str
    subtenant_code: str | None
    job_id: str | None
    input_json: dict[str, Any]
    output_text: str
    feedback: str | None
    edited_ideal_output: str | None
    tags: list[str] | None
    is_few_shot: bool
    created_at: datetime
    save_mode: str

    model_config = {"from_attributes": True}


class TrainingExportRequest(BaseModel):
    endpoint_id: str | None = None
    endpoint_version_id: str | None = None
    subtenant_code: str | None = None
    tags: list[str] = Field(default_factory=list)
    feedback: str | None = None
    few_shot_only: bool | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
