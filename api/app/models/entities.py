import uuid
from datetime import datetime
from enum import Enum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum as SqlEnum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, utcnow


class UserRole(str, Enum):
    owner = "owner"
    admin = "admin"
    dev = "dev"
    viewer = "viewer"


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"
    canceled = "canceled"


class SaveMode(str, Enum):
    full = "full"
    redacted = "redacted"


class LlmAuthMode(str, Enum):
    platform = "platform"
    tenant = "tenant"


class ProviderAuthMode(str, Enum):
    platform = "platform"
    tenant = "tenant"
    none = "none"


class ProviderBillingMode(str, Enum):
    byok = "byok"


class TenantQueryParamsMode(str, Enum):
    inherit = "inherit"
    merge = "merge"
    override = "override"


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    parent_tenant_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"), nullable=True, index=True
    )
    can_create_subtenants: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    inherit_provider_configs: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    query_params_mode: Mapped[TenantQueryParamsMode] = mapped_column(
        SqlEnum(TenantQueryParamsMode, name="tenant_query_params_mode"),
        nullable=False,
        default=TenantQueryParamsMode.override,
    )
    query_params_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    llm_auth_mode: Mapped[LlmAuthMode] = mapped_column(
        SqlEnum(LlmAuthMode, name="llm_auth_mode"), nullable=False, default=LlmAuthMode.platform
    )
    openai_key_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    parent_tenant: Mapped["Tenant | None"] = relationship(
        "Tenant",
        remote_side="Tenant.id",
        foreign_keys=[parent_tenant_id],
        back_populates="child_tenants",
    )
    child_tenants: Mapped[list["Tenant"]] = relationship(
        "Tenant",
        foreign_keys=[parent_tenant_id],
        back_populates="parent_tenant",
    )

    personas: Mapped[list["Persona"]] = relationship(cascade="all, delete-orphan")
    context_blocks: Mapped[list["ContextBlock"]] = relationship(cascade="all, delete-orphan")
    variables: Mapped[list["TenantVariable"]] = relationship(cascade="all, delete-orphan")
    provider_configs: Mapped[list["TenantProviderConfig"]] = relationship(cascade="all, delete-orphan")


