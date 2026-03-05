"""job idempotency for public submit API

Revision ID: 0009_job_idempotency
Revises: 0008_subtenant_code_tracking
Create Date: 2026-02-26 01:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_job_idempotency"
down_revision = "0008_subtenant_code_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("request_api_key_id", sa.String(length=36), nullable=True))
    op.add_column("jobs", sa.Column("idempotency_key", sa.String(length=128), nullable=True))

    op.create_index("ix_jobs_request_api_key_id", "jobs", ["request_api_key_id"])
    op.create_index("ix_jobs_idempotency_key", "jobs", ["idempotency_key"])
    op.create_foreign_key(
        "fk_jobs_request_api_key_id",
        source_table="jobs",
        referent_table="api_keys",
        local_cols=["request_api_key_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint(
        "uq_jobs_idempotency_scope",
        "jobs",
        ["tenant_id", "endpoint_id", "request_api_key_id", "idempotency_key"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_jobs_idempotency_scope", "jobs", type_="unique")
    op.drop_constraint("fk_jobs_request_api_key_id", "jobs", type_="foreignkey")
    op.drop_index("ix_jobs_idempotency_key", table_name="jobs")
    op.drop_index("ix_jobs_request_api_key_id", table_name="jobs")
    op.drop_column("jobs", "idempotency_key")
    op.drop_column("jobs", "request_api_key_id")
