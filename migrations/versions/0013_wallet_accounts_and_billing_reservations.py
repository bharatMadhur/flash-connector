"""wallet accounts and job reservation billing

Revision ID: 0013_wallet_billing
Revises: 0012_subtenant_portal_links
Create Date: 2026-02-26 04:05:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0013_wallet_billing"
down_revision = "0012_subtenant_portal_links"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("billing_mode", sa.String(length=32), nullable=False, server_default="byok"),
    )
    op.add_column(
        "jobs",
        sa.Column("reserved_cost_usd", sa.Float(), nullable=False, server_default="0"),
    )

    op.create_table(
        "wallet_accounts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("balance_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reserved_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", name="uq_wallet_accounts_tenant"),
    )
    op.create_index("ix_wallet_accounts_tenant_id", "wallet_accounts", ["tenant_id"])

    op.create_table(
        "wallet_ledger",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", sa.String(length=64), sa.ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("entry_type", sa.String(length=32), nullable=False),
        sa.Column("amount_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("balance_after_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reserved_after_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("subtenant_code", sa.String(length=128), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_wallet_ledger_tenant_id", "wallet_ledger", ["tenant_id"])
    op.create_index("ix_wallet_ledger_job_id", "wallet_ledger", ["job_id"])
    op.create_index("ix_wallet_ledger_entry_type", "wallet_ledger", ["entry_type"])
    op.create_index("ix_wallet_ledger_subtenant_code", "wallet_ledger", ["subtenant_code"])
    op.create_index("ix_wallet_ledger_created_at", "wallet_ledger", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_wallet_ledger_created_at", table_name="wallet_ledger")
    op.drop_index("ix_wallet_ledger_subtenant_code", table_name="wallet_ledger")
    op.drop_index("ix_wallet_ledger_entry_type", table_name="wallet_ledger")
    op.drop_index("ix_wallet_ledger_job_id", table_name="wallet_ledger")
    op.drop_index("ix_wallet_ledger_tenant_id", table_name="wallet_ledger")
    op.drop_table("wallet_ledger")

    op.drop_index("ix_wallet_accounts_tenant_id", table_name="wallet_accounts")
    op.drop_table("wallet_accounts")

    op.drop_column("jobs", "reserved_cost_usd")
    op.drop_column("jobs", "billing_mode")
