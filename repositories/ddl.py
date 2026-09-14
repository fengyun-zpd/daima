"""数据库层 DDL 护栏：追加式表禁止 UPDATE/DELETE（FR-073）。

ORM 事件只能拦住 ORM 路径，原生 SQL 仍然可以改写追加式记录，
因此必须在数据库层再落一道触发器。PostgreSQL 用 plpgsql 触发器函数，
SQLite 用 BEFORE UPDATE/DELETE 触发器。
"""

from __future__ import annotations

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from repositories.models import APPEND_ONLY_TABLES

PG_FUNCTION_NAME = "codepilot_block_append_only_mutation"


def pg_function_sql() -> str:
    return f"""
CREATE OR REPLACE FUNCTION {PG_FUNCTION_NAME}() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'append-only table %.% rejected (FR-073)', TG_TABLE_SCHEMA, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""


def pg_trigger_sql(table: str) -> str:
    return (
        f"CREATE TRIGGER {table}_append_only "
        f"BEFORE UPDATE OR DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION {PG_FUNCTION_NAME}();"
    )


def pg_drop_sql(table: str) -> str:
    return f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};"


def sqlite_trigger_sql(table: str) -> list[str]:
    return [
        (
            f"CREATE TRIGGER IF NOT EXISTS {table}_append_only_update "
            f"BEFORE UPDATE ON {table} BEGIN "
            f"SELECT RAISE(ABORT, 'append-only table {table} rejects UPDATE'); END;"
        ),
        (
            f"CREATE TRIGGER IF NOT EXISTS {table}_append_only_delete "
            f"BEFORE DELETE ON {table} BEGIN "
            f"SELECT RAISE(ABORT, 'append-only table {table} rejects DELETE'); END;"
        ),
    ]


def sqlite_drop_sql(table: str) -> list[str]:
    return [
        f"DROP TRIGGER IF EXISTS {table}_append_only_update;",
        f"DROP TRIGGER IF EXISTS {table}_append_only_delete;",
    ]


def install_append_only_guards(bind: Engine | Connection) -> list[str]:
    """安装追加式护栏，返回执行的语句列表（便于测试断言）。"""
    dialect = bind.dialect.name
    statements: list[str] = []
    is_engine = isinstance(bind, Engine)

    def _run(sql: str) -> None:
        if is_engine:
            with bind.begin() as connection:
                connection.execute(text(sql))
        else:
            bind.execute(text(sql))
        statements.append(sql)

    if dialect == "postgresql":
        _run(pg_function_sql())
        for table in APPEND_ONLY_TABLES:
            # 幂等安装：先删除同名触发器再创建，避免重复初始化同一数据库时报错。
            _run(pg_drop_sql(table))
            _run(pg_trigger_sql(table))
    elif dialect == "sqlite":
        for table in APPEND_ONLY_TABLES:
            for sql in sqlite_trigger_sql(table):
                _run(sql)
    else:  # pragma: no cover - 其他方言不在 MVP 范围
        raise NotImplementedError(f"追加式护栏尚未支持方言：{dialect}")
    return statements


def drop_append_only_guards(bind: Engine | Connection) -> None:
    dialect = bind.dialect.name
    is_engine = isinstance(bind, Engine)
    statements: list[str] = []
    if dialect == "postgresql":
        statements = [pg_drop_sql(table) for table in APPEND_ONLY_TABLES]
        statements.append(f"DROP FUNCTION IF EXISTS {PG_FUNCTION_NAME}();")
    elif dialect == "sqlite":
        for table in APPEND_ONLY_TABLES:
            statements.extend(sqlite_drop_sql(table))
    else:  # pragma: no cover
        raise NotImplementedError(f"追加式护栏尚未支持方言：{dialect}")

    for sql in statements:
        if is_engine:
            with bind.begin() as connection:
                connection.execute(text(sql))
        else:
            bind.execute(text(sql))


__all__ = [
    "PG_FUNCTION_NAME",
    "drop_append_only_guards",
    "install_append_only_guards",
    "pg_function_sql",
    "pg_trigger_sql",
    "sqlite_trigger_sql",
]
