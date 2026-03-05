"""tenant llm settings

Revision ID: 0002_tenant_llm_settings
Revises: 0001_initial
Create Date: 2026-02-22 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0002_tenant_llm_settings"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


llm_auth_mode = postgresql.ENUM("platform", "tenant", name="llm_auth_mode", create_type=False)



def upgrade() -> None:
    llm_auth_mode.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "tenants",
        sa.Column(
            "llm_auth_mode",
            llm_auth_mode,
            nullable=False,
            server_default="platform",
        ),
    )
    op.add_column("tenants", sa.Column("openai_key_ref", sa.String(length=255), nullable=True))



def downgrade() -> None:
    op.drop_column("tenants", "openai_key_ref")
    op.drop_column("tenants", "llm_auth_mode")
    llm_auth_mode.drop(op.get_bind(), checkfirst=True)
