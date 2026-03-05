"""oss schema cleanup: byok-only guards and remove unused billing tables

Revision ID: 0018_oss_schema_cleanup
Revises: 0017_data_integrity_guards
Create Date: 2026-03-05 21:15:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0018_oss_schema_cleanup"
down_revision = "0017_data_integrity_guards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE tenant_provider_configs SET billing_mode = 'byok' WHERE billing_mode <> 'byok'")
    op.execute("UPDATE jobs SET billing_mode = 'byok' WHERE billing_mode <> 'byok'")

    op.create_check_constraint(
        "ck_tenant_provider_configs_billing_mode_byok",
        "tenant_provider_configs",
        "billing_mode = 'byok'",
    )
    op.create_check_constraint(
        "ck_jobs_billing_mode_byok",
        "jobs",
        "billing_mode = 'byok'",
    )

    op.drop_table("wallet_ledger")
    op.drop_table("wallet_accounts")
    op.drop_table("model_pricing_rates")


def downgrade() -> None:
    op.create_table(
        "model_pricing_rates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("provider_slug", sa.String(length=64), nullable=False),
        sa.Column("model_pattern", sa.String(length=128), nullable=False),
        sa.Column("input_per_1m_usd", sa.Float(), nullable=False),
        sa.Column("output_per_1m_usd", sa.Float(), nullable=False),
        sa.Column("cached_input_per_1m_usd", sa.Float(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "provider_slug", "model_pattern", name="uq_pricing_tenant_provider_model"),
    )
    op.create_index("ix_model_pricing_rates_tenant_id", "model_pricing_rates", ["tenant_id"])
    op.create_index("ix_model_pricing_rates_provider_slug", "model_pricing_rates", ["provider_slug"])
    op.create_index("ix_model_pricing_rates_model_pattern", "model_pricing_rates", ["model_pattern"])

    op.create_table(
        "wallet_accounts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("balance_usd", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("reserved_usd", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_wallet_accounts_tenant"),
    )
    op.create_index("ix_wallet_accounts_tenant_id", "wallet_accounts", ["tenant_id"])

    op.create_table(
        "wallet_ledger",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=True),
        sa.Column("entry_type", sa.String(length=32), nullable=False),
        sa.Column("amount_usd", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("balance_after_usd", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("reserved_after_usd", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("subtenant_code", sa.String(length=128), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_wallet_ledger_tenant_id", "wallet_ledger", ["tenant_id"])
    op.create_index("ix_wallet_ledger_job_id", "wallet_ledger", ["job_id"])
    op.create_index("ix_wallet_ledger_entry_type", "wallet_ledger", ["entry_type"])
    op.create_index("ix_wallet_ledger_subtenant_code", "wallet_ledger", ["subtenant_code"])
    op.create_index("ix_wallet_ledger_created_at", "wallet_ledger", ["created_at"])

    op.drop_constraint("ck_jobs_billing_mode_byok", "jobs", type_="check")
    op.drop_constraint("ck_tenant_provider_configs_billing_mode_byok", "tenant_provider_configs", type_="check")
