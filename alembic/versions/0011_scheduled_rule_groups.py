from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0011_scheduled_rule_groups"
down_revision = "0010_scheduled_messages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduled_message_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("target_ref", sa.String(length=256), nullable=False),
        sa.Column("message_text", sa.Text(), nullable=False),
        sa.Column("base_minute", sa.Integer(), nullable=False),
        sa.Column("random_offset_minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("timezone", sa.String(length=64), nullable=False, server_default="Asia/Singapore"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "base_minute >= 0 AND base_minute <= 1439",
            name="ck_scheduled_message_rules_base_minute",
        ),
        sa.CheckConstraint(
            "random_offset_minutes >= 0 AND random_offset_minutes <= 720",
            name="ck_scheduled_message_rules_random_offset",
        ),
    )
    op.add_column("scheduled_messages", sa.Column("rule_id", sa.Integer(), nullable=True))
    op.execute(
        sa.text(
            """
            INSERT INTO scheduled_message_rules (
                id, target_ref, message_text, base_minute, random_offset_minutes,
                timezone, enabled, created_at, updated_at
            )
            SELECT
                id, target_ref, message_text, base_minute, random_offset_minutes,
                timezone, enabled, created_at, updated_at
            FROM scheduled_messages
            """
        )
    )
    op.execute(sa.text("UPDATE scheduled_messages SET rule_id = id"))
    op.execute(
        sa.text(
            """
            SELECT setval(
                pg_get_serial_sequence('scheduled_message_rules', 'id'),
                COALESCE((SELECT MAX(id) FROM scheduled_message_rules), 1),
                EXISTS(SELECT 1 FROM scheduled_message_rules)
            )
            """
        )
    )
    op.alter_column("scheduled_messages", "rule_id", nullable=False)
    op.create_foreign_key(
        "fk_scheduled_messages_rule_id",
        "scheduled_messages",
        "scheduled_message_rules",
        ["rule_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_scheduled_messages_rule_id",
        "scheduled_messages",
        ["rule_id"],
    )
    op.create_unique_constraint(
        "uq_scheduled_messages_rule_account",
        "scheduled_messages",
        ["rule_id", "account_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_scheduled_messages_rule_account",
        "scheduled_messages",
        type_="unique",
    )
    op.drop_index("ix_scheduled_messages_rule_id", table_name="scheduled_messages")
    op.drop_constraint(
        "fk_scheduled_messages_rule_id",
        "scheduled_messages",
        type_="foreignkey",
    )
    op.drop_column("scheduled_messages", "rule_id")
    op.drop_table("scheduled_message_rules")
