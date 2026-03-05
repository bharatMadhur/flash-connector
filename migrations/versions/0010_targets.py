"""targets registry and endpoint version binding

Revision ID: 0010_targets
Revises: 0009_job_idempotency
Create Date: 2026-02-26 02:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0010_targets"
down_revision = "0009_job_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "targets",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("provider_slug", sa.String(length=64), nullable=False),
        sa.Column("capability_profile", sa.String(length=64), nullable=False, server_default="responses_chat"),
        sa.Column("model_identifier", sa.String(length=128), nullable=False),
        sa.Column("params_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verification_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "name", name="uq_targets_tenant_name"),
    )
    op.create_index("ix_targets_tenant_id", "targets", ["tenant_id"])
    op.create_index("ix_targets_provider_slug", "targets", ["provider_slug"])

    op.add_column("endpoint_versions", sa.Column("target_id", sa.String(length=36), nullable=True))
    op.create_index("ix_endpoint_versions_target_id", "endpoint_versions", ["target_id"])
    op.create_foreign_key(
        "fk_endpoint_versions_target_id",
        source_table="endpoint_versions",
        referent_table="targets",
        local_cols=["target_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_endpoint_versions_target_id", "endpoint_versions", type_="foreignkey")
    op.drop_index("ix_endpoint_versions_target_id", table_name="endpoint_versions")
    op.drop_column("endpoint_versions", "target_id")

    op.drop_index("ix_targets_provider_slug", table_name="targets")
    op.drop_index("ix_targets_tenant_id", table_name="targets")
    op.drop_table("targets")
