from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Target
from app.schemas.targets import TargetCreate, TargetUpdate
from app.services.llm import run_provider_completion
from app.services.model_params import ModelParamValidationError, validate_model_params
from app.services.providers import get_tenant_provider_config_by_id, resolve_provider_credentials


def list_targets(db: Session, tenant_id: str) -> list[Target]:
    return db.scalars(
        select(Target)
        .where(Target.tenant_id == tenant_id)
        .order_by(Target.created_at.desc())
    ).all()


def get_target(db: Session, tenant_id: str, target_id: str) -> Target | None:
    return db.scalar(
        select(Target).where(
            Target.id == target_id,
            Target.tenant_id == tenant_id,
        )
    )


def create_target_record(db: Session, tenant_id: str, payload: TargetCreate) -> Target:
    provider_config = get_tenant_provider_config_by_id(db, tenant_id, payload.provider_config_id)
    if provider_config is None:
        raise ValueError("Selected provider connection not found for this tenant.")
    if provider_config.provider_slug != payload.provider_slug:
        raise ValueError("Provider connection does not match the selected provider.")

    try:
        validated = validate_model_params(
            provider_slug=payload.provider_slug,
            model=payload.model_identifier,
            params=payload.params_json,
        )
    except ModelParamValidationError as exc:
        raise ValueError(str(exc)) from exc

    target = Target(
        tenant_id=tenant_id,
        name=payload.name.strip(),
        provider_config_id=provider_config.id,
        provider_slug=payload.provider_slug,
        capability_profile=payload.capability_profile,
        model_identifier=payload.model_identifier.strip(),
        params_json=validated.params,
        is_active=payload.is_active,
    )
    db.add(target)
    db.commit()
    db.refresh(target)
    return target


def update_target_record(db: Session, target: Target, payload: TargetUpdate) -> Target:
    if payload.name is not None:
        target.name = payload.name.strip()
    if payload.provider_config_id is not None:
        provider_config = get_tenant_provider_config_by_id(db, target.tenant_id, payload.provider_config_id)
        if provider_config is None:
            raise ValueError("Selected provider connection not found for this tenant.")
        target.provider_config_id = provider_config.id
        if payload.provider_slug is None:
            target.provider_slug = provider_config.provider_slug

    if payload.provider_slug is not None:
        if target.provider_config_id:
            provider_config = get_tenant_provider_config_by_id(db, target.tenant_id, target.provider_config_id)
            if provider_config is not None and provider_config.provider_slug != payload.provider_slug:
                raise ValueError("Provider slug does not match selected provider connection.")
        target.provider_slug = payload.provider_slug
    if payload.capability_profile is not None:
        target.capability_profile = payload.capability_profile
    if payload.model_identifier is not None:
        target.model_identifier = payload.model_identifier.strip()
    if payload.params_json is not None:
        try:
            validated = validate_model_params(
                provider_slug=target.provider_slug,
                model=(payload.model_identifier.strip() if payload.model_identifier else target.model_identifier),
                params=payload.params_json,
            )
        except ModelParamValidationError as exc:
            raise ValueError(str(exc)) from exc
        target.params_json = validated.params
    if payload.is_active is not None:
        target.is_active = payload.is_active

    # Any configuration change invalidates the previous verification state.
    target.is_verified = False
    target.last_verification_error = None
    target.last_verified_at = None

    db.add(target)
    db.commit()
    db.refresh(target)
    return target


def delete_target_record(db: Session, target: Target) -> None:
    db.delete(target)
    db.commit()


def verify_target(db: Session, target: Target) -> tuple[bool, str]:
    if not target.is_active:
        return False, "Target is disabled. Enable it before verification."
    if target.capability_profile != "responses_chat":
        return False, f"Unsupported capability profile '{target.capability_profile}'"

    now = datetime.now(UTC)
    try:
        credentials = resolve_provider_credentials(
            db,
            tenant_id=target.tenant_id,
            provider_slug=target.provider_slug,
            provider_config_id=getattr(target, "provider_config_id", None),
        )
        params = dict(target.params_json or {})
        params.setdefault("max_output_tokens", 16)
        text, _provider_response_id, _usage = run_provider_completion(
            provider_slug=credentials.provider_slug,
            model=target.model_identifier,
            api_key=credentials.api_key,
            api_base=credentials.api_base,
            api_version=credentials.api_version,
            system_prompt="You are a connectivity probe. Respond with exactly: ok",
            input_payload="reply with ok",
            params=params,
            timeout_seconds=15,
            max_retries=0,
        )
        if not (text or "").strip():
            raise RuntimeError("Provider returned an empty response.")

        target.is_verified = True
        target.last_verified_at = now
        target.last_verification_error = None
        db.add(target)
        db.commit()
        db.refresh(target)
        return True, "Target verified successfully."
    except Exception as exc:  # noqa: BLE001
        target.is_verified = False
        target.last_verified_at = now
        target.last_verification_error = str(exc)
        db.add(target)
        db.commit()
        db.refresh(target)
        return False, f"Verification failed: {exc}"
