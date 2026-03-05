"""initial schema

Revision ID: 0001_initial
Revises: 
Create Date: 2026-02-21 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


user_role = postgresql.ENUM("owner", "admin", "dev", "viewer", name="user_role", create_type=False)
job_status = postgresql.ENUM("queued", "running", "completed", "failed", "canceled", name="job_status", create_type=False)
save_mode = postgresql.ENUM("full", "redacted", name="save_mode", create_type=False)



def upgrade() -> None:
    user_role.create(op.get_bind(), checkfirst=True)
    job_status.create(op.get_bind(), checkfirst=True)
    save_mode.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", user_role, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("key_prefix", sa.String(length=32), nullable=False),
        sa.Column("key_hash", sa.String(length=128), nullable=False),
        sa.Column("key_salt", sa.String(length=64), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("rate_limit_per_min", sa.Integer(), nullable=False),
        sa.Column("monthly_quota", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])
    op.create_index("ix_api_keys_key_prefix", "api_keys", ["key_prefix"])

    op.create_table(
        "endpoints",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("active_version_id", sa.String(length=36), nullable=True),
    )
    op.create_index("ix_endpoints_tenant_id", "endpoints", ["tenant_id"])

    op.create_table(
        "endpoint_versions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("endpoint_id", sa.String(length=36), sa.ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("params_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.UniqueConstraint("endpoint_id", "version", name="uq_endpoint_version"),
    )
    op.create_index("ix_endpoint_versions_endpoint_id", "endpoint_versions", ["endpoint_id"])

    op.create_foreign_key(
        "fk_endpoints_active_version",
        source_table="endpoints",
        referent_table="endpoint_versions",
        local_cols=["active_version_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("endpoint_id", sa.String(length=36), sa.ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "endpoint_version_id",
            sa.String(length=36),
            sa.ForeignKey("endpoint_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", job_status, nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("result_text", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("usage_json", sa.JSON(), nullable=True),
        sa.Column("provider_response_id", sa.String(length=128), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_jobs_tenant_id", "jobs", ["tenant_id"])
    op.create_index("ix_jobs_endpoint_id", "jobs", ["endpoint_id"])
    op.create_index("ix_jobs_endpoint_version_id", "jobs", ["endpoint_version_id"])
    op.create_index("ix_jobs_created_at", "jobs", ["created_at"])

    op.create_table(
        "training_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("endpoint_id", sa.String(length=36), sa.ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "endpoint_version_id",
            sa.String(length=36),
            sa.ForeignKey("endpoint_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("job_id", sa.String(length=64), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("output_text", sa.Text(), nullable=False),
        sa.Column("feedback", sa.String(length=64), nullable=True),
        sa.Column("edited_ideal_output", sa.Text(), nullable=True),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("save_mode", save_mode, nullable=False),
        sa.Column("redacted_input_json", sa.JSON(), nullable=True),
        sa.Column("redacted_output_text", sa.Text(), nullable=True),
    )
    op.create_index("ix_training_events_tenant_id", "training_events", ["tenant_id"])
    op.create_index("ix_training_events_endpoint_id", "training_events", ["endpoint_id"])
    op.create_index("ix_training_events_endpoint_version_id", "training_events", ["endpoint_version_id"])
    op.create_index("ix_training_events_job_id", "training_events", ["job_id"])
    op.create_index("ix_training_events_created_at", "training_events", ["created_at"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("target_type", sa.String(length=128), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=True),
        sa.Column("diff_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=255), nullable=True),
    )
    op.create_index("ix_audit_logs_tenant_id", "audit_logs", ["tenant_id"])
    op.create_index("ix_audit_logs_actor_user_id", "audit_logs", ["actor_user_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])



def downgrade() -> None:
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_user_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_tenant_id", table_name="audit_logs")
    op.drop_table("audit_logs")

    op.drop_index("ix_training_events_created_at", table_name="training_events")
    op.drop_index("ix_training_events_job_id", table_name="training_events")
    op.drop_index("ix_training_events_endpoint_version_id", table_name="training_events")
    op.drop_index("ix_training_events_endpoint_id", table_name="training_events")
    op.drop_index("ix_training_events_tenant_id", table_name="training_events")
    op.drop_table("training_events")

    op.drop_index("ix_jobs_created_at", table_name="jobs")
    op.drop_index("ix_jobs_endpoint_version_id", table_name="jobs")
    op.drop_index("ix_jobs_endpoint_id", table_name="jobs")
    op.drop_index("ix_jobs_tenant_id", table_name="jobs")
    op.drop_table("jobs")

    op.drop_constraint("fk_endpoints_active_version", "endpoints", type_="foreignkey")
    op.drop_index("ix_endpoint_versions_endpoint_id", table_name="endpoint_versions")
    op.drop_table("endpoint_versions")

    op.drop_index("ix_endpoints_tenant_id", table_name="endpoints")
    op.drop_table("endpoints")

    op.drop_index("ix_api_keys_key_prefix", table_name="api_keys")
    op.drop_index("ix_api_keys_tenant_id", table_name="api_keys")
    op.drop_table("api_keys")

    op.drop_index("ix_users_tenant_id", table_name="users")
    op.drop_table("users")

    op.drop_table("tenants")

    save_mode.drop(op.get_bind(), checkfirst=True)
    job_status.drop(op.get_bind(), checkfirst=True)
    user_role.drop(op.get_bind(), checkfirst=True)
