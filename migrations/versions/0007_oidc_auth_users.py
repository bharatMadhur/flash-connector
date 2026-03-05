"""oidc user identity columns

Revision ID: 0007_oidc_auth_users
Revises: 0006_pricing_cost_tracking
Create Date: 2026-02-26 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_oidc_auth_users"
down_revision = "0006_pricing_cost_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("display_name", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("oidc_issuer", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("oidc_subject", sa.String(length=255), nullable=True))
    op.create_unique_constraint(
        "uq_users_tenant_oidc_subject",
        "users",
        ["tenant_id", "oidc_issuer", "oidc_subject"],
    )
    op.create_index("ix_users_oidc_subject", "users", ["oidc_subject"])


def downgrade() -> None:
    op.drop_index("ix_users_oidc_subject", table_name="users")
    op.drop_constraint("uq_users_tenant_oidc_subject", "users", type_="unique")
    op.drop_column("users", "oidc_subject")
    op.drop_column("users", "oidc_issuer")
    op.drop_column("users", "display_name")
