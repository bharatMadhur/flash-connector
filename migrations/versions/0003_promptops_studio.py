"""promptops studio features

Revision ID: 0003_promptops_studio
Revises: 0002_tenant_llm_settings
Create Date: 2026-02-22 00:30:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0003_promptops_studio"
down_revision = "0002_tenant_llm_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "personas",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("style_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "name", name="uq_personas_tenant_name"),
    )
    op.create_index("ix_personas_tenant_id", "personas", ["tenant_id"])

    op.create_table(
        "context_blocks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "name", name="uq_context_blocks_tenant_name"),
    )
    op.create_index("ix_context_blocks_tenant_id", "context_blocks", ["tenant_id"])

    op.create_table(
        "tenant_variables",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("is_secret", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "key", name="uq_tenant_variables_tenant_key"),
    )
    op.create_index("ix_tenant_variables_tenant_id", "tenant_variables", ["tenant_id"])

    op.add_column("endpoint_versions", sa.Column("input_template", sa.Text(), nullable=True))
    op.add_column(
        "endpoint_versions",
        sa.Column(
            "variable_schema_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )
    op.add_column("endpoint_versions", sa.Column("persona_id", sa.String(length=36), nullable=True))
    op.create_index("ix_endpoint_versions_persona_id", "endpoint_versions", ["persona_id"])
    op.create_foreign_key(
        "fk_endpoint_versions_persona_id",
        source_table="endpoint_versions",
        referent_table="personas",
        local_cols=["persona_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "endpoint_version_contexts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "endpoint_version_id",
            sa.String(length=36),
            sa.ForeignKey("endpoint_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "context_block_id",
            sa.String(length=36),
            sa.ForeignKey("context_blocks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("endpoint_version_id", "context_block_id", name="uq_endpoint_version_context"),
    )
    op.create_index("ix_endpoint_version_contexts_endpoint_version_id", "endpoint_version_contexts", ["endpoint_version_id"])
    op.create_index("ix_endpoint_version_contexts_context_block_id", "endpoint_version_contexts", ["context_block_id"])

    op.add_column("jobs", sa.Column("request_hash", sa.String(length=64), nullable=True))
    op.add_column("jobs", sa.Column("cache_hit", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("jobs", sa.Column("cached_from_job_id", sa.String(length=64), nullable=True))
    op.add_column("jobs", sa.Column("model_used", sa.String(length=128), nullable=True))
    op.create_index("ix_jobs_request_hash", "jobs", ["request_hash"])
    op.create_index("ix_jobs_cached_from_job_id", "jobs", ["cached_from_job_id"])
    op.create_foreign_key(
        "fk_jobs_cached_from_job_id",
        source_table="jobs",
        referent_table="jobs",
        local_cols=["cached_from_job_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_jobs_cached_from_job_id", "jobs", type_="foreignkey")
    op.drop_index("ix_jobs_cached_from_job_id", table_name="jobs")
    op.drop_index("ix_jobs_request_hash", table_name="jobs")
    op.drop_column("jobs", "model_used")
    op.drop_column("jobs", "cached_from_job_id")
    op.drop_column("jobs", "cache_hit")
    op.drop_column("jobs", "request_hash")

    op.drop_index("ix_endpoint_version_contexts_context_block_id", table_name="endpoint_version_contexts")
    op.drop_index("ix_endpoint_version_contexts_endpoint_version_id", table_name="endpoint_version_contexts")
    op.drop_table("endpoint_version_contexts")

    op.drop_constraint("fk_endpoint_versions_persona_id", "endpoint_versions", type_="foreignkey")
    op.drop_index("ix_endpoint_versions_persona_id", table_name="endpoint_versions")
    op.drop_column("endpoint_versions", "persona_id")
    op.drop_column("endpoint_versions", "variable_schema_json")
    op.drop_column("endpoint_versions", "input_template")

    op.drop_index("ix_tenant_variables_tenant_id", table_name="tenant_variables")
    op.drop_table("tenant_variables")

    op.drop_index("ix_context_blocks_tenant_id", table_name="context_blocks")
    op.drop_table("context_blocks")

    op.drop_index("ix_personas_tenant_id", table_name="personas")
    op.drop_table("personas")
