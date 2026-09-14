"""数据库结构与护栏自检脚本。

用法：
    python scripts/db_check.py                # 使用 CODEPILOT_DATABASE_URL
    python scripts/db_check.py --create-all   # 先建表（测试用，生产请用 alembic upgrade head）

检查项：
1. 14 张核心表是否齐全；
2. audit_event / approval 追加式护栏是否存在；
3. 关键唯一约束是否建立（幂等键、去重键）。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import inspect, text  # noqa: E402

from repositories.database import database_url, get_engine  # noqa: E402
from repositories.ddl import install_append_only_guards  # noqa: E402
from repositories.models import ALL_TABLES, APPEND_ONLY_TABLES, Base  # noqa: E402

REQUIRED_UNIQUE_HINTS = {
    "review_task": "uq_review_task_idempotency",
    "a2a_task": "uq_a2a_task_idempotency",
    "review_comment": "uq_review_comment_dedup",
    "fix_patch": "uq_fix_patch_version",
    "tool_execution": "uq_tool_execution_idempotency",
    "approval": "uq_approval_patch_version",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CodePilot 数据库自检")
    parser.add_argument("--create-all", action="store_true", help="先创建全部表（不替代 alembic 迁移）")
    parser.add_argument("--url", default=None, help="覆盖数据库 URL")
    args = parser.parse_args(argv)

    url = args.url or database_url()
    engine = get_engine(url)
    if args.create_all:
        Base.metadata.create_all(engine)
        install_append_only_guards(engine)

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    missing = [table for table in ALL_TABLES if table not in tables]

    print(f"数据库：{url}")
    print(f"表数量：{len(tables)}；缺失：{missing or '无'}")

    triggers: list[str] = []
    if engine.dialect.name == "postgresql":
        with engine.connect() as connection:
            triggers = [
                row[0]
                for row in connection.execute(
                    text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal ORDER BY tgname")
                )
            ]
    else:
        with engine.connect() as connection:
            triggers = [
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name")
                )
            ]

    guard_ok = all(any(table in name for name in triggers) for table in APPEND_ONLY_TABLES)
    print(f"追加式护栏触发器：{[t for t in triggers if any(x in t for x in APPEND_ONLY_TABLES)]}")
    print(f"护栏状态：{'OK' if guard_ok else '缺失'}")

    unique_report: dict[str, bool] = {}
    for table, name in REQUIRED_UNIQUE_HINTS.items():
        if table not in tables:
            unique_report[name] = False
            continue
        constraints = {item["name"] for item in inspector.get_unique_constraints(table)}
        constraints |= {item["name"] for item in inspector.get_indexes(table)}
        unique_report[name] = name in constraints

    print("唯一约束：")
    for name, ok in unique_report.items():
        print(f"  {'OK ' if ok else 'MISSING'} {name}")

    ok = not missing and guard_ok and all(unique_report.values())
    print("结论：", "通过" if ok else "存在问题")
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
