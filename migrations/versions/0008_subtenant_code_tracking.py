"""subtenant code attribution fields

Revision ID: 0008_subtenant_code_tracking
Revises: 0007_oidc_auth_users
Create Date: 2026-02-26 00:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_subtenant_code_tracking"
down_revision = "0007_oidc_auth_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("subtenant_code", sa.String(length=128), nullable=True))
    op.create_index("ix_jobs_subtenant_code", "jobs", ["subtenant_code"])

    op.add_column("training_events", sa.Column("subtenant_code", sa.String(length=128), nullable=True))
    op.create_index("ix_training_events_subtenant_code", "training_events", ["subtenant_code"])


def downgrade() -> None:
    op.drop_index("ix_training_events_subtenant_code", table_name="training_events")
    op.drop_column("training_events", "subtenant_code")

    op.drop_index("ix_jobs_subtenant_code", table_name="jobs")
    op.drop_column("jobs", "subtenant_code")
