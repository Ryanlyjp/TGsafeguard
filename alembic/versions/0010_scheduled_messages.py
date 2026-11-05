from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0010_scheduled_messages"
down_revision = "0009_temp_mail_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduled_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("tg_accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("target_ref", sa.String(length=256), nullable=False),
        sa.Column("message_text", sa.Text(), nullable=False),
        sa.Column("base_minute", sa.Integer(), nullable=False),
        sa.Column("random_offset_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="Asia/Singapore"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "base_minute >= 0 AND base_minute <= 1439",
            name="ck_scheduled_messages_base_minute",
        ),
        sa.CheckConstraint(
            "random_offset_minutes >= 0 AND random_offset_minutes <= 720",
            name="ck_scheduled_messages_random_offset",
        ),
    )
    op.create_index(
        "ix_scheduled_messages_account_id",
        "scheduled_messages",
        ["account_id"],
    )
    op.create_index(
        "ix_scheduled_messages_enabled",
        "scheduled_messages",
        ["enabled"],
    )
    op.create_index(
        "ix_scheduled_messages_next_run_at",
        "scheduled_messages",
        ["next_run_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_scheduled_messages_next_run_at", table_name="scheduled_messages")
    op.drop_index("ix_scheduled_messages_enabled", table_name="scheduled_messages")
    op.drop_index("ix_scheduled_messages_account_id", table_name="scheduled_messages")
    op.drop_table("scheduled_messages")
