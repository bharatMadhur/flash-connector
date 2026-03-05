import hashlib
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ContextBlock, EndpointVersionContext, Persona, TenantVariable

_VAR_PATTERN = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")


def list_personas(db: Session, tenant_id: str) -> list[Persona]:
    return db.scalars(select(Persona).where(Persona.tenant_id == tenant_id).order_by(Persona.name.asc())).all()


def list_context_blocks(db: Session, tenant_id: str) -> list[ContextBlock]:
    return db.scalars(select(ContextBlock).where(ContextBlock.tenant_id == tenant_id).order_by(ContextBlock.name.asc())).all()


def list_tenant_variables(db: Session, tenant_id: str) -> list[TenantVariable]:
    return db.scalars(select(TenantVariable).where(TenantVariable.tenant_id == tenant_id).order_by(TenantVariable.key.asc())).all()


def tenant_variables_map(db: Session, tenant_id: str) -> dict[str, str]:
    variables = list_tenant_variables(db, tenant_id)
    return {item.key: item.value for item in variables}


def render_template_text(template: str, variables: dict[str, Any]) -> str:
    def replace_var(match: re.Match[str]) -> str:
        key = match.group(1)
        value = variables.get(key, "")
        if value is None:
            return ""
        if isinstance(value, (str, int, float, bool)):
            return str(value)
        return json.dumps(value, ensure_ascii=True)

    return _VAR_PATTERN.sub(replace_var, template)


def merge_variables(
    tenant_variables: dict[str, str],
    metadata: dict[str, Any] | None,
    input_text: str | None,
) -> dict[str, Any]:
    merged: dict[str, Any] = dict(tenant_variables)
    for key, value in (metadata or {}).items():
        merged[key] = value
    merged.setdefault("input", input_text or "")
    return merged


def render_job_input(
    *,
    input_template: str | None,
    input_text: str | None,
    metadata: dict[str, Any] | None,
    tenant_variables: dict[str, str],
) -> tuple[str | None, dict[str, Any]]:
    variables = merge_variables(tenant_variables=tenant_variables, metadata=metadata, input_text=input_text)
    if input_template and input_text is not None:
        return render_template_text(input_template, variables), variables
    return input_text, variables


def build_request_hash(
    *,
    endpoint_version_id: str,
    input_text: str | None,
    messages: list[dict[str, Any]] | None,
    metadata: dict[str, Any] | None,
) -> str:
    canonical_payload = {
        "endpoint_version_id": endpoint_version_id,
        "input": input_text,
        "messages": messages or [],
        "metadata": metadata or {},
    }
    as_text = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(as_text.encode("utf-8")).hexdigest()


def list_context_blocks_for_version(db: Session, tenant_id: str, endpoint_version_id: str) -> list[ContextBlock]:
    rows = db.execute(
        select(ContextBlock)
        .join(EndpointVersionContext, EndpointVersionContext.context_block_id == ContextBlock.id)
        .where(
            EndpointVersionContext.endpoint_version_id == endpoint_version_id,
            ContextBlock.tenant_id == tenant_id,
        )
        .order_by(ContextBlock.name.asc())
    )
    return [row[0] for row in rows.all()]


def compose_system_prompt(
    *,
    system_prompt: str,
    persona: Persona | None,
    context_blocks: list[ContextBlock],
) -> str:
    parts: list[str] = []
    if persona is not None:
        parts.append(f"[Persona: {persona.name}]\n{persona.instructions}")
        if persona.style_json:
            parts.append(f"[Persona Style JSON]\n{json.dumps(persona.style_json, ensure_ascii=True)}")
    if context_blocks:
        context_text = "\n\n".join(f"[Context: {item.name}]\n{item.content}" for item in context_blocks)
        parts.append(context_text)
    parts.append(system_prompt)
    return "\n\n".join(parts)


def collect_request_text(input_text: str | None, messages: list[dict[str, Any]] | None) -> str:
    if input_text:
        return input_text
    if not messages:
        return ""
    chunks: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
    return "\n".join(chunks)


def parse_list_param(params: dict[str, Any], key: str) -> list[str]:
    raw = params.get(key, [])
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in raw:
        if not isinstance(value, str):
            continue
        item = value.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def parse_int_param(params: dict[str, Any], key: str, default: int, *, min_value: int = 0, max_value: int = 86400) -> int:
    raw = params.get(key, default)
    try:
        as_int = int(raw)
    except (TypeError, ValueError):
        return default
    return min(max(as_int, min_value), max_value)


def find_blocked_phrase(text: str, blocked_phrases: list[str]) -> str | None:
    lowered = text.lower()
    for phrase in blocked_phrases:
        probe = phrase.lower()
        if probe and probe in lowered:
            return phrase
    return None