class TenantProviderConfig(Base):
    __tablename__ = "tenant_provider_configs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "provider_slug",
            "name",
            name="uq_tenant_provider_configs_tenant_provider_name",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    provider_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    billing_mode: Mapped[ProviderBillingMode] = mapped_column(
        SqlEnum(ProviderBillingMode, name="provider_billing_mode"),
        nullable=False,
        default=ProviderBillingMode.byok,
    )
    auth_mode: Mapped[ProviderAuthMode] = mapped_column(
        SqlEnum(ProviderAuthMode, name="provider_auth_mode"),
        nullable=False,
        default=ProviderAuthMode.platform,
    )
    key_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    api_base: Mapped[str | None] = mapped_column(String(255), nullable=True)
    api_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extra_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class Target(Base):
    __tablename__ = "targets"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_targets_tenant_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_config_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenant_provider_configs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    provider_slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    capability_profile: Mapped[str] = mapped_column(String(64), nullable=False, default="responses_chat")
    model_identifier: Mapped[str] = mapped_column(String(128), nullable=False)
    params_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_verification_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
        UniqueConstraint("tenant_id", "oidc_issuer", "oidc_subject", name="uq_users_tenant_oidc_subject"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    oidc_issuer: Mapped[str | None] = mapped_column(String(255), nullable=True)
    oidc_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[UserRole] = mapped_column(SqlEnum(UserRole, name="user_role"), nullable=False, default=UserRole.owner)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped["Tenant"] = relationship()


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    key_salt: Mapped[str] = mapped_column(String(64), nullable=False)
    scopes: Mapped[dict] = mapped_column(JSON, nullable=False, default=lambda: {"all": True})
    rate_limit_per_min: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    monthly_quota: Mapped[int] = mapped_column(Integer, nullable=False, default=10000)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Endpoint(Base):
    __tablename__ = "endpoints"
    __table_args__ = (UniqueConstraint("tenant_id", "id", name="uq_endpoints_tenant_id_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    active_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("endpoint_versions.id", ondelete="SET NULL"), nullable=True
    )

    versions: Mapped[list["EndpointVersion"]] = relationship(
        back_populates="endpoint",
        foreign_keys="EndpointVersion.endpoint_id",
        cascade="all, delete-orphan",
        order_by="EndpointVersion.version",
    )


class EndpointVersion(Base):
    __tablename__ = "endpoint_versions"
    __table_args__ = (
        UniqueConstraint("endpoint_id", "version", name="uq_endpoint_version"),
        UniqueConstraint("id", "endpoint_id", name="uq_endpoint_versions_id_endpoint_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    input_template: Mapped[str | None] = mapped_column(Text, nullable=True)
    variable_schema_json: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    target_id: Mapped[str | None] = mapped_column(ForeignKey("targets.id", ondelete="SET NULL"), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="openai")
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    params_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    persona_id: Mapped[str | None] = mapped_column(ForeignKey("personas.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    endpoint: Mapped["Endpoint"] = relationship(back_populates="versions", foreign_keys=[endpoint_id])
    target: Mapped["Target | None"] = relationship(foreign_keys=[target_id])
    persona: Mapped["Persona | None"] = relationship(foreign_keys=[persona_id])
    contexts: Mapped[list["EndpointVersionContext"]] = relationship(cascade="all, delete-orphan")


class EndpointVersionContext(Base):
    __tablename__ = "endpoint_version_contexts"
    __table_args__ = (UniqueConstraint("endpoint_version_id", "context_block_id", name="uq_endpoint_version_context"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    endpoint_version_id: Mapped[str] = mapped_column(
        ForeignKey("endpoint_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    context_block_id: Mapped[str] = mapped_column(
        ForeignKey("context_blocks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Persona(Base):
    __tablename__ = "personas"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_personas_tenant_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    style_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class ContextBlock(Base):
    __tablename__ = "context_blocks"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_context_blocks_tenant_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class TenantVariable(Base):
    __tablename__ = "tenant_variables"
    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_tenant_variables_tenant_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class ProviderBatchRun(Base):
    __tablename__ = "provider_batch_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint_version_id: Mapped[str] = mapped_column(
        ForeignKey("endpoint_versions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider_slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    provider_config_id: Mapped[str | None] = mapped_column(
        ForeignKey("tenant_provider_configs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    model_used: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    completion_window: Mapped[str] = mapped_column(String(32), nullable=False, default="24h")
    provider_batch_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    input_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    output_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_file_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    canceled_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "endpoint_id",
            "request_api_key_id",
            "idempotency_key",
            name="uq_jobs_idempotency_scope",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "endpoint_id"],
            ["endpoints.tenant_id", "endpoints.id"],
            name="fk_jobs_tenant_endpoint_consistency",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["endpoint_version_id", "endpoint_id"],
            ["endpoint_versions.id", "endpoint_versions.endpoint_id"],
            name="fk_jobs_version_endpoint_consistency",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint_version_id: Mapped[str] = mapped_column(
        ForeignKey("endpoint_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    billing_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="byok")
    reserved_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    request_api_key_id: Mapped[str | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    provider_batch_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("provider_batch_runs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    provider_batch_item_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    provider_batch_status: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    subtenant_code: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[JobStatus] = mapped_column(SqlEnum(JobStatus, name="job_status"), nullable=False, default=JobStatus.queued)
    request_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cached_from_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    result_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    provider_response_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_used: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TrainingEvent(Base):
    __tablename__ = "training_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "endpoint_id"],
            ["endpoints.tenant_id", "endpoints.id"],
            name="fk_training_events_tenant_endpoint_consistency",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["endpoint_version_id", "endpoint_id"],
            ["endpoint_versions.id", "endpoint_versions.endpoint_id"],
            name="fk_training_events_version_endpoint_consistency",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint_id: Mapped[str] = mapped_column(ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint_version_id: Mapped[str] = mapped_column(
        ForeignKey("endpoint_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subtenant_code: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True)
    input_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    output_text: Mapped[str] = mapped_column(Text, nullable=False)
    feedback: Mapped[str | None] = mapped_column(String(64), nullable=True)
    edited_ideal_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)
    is_few_shot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    save_mode: Mapped[SaveMode] = mapped_column(SqlEnum(SaveMode, name="save_mode"), nullable=False, default=SaveMode.full)
    redacted_input_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    redacted_output_text: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(128), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    diff_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)


class PortalLink(Base):
    __tablename__ = "portal_links"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    subtenant_code: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    token_prefix: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    permissions_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    is_revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

