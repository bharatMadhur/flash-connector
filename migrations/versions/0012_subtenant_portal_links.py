"""subtenant portal links

Revision ID: 0012_subtenant_portal_links
Revises: 0011_provider_billing_mode
Create Date: 2026-02-26 03:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0012_subtenant_portal_links"
down_revision = "0011_provider_billing_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portal_links",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subtenant_code", sa.String(length=128), nullable=False),
        sa.Column("token_prefix", sa.String(length=20), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("permissions_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by_user_id", sa.String(length=36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_portal_links_tenant_id", "portal_links", ["tenant_id"])
    op.create_index("ix_portal_links_subtenant_code", "portal_links", ["subtenant_code"])
    op.create_index("ix_portal_links_token_prefix", "portal_links", ["token_prefix"])
    op.create_index("ix_portal_links_expires_at", "portal_links", ["expires_at"])
    op.create_index("ix_portal_links_is_revoked", "portal_links", ["is_revoked"])
    op.create_index("ix_portal_links_created_at", "portal_links", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_portal_links_created_at", table_name="portal_links")
    op.drop_index("ix_portal_links_is_revoked", table_name="portal_links")
    op.drop_index("ix_portal_links_expires_at", table_name="portal_links")
    op.drop_index("ix_portal_links_token_prefix", table_name="portal_links")
    op.drop_index("ix_portal_links_subtenant_code", table_name="portal_links")
    op.drop_index("ix_portal_links_tenant_id", table_name="portal_links")
    op.drop_table("portal_links")
