"""append-only guards for audit_event and approval (FR-073)

Revision ID: 0002_append_only_guards
Revises: 1e1cbdae8e3b
Create Date: 2026-09-13

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from repositories.ddl import drop_append_only_guards, install_append_only_guards

revision: str = "0002_append_only_guards"
down_revision: str | None = "1e1cbdae8e3b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    install_append_only_guards(op.get_bind())


def downgrade() -> None:
    drop_append_only_guards(op.get_bind())
