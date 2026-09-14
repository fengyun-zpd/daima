"""add local accounts and user-facing review ids

Revision ID: 0006_accounts
Revises: 0005_idempotency_record
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_accounts"
down_revision: str | None = "0005_idempotency_record"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_task", sa.Column("custom_task_id", sa.String(length=128), nullable=True))
    op.create_index(
        "uq_review_task_actor_custom_id", "review_task", ["actor_id", "custom_task_id"], unique=True
    )
    op.create_table(
        "user_account",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("employee_id", sa.String(length=64), nullable=False, unique=True),
        sa.Column("username", sa.String(length=64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="developer"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "auth_session",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.String(length=64), sa.ForeignKey("user_account.id"), nullable=False),
        sa.Column("token_hash", sa.String(length=80), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_auth_session_user_id", "auth_session", ["user_id"])
    op.create_index("ix_auth_session_expires_at", "auth_session", ["expires_at"])


def downgrade() -> None:
    op.drop_table("auth_session")
    op.drop_table("user_account")
    op.drop_index("uq_review_task_actor_custom_id", table_name="review_task")
    op.drop_column("review_task", "custom_task_id")
