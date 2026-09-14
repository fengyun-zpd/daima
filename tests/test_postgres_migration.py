"""真实 PostgreSQL 迁移验证（docs/00 §3：可从空库执行迁移）。

只有在 PostgreSQL 可用时运行；否则自动跳过（不代表验收通过，CI 中应提供 PG）。
覆盖：
1. 空库执行 ``alembic upgrade head``；
2. 14 张表与追加式触发器存在；
3. 追加式护栏在数据库层真正生效（原生 SQL 也无法 UPDATE/DELETE）；
4. ``alembic downgrade base`` 可回滚。
"""

from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DatabaseError

from repositories.database import create_db_engine
from repositories.models import ALL_TABLES
from tests.conftest import POSTGRES_URL, requires_postgres

MIGRATION_DB = os.environ.get("CODEPILOT_MIGRATION_TEST_DB", "codepilot_migration_test")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _admin_url() -> str:
    """连接到默认 maintenance 库，用于创建/删除测试库。"""
    return POSTGRES_URL.rsplit("/", 1)[0] + "/postgres"


def _test_url() -> str:
    return POSTGRES_URL.rsplit("/", 1)[0] + f"/{MIGRATION_DB}"


def _alembic_config(url: str) -> Config:
    config = Config(os.path.join(REPO_ROOT, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(REPO_ROOT, "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


@pytest.fixture()
def fresh_database(monkeypatch):
    admin = create_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{MIGRATION_DB}"'))
        connection.execute(text(f'CREATE DATABASE "{MIGRATION_DB}"'))
    monkeypatch.setenv("CODEPILOT_DATABASE_URL", _test_url())
    yield _test_url()
    with admin.connect() as connection:
        connection.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :name AND pid <> pg_backend_pid()"
            ),
            {"name": MIGRATION_DB},
        )
        connection.execute(text(f'DROP DATABASE IF EXISTS "{MIGRATION_DB}"'))
    admin.dispose()


@requires_postgres
def test_migration_from_empty_database(fresh_database) -> None:
    config = _alembic_config(fresh_database)
    command.upgrade(config, "head")

    engine = create_db_engine(fresh_database)
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    missing = [table for table in ALL_TABLES if table not in tables]
    assert not missing, f"迁移后缺少表：{missing}"
    assert "alembic_version" in tables

    with engine.connect() as connection:
        triggers = [
            row[0]
            for row in connection.execute(
                text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
            )
        ]
    assert any("audit_event" in name for name in triggers)
    assert any("approval" in name for name in triggers)

    # 关键唯一约束真的建好了
    a2a_task_constraints = {item["name"] for item in inspector.get_unique_constraints("a2a_task")}
    assert "uq_a2a_task_idempotency" in a2a_task_constraints
    review_task_constraints = {
        item["name"] for item in inspector.get_unique_constraints("review_task")
    }
    assert "uq_review_task_idempotency" in review_task_constraints

    # 追加式护栏在数据库层生效
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO audit_event (id, actor_id, actor_role, action, entity_type, entity_id,"
                " event_type, before_state, after_state, created_at)"
                " VALUES ('audit-x', 'a', 'admin', 'act', 'ent', 'e', 'task_started', '{}', '{}', now())"
            )
        )
    with pytest.raises(DatabaseError) as excinfo, engine.begin() as connection:
        connection.execute(text("UPDATE audit_event SET action='tampered' WHERE id='audit-x'"))
    assert "append-only" in str(excinfo.value)

    with pytest.raises(DatabaseError), engine.begin() as connection:
        connection.execute(text("DELETE FROM audit_event WHERE id='audit-x'"))

    engine.dispose()


@requires_postgres
def test_migration_downgrade_returns_to_empty(fresh_database) -> None:
    config = _alembic_config(fresh_database)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_db_engine(fresh_database)
    tables = set(inspect(engine).get_table_names())
    remaining = [table for table in ALL_TABLES if table in tables]
    assert remaining == []
    engine.dispose()
