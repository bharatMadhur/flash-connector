"""provider native batches

Revision ID: 0016_provider_native_batches
Revises: 0015_provider_connections
Create Date: 2026-02-27 02:20:00
"""

from alembic import op
import sqlalchemy as sa


revision = "0016_provider_native_batches"
down_revision = "0015_provider_connections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_batch_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("endpoint_id", sa.String(length=36), nullable=False),
        sa.Column("endpoint_version_id", sa.String(length=36), nullable=False),
        sa.Column("provider_slug", sa.String(length=64), nullable=False),
        sa.Column("provider_config_id", sa.String(length=36), nullable=True),
        sa.Column("model_used", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("completion_window", sa.String(length=32), nullable=False, server_default=sa.text("'24h'")),
        sa.Column("provider_batch_id", sa.String(length=128), nullable=True),
        sa.Column("input_file_id", sa.String(length=128), nullable=True),
        sa.Column("output_file_id", sa.String(length=128), nullable=True),
        sa.Column("error_file_id", sa.String(length=128), nullable=True),
        sa.Column("request_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("total_jobs", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("completed_jobs", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed_jobs", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("canceled_jobs", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["endpoint_id"], ["endpoints.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["endpoint_version_id"], ["endpoint_versions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["provider_config_id"], ["tenant_provider_configs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_provider_batch_runs_tenant_id", "provider_batch_runs", ["tenant_id"])
    op.create_index("ix_provider_batch_runs_endpoint_id", "provider_batch_runs", ["endpoint_id"])
    op.create_index("ix_provider_batch_runs_endpoint_version_id", "provider_batch_runs", ["endpoint_version_id"])
    op.create_index("ix_provider_batch_runs_provider_slug", "provider_batch_runs", ["provider_slug"])
    op.create_index("ix_provider_batch_runs_provider_config_id", "provider_batch_runs", ["provider_config_id"])
    op.create_index("ix_provider_batch_runs_status", "provider_batch_runs", ["status"])
    op.create_index("ix_provider_batch_runs_provider_batch_id", "provider_batch_runs", ["provider_batch_id"])
    op.create_index("ix_provider_batch_runs_created_at", "provider_batch_runs", ["created_at"])
    op.create_index("ix_provider_batch_runs_next_poll_at", "provider_batch_runs", ["next_poll_at"])

    op.add_column("jobs", sa.Column("provider_batch_run_id", sa.String(length=64), nullable=True))
    op.add_column("jobs", sa.Column("provider_batch_item_id", sa.String(length=128), nullable=True))
    op.add_column("jobs", sa.Column("provider_batch_status", sa.String(length=32), nullable=True))
    op.create_index("ix_jobs_provider_batch_run_id", "jobs", ["provider_batch_run_id"])
    op.create_index("ix_jobs_provider_batch_item_id", "jobs", ["provider_batch_item_id"])
    op.create_index("ix_jobs_provider_batch_status", "jobs", ["provider_batch_status"])
    op.create_foreign_key(
        "fk_jobs_provider_batch_run_id",
        source_table="jobs",
        referent_table="provider_batch_runs",
        local_cols=["provider_batch_run_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )

    op.alter_column("provider_batch_runs", "status", server_default=None)
    op.alter_column("provider_batch_runs", "completion_window", server_default=None)
    op.alter_column("provider_batch_runs", "request_json", server_default=None)
    op.alter_column("provider_batch_runs", "total_jobs", server_default=None)
    op.alter_column("provider_batch_runs", "completed_jobs", server_default=None)
    op.alter_column("provider_batch_runs", "failed_jobs", server_default=None)
    op.alter_column("provider_batch_runs", "canceled_jobs", server_default=None)
    op.alter_column("provider_batch_runs", "cancel_requested", server_default=None)
    op.alter_column("provider_batch_runs", "created_at", server_default=None)
    op.alter_column("provider_batch_runs", "updated_at", server_default=None)


def downgrade() -> None:
    op.drop_constraint("fk_jobs_provider_batch_run_id", "jobs", type_="foreignkey")
    op.drop_index("ix_jobs_provider_batch_status", table_name="jobs")
    op.drop_index("ix_jobs_provider_batch_item_id", table_name="jobs")
    op.drop_index("ix_jobs_provider_batch_run_id", table_name="jobs")
    op.drop_column("jobs", "provider_batch_status")
    op.drop_column("jobs", "provider_batch_item_id")
    op.drop_column("jobs", "provider_batch_run_id")

    op.drop_index("ix_provider_batch_runs_next_poll_at", table_name="provider_batch_runs")
    op.drop_index("ix_provider_batch_runs_created_at", table_name="provider_batch_runs")
    op.drop_index("ix_provider_batch_runs_provider_batch_id", table_name="provider_batch_runs")
    op.drop_index("ix_provider_batch_runs_status", table_name="provider_batch_runs")
    op.drop_index("ix_provider_batch_runs_provider_config_id", table_name="provider_batch_runs")
    op.drop_index("ix_provider_batch_runs_provider_slug", table_name="provider_batch_runs")
    op.drop_index("ix_provider_batch_runs_endpoint_version_id", table_name="provider_batch_runs")
    op.drop_index("ix_provider_batch_runs_endpoint_id", table_name="provider_batch_runs")
    op.drop_index("ix_provider_batch_runs_tenant_id", table_name="provider_batch_runs")
    op.drop_table("provider_batch_runs")
