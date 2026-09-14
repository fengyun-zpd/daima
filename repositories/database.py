"""数据库引擎与会话管理（宪法第三条：PostgreSQL 是唯一事实来源）。

- 生产/默认：PostgreSQL（``postgresql+psycopg://``）；
- 测试与本地降级：SQLite 文件库（``sqlite+pysqlite://``），能力等价但需注意
  JSONB、行级锁和触发器的差异；降级仅用于测试，不改变业务结论。

所有写操作必须经由 ``session_scope`` 事务，禁止游离提交。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_POSTGRES_URL = "postgresql+psycopg://codepilot:codepilot@localhost:55432/codepilot"
DEFAULT_SQLITE_URL = "sqlite+pysqlite:///./var/codepilot.db"

_ENGINES: dict[str, Engine] = {}


def database_url() -> str:
    """读取 ``CODEPILOT_DATABASE_URL``；未设置时默认 PostgreSQL。"""
    return os.environ.get("CODEPILOT_DATABASE_URL", DEFAULT_POSTGRES_URL)


def is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def is_postgres(url: str) -> bool:
    return url.startswith("postgresql")


def _prepare_sqlite_path(url: str) -> None:
    prefix = "sqlite+pysqlite:///"
    if url.startswith(prefix):
        raw = url[len(prefix) :]
        if raw and raw != ":memory:":
            path = Path(raw)
            path.parent.mkdir(parents=True, exist_ok=True)


def create_db_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    target = url or database_url()
    if not is_sqlite(target) and not is_postgres(target):
        raise ValueError(f"不支持的数据库 URL：{target}")
    if is_sqlite(target):
        _prepare_sqlite_path(target)
    kwargs: dict[str, object] = {"echo": echo, "future": True, "pool_pre_ping": True}
    if is_sqlite(target):
        # SQLite 是单写者：给足够长的 busy timeout，避免并发写入直接失败。
        # 生产/编排使用 PostgreSQL（宪法第三条），该降级只服务本地与测试环境。
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30.0}
    engine = create_engine(target, **kwargs)

    if is_sqlite(target):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, _record) -> None:  # pragma: no cover - 驱动回调
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def get_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    """按 URL 复用引擎（同一进程内相同 URL 只创建一次）。"""
    target = url or database_url()
    if target not in _ENGINES:
        _ENGINES[target] = create_db_engine(target, echo=echo)
    return _ENGINES[target]


def dispose_engines() -> None:
    for engine in _ENGINES.values():
        engine.dispose()
    _ENGINES.clear()


def session_factory(url: str | None = None, *, echo: bool = False) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url, echo=echo), expire_on_commit=False, future=True)


@contextmanager
def session_scope(url: str | None = None, *, echo: bool = False) -> Iterator[Session]:
    """事务作用域：正常提交，异常回滚。"""
    factory = session_factory(url, echo=echo)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = [
    "DEFAULT_POSTGRES_URL",
    "DEFAULT_SQLITE_URL",
    "create_db_engine",
    "database_url",
    "dispose_engines",
    "get_engine",
    "is_postgres",
    "is_sqlite",
    "session_factory",
    "session_scope",
]
