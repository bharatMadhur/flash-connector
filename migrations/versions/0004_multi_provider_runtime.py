"""multi-provider runtime

Revision ID: 0004_multi_provider_runtime
Revises: 0003_promptops_studio
Create Date: 2026-02-22 16:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0004_multi_provider_runtime"
down_revision = "0003_promptops_studio"
branch_labels = None
depends_on = None


provider_auth_mode = postgresql.ENUM("platform", "tenant", "none", name="provider_auth_mode", create_type=False)


def upgrade() -> None:
    provider_auth_mode.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "tenant_provider_configs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider_slug", sa.String(length=64), nullable=False),
        sa.Column("auth_mode", provider_auth_mode, nullable=False, server_default="platform"),
        sa.Column("key_ref", sa.String(length=255), nullable=True),
        sa.Column("api_base", sa.String(length=255), nullable=True),
        sa.Column("api_version", sa.String(length=64), nullable=True),
        sa.Column("extra_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "provider_slug", name="uq_tenant_provider_configs_tenant_provider"),
    )
    op.create_index("ix_tenant_provider_configs_tenant_id", "tenant_provider_configs", ["tenant_id"])

    op.add_column(
        "endpoint_versions",
        sa.Column("provider", sa.String(length=64), nullable=False, server_default="openai"),
    )
    op.add_column("jobs", sa.Column("provider_used", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "provider_used")
    op.drop_column("endpoint_versions", "provider")

    op.drop_index("ix_tenant_provider_configs_tenant_id", table_name="tenant_provider_configs")
    op.drop_table("tenant_provider_configs")

    provider_auth_mode.drop(op.get_bind(), checkfirst=True)
