"""add cross-table integrity guards

Revision ID: 0017_data_integrity_guards
Revises: 0016_provider_native_batches
Create Date: 2026-03-04 15:20:00
"""

from alembic import op


revision = "0017_data_integrity_guards"
down_revision = "0016_provider_native_batches"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_endpoints_tenant_id_id",
        "endpoints",
        ["tenant_id", "id"],
    )
    op.create_unique_constraint(
        "uq_endpoint_versions_id_endpoint_id",
        "endpoint_versions",
        ["id", "endpoint_id"],
    )
    op.create_foreign_key(
        "fk_jobs_tenant_endpoint_consistency",
        source_table="jobs",
        referent_table="endpoints",
        local_cols=["tenant_id", "endpoint_id"],
        remote_cols=["tenant_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_jobs_version_endpoint_consistency",
        source_table="jobs",
        referent_table="endpoint_versions",
        local_cols=["endpoint_version_id", "endpoint_id"],
        remote_cols=["id", "endpoint_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_training_events_tenant_endpoint_consistency",
        source_table="training_events",
        referent_table="endpoints",
        local_cols=["tenant_id", "endpoint_id"],
        remote_cols=["tenant_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_training_events_version_endpoint_consistency",
        source_table="training_events",
        referent_table="endpoint_versions",
        local_cols=["endpoint_version_id", "endpoint_id"],
        remote_cols=["id", "endpoint_id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_training_events_version_endpoint_consistency", "training_events", type_="foreignkey")
    op.drop_constraint("fk_training_events_tenant_endpoint_consistency", "training_events", type_="foreignkey")
    op.drop_constraint("fk_jobs_version_endpoint_consistency", "jobs", type_="foreignkey")
    op.drop_constraint("fk_jobs_tenant_endpoint_consistency", "jobs", type_="foreignkey")
    op.drop_constraint("uq_endpoint_versions_id_endpoint_id", "endpoint_versions", type_="unique")
    op.drop_constraint("uq_endpoints_tenant_id_id", "endpoints", type_="unique")
