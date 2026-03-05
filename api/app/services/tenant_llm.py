from sqlalchemy.orm import Session

from app.models import Tenant
from app.services.providers import build_provider_secret_ref, resolve_provider_credentials
from app.services.tenant_secrets import get_secret



def build_openai_secret_ref(tenant_id: str) -> str:
    return build_provider_secret_ref(tenant_id, "openai")



def tenant_has_configured_key(tenant: Tenant) -> bool:
    try:
        if tenant.openai_key_ref and get_secret(tenant.openai_key_ref):
            return True
        if get_secret(build_openai_secret_ref(tenant.id)):
            return True
        return False
    except RuntimeError:
        return False



def resolve_openai_api_key_for_tenant(db: Session, tenant_id: str) -> str:
    resolved = resolve_provider_credentials(db, tenant_id=tenant_id, provider_slug="openai")
    if not resolved.api_key:
        raise RuntimeError("No API key available for provider 'openai'")
    return resolved.api_key
