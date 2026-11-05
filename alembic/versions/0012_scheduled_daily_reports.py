from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012_scheduled_daily_reports"
down_revision = "0011_scheduled_rule_groups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduled_message_daily_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "rule_id",
            sa.Integer(),
            sa.ForeignKey("scheduled_message_rules.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("claimed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("succeeded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "claimed_count >= 0 AND completed_count >= 0 "
            "AND succeeded_count >= 0 AND failed_count >= 0 AND skipped_count >= 0",
            name="ck_scheduled_message_daily_runs_nonnegative",
        ),
        sa.CheckConstraint(
            "completed_count = succeeded_count + failed_count + skipped_count",
            name="ck_scheduled_message_daily_runs_completed_total",
        ),
        sa.CheckConstraint(
            "completed_count <= claimed_count",
            name="ck_scheduled_message_daily_runs_claimed_total",
        ),
        sa.UniqueConstraint(
            "rule_id",
            "run_date",
            name="uq_scheduled_message_daily_run_rule_date",
        ),
    )
    op.create_index(
        "ix_scheduled_message_daily_runs_rule_id",
        "scheduled_message_daily_runs",
        ["rule_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_scheduled_message_daily_runs_rule_id",
        table_name="scheduled_message_daily_runs",
    )
    op.drop_table("scheduled_message_daily_runs")
