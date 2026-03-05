"""Endpoint version creation with tenant/provider integrity checks."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.provider_catalog import ensure_supported_provider_slug
from app.models import ContextBlock, Endpoint, EndpointVersion, EndpointVersionContext, Persona, Target
from app.schemas.endpoints import EndpointVersionCreate
from app.services.model_params import ModelParamValidationError, validate_model_params


def create_endpoint_version_record(
    db: Session,
    *,
    endpoint: Endpoint,
    tenant_id: str,
    created_by_user_id: str | None,
    payload: EndpointVersionCreate,
) -> EndpointVersion:
    """Create immutable endpoint version and optional context associations."""
    latest_version = db.scalar(select(func.max(EndpointVersion.version)).where(EndpointVersion.endpoint_id == endpoint.id))
    next_version = (latest_version or 0) + 1

    target_id = payload.target_id
    resolved_provider = ensure_supported_provider_slug(payload.provider)
    resolved_model = (payload.model or "").strip()
    if target_id:
        target = db.scalar(select(Target).where(Target.id == target_id, Target.tenant_id == tenant_id))
        if target is None:
            raise ValueError("Selected target was not found for this tenant.")
        if not target.is_active:
            raise ValueError("Selected target is disabled.")
        resolved_provider = ensure_supported_provider_slug(target.provider_slug)
        resolved_model = target.model_identifier
    elif not resolved_model:
        raise ValueError("Model is required when target_id is not set.")

    try:
        validated_params = validate_model_params(
            provider_slug=resolved_provider,
            model=resolved_model,
            params=payload.params_json,
        )
    except ModelParamValidationError as exc:
        raise ValueError(str(exc)) from exc

    persona_id = payload.persona_id
    if persona_id:
        persona = db.scalar(select(Persona).where(Persona.id == persona_id, Persona.tenant_id == tenant_id))
        if persona is None:
            persona_id = None

    version = EndpointVersion(
        endpoint_id=endpoint.id,
        version=next_version,
        system_prompt=payload.system_prompt,
        input_template=payload.input_template,
        variable_schema_json=payload.variable_schema_json,
        target_id=target_id,
        provider=resolved_provider,
        model=resolved_model,
        params_json=validated_params.params,
        persona_id=persona_id,
        created_by_user_id=created_by_user_id,
    )
    db.add(version)
    db.commit()
    db.refresh(version)

    if payload.context_block_ids:
        valid_ids = set(
            db.scalars(
                select(ContextBlock.id).where(
                    ContextBlock.tenant_id == tenant_id,
                    ContextBlock.id.in_(payload.context_block_ids),
                )
            ).all()
        )
        for context_block_id in payload.context_block_ids:
            if context_block_id not in valid_ids:
                continue
            db.add(
                EndpointVersionContext(
                    endpoint_version_id=version.id,
                    context_block_id=context_block_id,
                )
            )
        db.commit()

    return version
