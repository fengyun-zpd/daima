"""add a2a_task.side and scope idempotency constraint to side

Revision ID: 0004_task_side
Revises: 0003_remote_task_id
Create Date: 2026-09-13

MVP 允许 Coordinator 与 Agent 服务共享同一个 PostgreSQL（docs/01 §5）。
同一条逻辑子任务在两侧各有一条生命周期记录，因此
`(parent_task_id, agent_id, task_type, idempotency_key)` 唯一约束必须加上 `side`，
否则远端任务行会与协调方跟踪行冲突（表现为远端任务永远停留在 submitted）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_task_side"
down_revision: str | None = "0003_remote_task_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "a2a_task",
        sa.Column("side", sa.String(length=16), nullable=False, server_default="coordinator"),
    )
    with op.batch_alter_table("a2a_task", schema=None) as batch_op:
        batch_op.drop_constraint("uq_a2a_task_idempotency", type_="unique")
        batch_op.create_unique_constraint(
            "uq_a2a_task_idempotency",
            ["side", "parent_task_id", "agent_id", "task_type", "idempotency_key"],
        )
        batch_op.create_check_constraint("ck_a2a_task_side", "side IN ('coordinator', 'agent')")


def downgrade() -> None:
    with op.batch_alter_table("a2a_task", schema=None) as batch_op:
        batch_op.drop_constraint("ck_a2a_task_side", type_="check")
        batch_op.drop_constraint("uq_a2a_task_idempotency", type_="unique")
        batch_op.create_unique_constraint(
            "uq_a2a_task_idempotency",
            ["parent_task_id", "agent_id", "task_type", "idempotency_key"],
        )
    op.drop_column("a2a_task", "side")
