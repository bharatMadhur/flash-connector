"""nested tenants and query params inheritance

Revision ID: 0005_nested_tenants_query_params
Revises: 0004_multi_provider_runtime
Create Date: 2026-02-23 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0005_nested_tenants_query_params"
down_revision = "0004_multi_provider_runtime"
branch_labels = None
depends_on = None


tenant_query_params_mode = postgresql.ENUM(
    "inherit",
    "merge",
    "override",
    name="tenant_query_params_mode",
    create_type=False,
)


def upgrade() -> None:
    tenant_query_params_mode.create(op.get_bind(), checkfirst=True)

    op.add_column("tenants", sa.Column("parent_tenant_id", sa.String(length=36), nullable=True))
    op.add_column(
        "tenants",
        sa.Column("can_create_subtenants", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "tenants",
        sa.Column("inherit_provider_configs", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "query_params_mode",
            tenant_query_params_mode,
            nullable=False,
            server_default="override",
        ),
    )
    op.add_column(
        "tenants",
        sa.Column("query_params_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
    )

    op.create_foreign_key(
        "fk_tenants_parent_tenant",
        source_table="tenants",
        referent_table="tenants",
        local_cols=["parent_tenant_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_tenants_parent_tenant_id", "tenants", ["parent_tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_tenants_parent_tenant_id", table_name="tenants")
    op.drop_constraint("fk_tenants_parent_tenant", "tenants", type_="foreignkey")

    op.drop_column("tenants", "query_params_json")
    op.drop_column("tenants", "query_params_mode")
    op.drop_column("tenants", "inherit_provider_configs")
    op.drop_column("tenants", "can_create_subtenants")
    op.drop_column("tenants", "parent_tenant_id")

    tenant_query_params_mode.drop(op.get_bind(), checkfirst=True)
