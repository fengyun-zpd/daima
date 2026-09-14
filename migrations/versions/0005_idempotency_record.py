"""add idempotency_record ledger

Revision ID: 0005_idempotency_record
Revises: 0004_task_side
Create Date: 2026-09-13

命令级幂等台账：docs/03 §6 要求对
``(actor_id, command_type, aggregate_ref, idempotency_key)`` 建唯一约束，
并持久化历史响应，使"相同幂等键返回原结果"在进程重启后依然成立（宪法第七条）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0005_idempotency_record"
down_revision: str | None = "0004_task_side"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON_TYPE = sa.JSON().with_variant(JSONB, "postgresql")


def upgrade() -> None:
    op.create_table(
        "idempotency_record",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("actor_role", sa.String(length=32), nullable=False),
        sa.Column("command_type", sa.String(length=48), nullable=False),
        sa.Column("aggregate_ref", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("request_hash", sa.String(length=80), nullable=False),
        sa.Column("request_summary", JSON_TYPE, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="in_progress"),
        sa.Column("response", JSON_TYPE, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "actor_id",
            "command_type",
            "aggregate_ref",
            "idempotency_key",
            name="uq_idempotency_record_key",
        ),
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed', 'failed')",
            name="ck_idempotency_record_status",
        ),
    )
    op.create_index(
        "ix_idempotency_record_aggregate",
        "idempotency_record",
        ["command_type", "aggregate_ref"],
    )
    op.create_index("ix_idempotency_record_created_at", "idempotency_record", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_idempotency_record_created_at", table_name="idempotency_record")
    op.drop_index("ix_idempotency_record_aggregate", table_name="idempotency_record")
    op.drop_table("idempotency_record")
