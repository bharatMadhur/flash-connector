"""Training event persistence, redaction, export, and few-shot retrieval helpers."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
import re
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models import Job, SaveMode, TrainingEvent
from app.schemas.training import SaveTrainingRequest, TrainingExportRequest

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")


def _redact_text(value: str) -> str:
    redacted = _EMAIL_RE.sub("[REDACTED_EMAIL]", value)
    redacted = _CARD_RE.sub("[REDACTED_CARD]", redacted)
    redacted = _PHONE_RE.sub("[REDACTED_PHONE]", redacted)
    return redacted


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    return value


def redact_training_payload(input_json: dict[str, Any], output_text: str) -> tuple[dict[str, Any], str]:
    """Apply best-effort structured redaction for redacted training save mode."""
    return _redact_value(input_json), _redact_text(output_text)


def create_training_event_from_job(
    db: Session,
    *,
    tenant_id: str,
    job: Job,
    payload: SaveTrainingRequest,
) -> TrainingEvent:
    """Create one training event from a completed job and save request payload."""
    request_payload = job.request_json or {}
    input_json = {
        "input": request_payload.get("input"),
        "messages": request_payload.get("messages"),
        "metadata": request_payload.get("metadata", {}),
    }

    save_mode = SaveMode(payload.save_mode)
    if save_mode == SaveMode.redacted:
        redacted_input_json, redacted_output_text = redact_training_payload(input_json, job.result_text or "")
    else:
        redacted_input_json = None
        redacted_output_text = None

    event = TrainingEvent(
        tenant_id=tenant_id,
        endpoint_id=job.endpoint_id,
        endpoint_version_id=job.endpoint_version_id,
        subtenant_code=job.subtenant_code,
        job_id=job.id,
        input_json=input_json,
        output_text=job.result_text or "",
        feedback=payload.feedback,
        edited_ideal_output=payload.edited_ideal_output,
        tags=payload.tags,
        is_few_shot=payload.is_few_shot,
        save_mode=save_mode,
        redacted_input_json=redacted_input_json,
        redacted_output_text=redacted_output_text,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event



def auto_save_training_event(db: Session, job: Job) -> None:
    """Persist default training event when request opts in via `save_default`."""
    request_payload = job.request_json or {}
    if not request_payload.get("save_default"):
        return

    payload = SaveTrainingRequest()
    create_training_event_from_job(db, tenant_id=job.tenant_id, job=job, payload=payload)



def query_training_events(db: Session, tenant_id: str, payload: TrainingExportRequest) -> list[TrainingEvent]:
    """Return tenant-scoped training events filtered for UI/export requests."""
    filters = [TrainingEvent.tenant_id == tenant_id]

    if payload.endpoint_id:
        filters.append(TrainingEvent.endpoint_id == payload.endpoint_id)
    if payload.endpoint_version_id:
        filters.append(TrainingEvent.endpoint_version_id == payload.endpoint_version_id)
    if payload.feedback:
        filters.append(TrainingEvent.feedback == payload.feedback)
    if payload.few_shot_only is not None:
        filters.append(TrainingEvent.is_few_shot.is_(payload.few_shot_only))
    if payload.subtenant_code:
        filters.append(TrainingEvent.subtenant_code == payload.subtenant_code)
    if payload.tags:
        filters.append(TrainingEvent.tags.contains(payload.tags))
    if payload.date_from:
        filters.append(TrainingEvent.created_at >= payload.date_from)
    if payload.date_to:
        filters.append(TrainingEvent.created_at <= payload.date_to)

    return db.scalars(select(TrainingEvent).where(and_(*filters)).order_by(TrainingEvent.created_at.desc())).all()



def export_training_jsonl(events: list[TrainingEvent]) -> Iterator[bytes]:
    """Stream training events as JSONL bytes for download/export."""
    for event in events:
        if event.save_mode == SaveMode.redacted:
            input_payload = event.redacted_input_json or {"redacted": True}
            output_text = event.redacted_output_text or "[REDACTED]"
        else:
            input_payload = event.input_json
            output_text = event.output_text

        row = {
            "event_id": event.id,
            "tenant_id": event.tenant_id,
            "endpoint_id": event.endpoint_id,
            "endpoint_version_id": event.endpoint_version_id,
            "subtenant_code": event.subtenant_code,
            "job_id": event.job_id,
            "input": input_payload,
            "output": output_text,
            "feedback": event.feedback,
            "edited_ideal_output": event.edited_ideal_output,
            "tags": event.tags or [],
            "is_few_shot": event.is_few_shot,
            "created_at": event.created_at.astimezone(UTC).isoformat(),
        }
        yield (json.dumps(row, ensure_ascii=True) + "\n").encode("utf-8")


def extract_training_input_text(event: TrainingEvent) -> str:
    """Derive canonical input text from training event input payload."""
    source = event.input_json or {}
    direct_input = source.get("input")
    if isinstance(direct_input, str) and direct_input.strip():
        return direct_input.strip()

    rendered_input = source.get("rendered_input")
    if isinstance(rendered_input, str) and rendered_input.strip():
        return rendered_input.strip()

    messages = source.get("messages")
    if isinstance(messages, list):
        fragments: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "user")
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                fragments.append(f"{role}: {content.strip()}")
            elif isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        fragments.append(f"{role}: {text.strip()}")
        if fragments:
            return "\n".join(fragments)

    return ""


def list_few_shot_examples(
    db: Session,
    *,
    tenant_id: str,
    endpoint_id: str,
    limit: int = 5,
) -> list[tuple[str, str]]:
    """Return newest valid few-shot input/output pairs for endpoint context."""
    bounded_limit = min(max(int(limit), 1), 20)
    rows = db.scalars(
        select(TrainingEvent)
        .where(
            TrainingEvent.tenant_id == tenant_id,
            TrainingEvent.endpoint_id == endpoint_id,
            TrainingEvent.is_few_shot.is_(True),
            TrainingEvent.save_mode == SaveMode.full,
        )
        .order_by(TrainingEvent.created_at.desc())
        .limit(bounded_limit)
    ).all()

    examples: list[tuple[str, str]] = []
    for event in rows:
        input_text = extract_training_input_text(event)
        output_text = (event.edited_ideal_output or event.output_text or "").strip()
        if not input_text or not output_text:
            continue
        examples.append((input_text, output_text))
    examples.reverse()
    return examples
