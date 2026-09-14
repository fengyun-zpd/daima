"""测试公共夹具：SQLite 数据库（含追加式护栏）与 PostgreSQL 可用性探测。

宪法第三条要求 PostgreSQL 是唯一事实来源；单元测试使用 SQLite 做快速回归，
另有 ``tests/test_postgres_migration.py`` 在 PostgreSQL 可用时验证真实迁移。
"""

from __future__ import annotations

import os
import pathlib
import sys
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from repositories.database import create_db_engine  # noqa: E402
from repositories.ddl import install_append_only_guards  # noqa: E402
from repositories.models import Base  # noqa: E402

POSTGRES_URL = os.environ.get(
    "CODEPILOT_TEST_POSTGRES_URL",
    "postgresql+psycopg://codepilot:codepilot@localhost:55432/codepilot",
)


def postgres_available() -> bool:
    try:
        engine = create_engine(POSTGRES_URL, connect_args={"connect_timeout": 3})
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:  # noqa: BLE001 - 探测失败即视为不可用
        return False


requires_postgres = pytest.mark.skipif(
    not postgres_available(), reason="PostgreSQL 不可用，跳过真实迁移测试"
)


@pytest.fixture()
def sqlite_engine(tmp_path):
    url = f"sqlite+pysqlite:///{(tmp_path / 'codepilot-test.db').as_posix()}"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    install_append_only_guards(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(sqlite_engine) -> Session:
    factory = sessionmaker(bind=sqlite_engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.close()


def _admin_engine():
    base = POSTGRES_URL.rsplit("/", 1)[0]
    return create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3})


def create_postgres_database(name: str) -> str:
    """创建一个独立测试库并返回其 URL（HTTP A2A 测试需要真正的并发写能力）。"""
    engine = _admin_engine()
    with engine.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine.dispose()
    return f"{POSTGRES_URL.rsplit('/', 1)[0]}/{name}"


def drop_postgres_database(name: str) -> None:
    engine = _admin_engine()
    with engine.connect() as connection:
        connection.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :name AND pid <> pg_backend_pid()"
            ),
            {"name": name},
        )
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    engine.dispose()


@pytest.fixture()
def concurrent_database(tmp_path):
    """并发型测试使用的数据库 URL：优先 PostgreSQL，不可用时退回 SQLite。

    HTTP A2A 涉及"客户端进程 + 服务端后台线程"同时写入，SQLite 单写者会引入
    锁等待；PostgreSQL 是宪法第三条要求的事实来源，因此优先使用。
    """
    if postgres_available():
        name = f"codepilot_it_{uuid.uuid4().hex[:12]}"
        url = create_postgres_database(name)
        try:
            yield url
        finally:
            drop_postgres_database(name)
    else:
        yield f"sqlite+pysqlite:///{(tmp_path / 'concurrent.db').as_posix()}"
