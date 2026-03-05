"""provider billing mode byok vs flash credits

Revision ID: 0011_provider_billing_mode
Revises: 0010_targets
Create Date: 2026-02-26 03:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0011_provider_billing_mode"
down_revision = "0010_targets"
branch_labels = None
depends_on = None


provider_billing_mode = postgresql.ENUM(
    "byok",
    "flash_credits",
    name="provider_billing_mode",
    create_type=False,
)


def upgrade() -> None:
    provider_billing_mode.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "tenant_provider_configs",
        sa.Column(
            "billing_mode",
            provider_billing_mode,
            nullable=False,
            server_default="byok",
        ),
    )
    op.execute(
        """
        UPDATE tenant_provider_configs
        SET billing_mode = CASE
            WHEN auth_mode = 'platform' THEN 'flash_credits'::provider_billing_mode
            WHEN auth_mode = 'none' THEN 'flash_credits'::provider_billing_mode
            ELSE 'byok'::provider_billing_mode
        END
        """
    )


def downgrade() -> None:
    op.drop_column("tenant_provider_configs", "billing_mode")
    provider_billing_mode.drop(op.get_bind(), checkfirst=True)
