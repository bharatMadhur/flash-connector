"""training event few-shot selection

Revision ID: 0014_training_few_shot
Revises: 0013_wallet_billing
Create Date: 2026-02-26 05:20:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "0014_training_few_shot"
down_revision = "0013_wallet_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "training_events",
        sa.Column("is_few_shot", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_training_events_is_few_shot", "training_events", ["is_few_shot"])


def downgrade() -> None:
    op.drop_index("ix_training_events_is_few_shot", table_name="training_events")
    op.drop_column("training_events", "is_few_shot")
