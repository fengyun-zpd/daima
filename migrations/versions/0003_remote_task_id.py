"""add a2a_task.remote_task_id for HTTP A2A transport

Revision ID: 0003_remote_task_id
Revises: 0002_append_only_guards
Create Date: 2026-09-13

HTTP A2A 传输下，本地子任务行与远程 Agent 的任务 ID 在受控重试后可能不同
（重试使用新的幂等键），因此需要独立记录远程任务 ID。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_remote_task_id"
down_revision: str | None = "0002_append_only_guards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("a2a_task", sa.Column("remote_task_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("a2a_task", "remote_task_id")
