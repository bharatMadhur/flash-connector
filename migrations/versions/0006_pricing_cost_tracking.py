"""pricing rates and per-job cost tracking

Revision ID: 0006_pricing_cost_tracking
Revises: 0005_nested_tenants_query_params
Create Date: 2026-02-24 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_pricing_cost_tracking"
down_revision = "0005_nested_tenants_query_params"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_pricing_rates",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider_slug", sa.String(length=64), nullable=False),
        sa.Column("model_pattern", sa.String(length=128), nullable=False),
        sa.Column("input_per_1m_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("output_per_1m_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cached_input_per_1m_usd", sa.Float(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "provider_slug", "model_pattern", name="uq_pricing_tenant_provider_model"),
    )
    op.create_index("ix_model_pricing_rates_tenant_id", "model_pricing_rates", ["tenant_id"])
    op.create_index("ix_model_pricing_rates_provider_slug", "model_pricing_rates", ["provider_slug"])
    op.create_index("ix_model_pricing_rates_model_pattern", "model_pricing_rates", ["model_pattern"])

    op.add_column("jobs", sa.Column("estimated_cost_usd", sa.Float(), nullable=True))
    op.create_index("ix_jobs_estimated_cost_usd", "jobs", ["estimated_cost_usd"])


def downgrade() -> None:
    op.drop_index("ix_jobs_estimated_cost_usd", table_name="jobs")
    op.drop_column("jobs", "estimated_cost_usd")

    op.drop_index("ix_model_pricing_rates_model_pattern", table_name="model_pricing_rates")
    op.drop_index("ix_model_pricing_rates_provider_slug", table_name="model_pricing_rates")
    op.drop_index("ix_model_pricing_rates_tenant_id", table_name="model_pricing_rates")
    op.drop_table("model_pricing_rates")
