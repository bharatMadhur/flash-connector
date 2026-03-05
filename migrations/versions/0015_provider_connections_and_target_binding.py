"""provider connections and target binding

Revision ID: 0015_provider_connections
Revises: 0014_training_few_shot
Create Date: 2026-02-27 00:00:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0015_provider_connections"
down_revision = "0014_training_few_shot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenant_provider_configs", sa.Column("name", sa.String(length=255), nullable=True))
    op.add_column("tenant_provider_configs", sa.Column("description", sa.Text(), nullable=True))
    op.add_column(
        "tenant_provider_configs",
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    op.execute(
        """
        UPDATE tenant_provider_configs
        SET
          name = CASE
            WHEN provider_slug = 'openai' THEN 'OpenAI Default'
            WHEN provider_slug = 'azure_openai' THEN 'Azure OpenAI Default'
            ELSE initcap(replace(provider_slug, '_', ' ')) || ' Default'
          END,
          is_default = true
        WHERE name IS NULL OR name = ''
        """
    )
    op.alter_column("tenant_provider_configs", "name", nullable=False)

    op.drop_constraint("uq_tenant_provider_configs_tenant_provider", "tenant_provider_configs", type_="unique")
    op.create_unique_constraint(
        "uq_tenant_provider_configs_tenant_provider_name",
        "tenant_provider_configs",
        ["tenant_id", "provider_slug", "name"],
    )

    op.add_column("targets", sa.Column("provider_config_id", sa.String(length=36), nullable=True))
    op.create_index("ix_targets_provider_config_id", "targets", ["provider_config_id"])
    op.create_foreign_key(
        "fk_targets_provider_config_id",
        source_table="targets",
        referent_table="tenant_provider_configs",
        local_cols=["provider_config_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )

    op.execute(
        """
        UPDATE targets t
        SET provider_config_id = c.id
        FROM tenant_provider_configs c
        WHERE c.tenant_id = t.tenant_id
          AND c.provider_slug = t.provider_slug
        """
    )


def downgrade() -> None:
    op.drop_constraint("fk_targets_provider_config_id", "targets", type_="foreignkey")
    op.drop_index("ix_targets_provider_config_id", table_name="targets")
    op.drop_column("targets", "provider_config_id")

    op.drop_constraint("uq_tenant_provider_configs_tenant_provider_name", "tenant_provider_configs", type_="unique")

    # Restore legacy uniqueness by keeping one config per tenant/provider.
    op.execute(
        """
        DELETE FROM tenant_provider_configs a
        USING tenant_provider_configs b
        WHERE a.tenant_id = b.tenant_id
          AND a.provider_slug = b.provider_slug
          AND a.id <> b.id
          AND a.created_at < b.created_at
        """
    )

    op.create_unique_constraint(
        "uq_tenant_provider_configs_tenant_provider",
        "tenant_provider_configs",
        ["tenant_id", "provider_slug"],
    )

    op.drop_column("tenant_provider_configs", "is_default")
    op.drop_column("tenant_provider_configs", "description")
    op.drop_column("tenant_provider_configs", "name")
