"""黄金评测集生成器（SRS §11.1：20 个合成 PR，覆盖 ≥10 类规则）。

设计取舍：
- 用例由**模板 + 缺陷标记**确定性生成：标记行 ``# CODEPILOT-DEFECT:<RULE>`` 决定期望命中的
  文件与行号，生成时再剔除标记行，因此期望值是精确的 (file, line, rule_id) 三元组；
- 每个用例同时给出 ZIP 输入（含测试文件，便于 Fix/Verify 链路）与 SQL 无关的安全约束；
- 生成结果落盘到 ``evals/golden-v1.json``，可被人审阅与版本化。
"""

from __future__ import annotations

import base64
import io
import json
import pathlib
import zipfile
from dataclasses import dataclass, field
from typing import Any

MARKER = "# CODEPILOT-DEFECT:"
DEFAULT_BASE_COMMIT = "synthetic-base-001"
CLEAN_TEST = '''import os

os.environ.setdefault("APP_TOKEN", "test-token")
os.environ.setdefault("DB_PASSWORD", "test-password")
os.environ.setdefault("API_KEY", "test-key")


def test_module_imports():
    import app.main  # noqa: F401
'''

SAFE_HELPER = '''def normalise(value):
    """无缺陷的辅助函数，用于保证用例中只有一个可变点。"""
    if value is None:
        return ""
    return str(value).strip()
'''


@dataclass(slots=True)
class CaseSpec:
    case_id: str
    title: str
    imports: str
    body: list[str]
    expected: list[tuple[str, str, str]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    with_helper: bool = True

    def build(self) -> tuple[dict[str, str], list[dict[str, Any]]]:
        """返回 (项目文件, 期望命中列表)；期望行号由标记行的下一行精确推导。"""
        lines: list[str] = ['"""Synthetic module for evaluation."""']
        if self.imports:
            lines.extend(self.imports.rstrip("\n").splitlines())
        lines.append("")
        if self.with_helper:
            lines.extend(SAFE_HELPER.rstrip("\n").splitlines())
            lines.append("")
        lines.append("")

        expectations: list[dict[str, Any]] = []
        pending: tuple[str, str, str] | None = None
        for entry in self.body:
            if entry.startswith(MARKER):
                _, rule_id, cwe, severity = entry.split(":")
                pending = (rule_id, cwe, severity)
                continue
            lines.append(entry)
            if pending is not None:
                expectations.append(
                    {
                        "file": "app/main.py",
                        "line": len(lines),
                        "rule_id": pending[0],
                        "cwe": pending[1],
                        "severity": pending[2],
                    }
                )
                pending = None
        files = {
            "app/__init__.py": "",
            "app/main.py": "\n".join(lines) + "\n",
            "tests/test_main.py": CLEAN_TEST,
        }
        return files, expectations


def _specs() -> list[CaseSpec]:
    """恰好 20 个合成 PR：覆盖全部 24 条规则，其中 6 个用例包含多重缺陷。

    ``imports`` 的选择规则：**补丁应用后仍然 lint 干净**。Verify 的 lint 层只检查打过补丁
    的代码树，如果用例声明了修复后不再使用的 import，ruff 会报 F401 并让门禁失败——
    那是夹具的问题，不是自动修复的问题（历史缺陷：所有用例都固定 import os/subprocess，
    导致 4 个可修复用例在 `--with-fix` 评测里全部 QUALITY_GATE_FAILED）。
    """
    imports = "import os\nimport subprocess"
    return [
        CaseSpec(
            case_id="PR-001",
            title="SQL 字符串拼接",
            # SQL 参数化修复不引入也不移除任何 import；声明 import 反而会留下 F401。
            imports="",
            body=[
                "",
                "",
                "def find_user(cursor, name):",
                f"{MARKER}R001_SQL_CONCAT:CWE-89:critical",
                '    return cursor.execute("SELECT * FROM users WHERE name=\'" + name + "\'")',
            ],
            tags=["sql", "injection"],
        ),
        CaseSpec(
            case_id="PR-002",
            title="硬编码密钥",
            # 修复后使用 os.environ，因此 import os 在补丁后仍被使用。
            imports="import os",
            body=[
                "",
                "",
                f"{MARKER}R002_HARDCODED_SECRET:CWE-798:critical",
                'API_KEY = "sk-live-abcdef123456"',
            ],
            tags=["secret"],
        ),
        CaseSpec(
            case_id="PR-003",
            title="shell=True 命令注入",
            # 修复后仍是 subprocess.run(...)，import 继续被使用。
            imports="import subprocess",
            body=[
                "",
                "",
                "def list_dir(directory):",
                f"{MARKER}R003_SHELL_TRUE:CWE-78:critical",
                '    return subprocess.run("ls " + directory, shell=True)',
            ],
            tags=["command-injection"],
        ),
        CaseSpec(
            case_id="PR-004",
            title="os.system 命令执行",
            imports=imports,
            body=[
                "",
                "",
                "def proxy(target):",
                f"{MARKER}R004_OS_SYSTEM:CWE-78:critical",
                '    os.system("curl " + target)',
            ],
            tags=["command-injection"],
        ),
        CaseSpec(
            case_id="PR-005",
            title="eval 动态执行",
            imports=imports,
            body=[
                "",
                "",
                "def parse(payload):",
                f"{MARKER}R005_EVAL_EXEC:CWE-95:critical",
                "    return eval(payload)",
            ],
            tags=["dynamic-exec"],
        ),
        CaseSpec(
            case_id="PR-006",
            title="pickle 反序列化",
            imports="import pickle",
            body=[
                "",
                "",
                "def load_state(blob):",
                f"{MARKER}R006_INSECURE_DESERIALIZE:CWE-502:critical",
                "    return pickle.loads(blob)",
            ],
            tags=["deserialization"],
        ),
        CaseSpec(
            case_id="PR-007",
            title="可变默认参数",
            imports=imports,
            body=[
                "",
                "",
                f"{MARKER}R007_MUTABLE_DEFAULT:CWE-1188:warning",
                "def collect(items=[]):",
                "    items.append(1)",
                "    return items",
            ],
            tags=["python-semantics"],
        ),
        CaseSpec(
            case_id="PR-008",
            title="裸 except",
            imports=imports,
            body=[
                "",
                "",
                "def guarded(callback):",
                "    try:",
                "        return callback()",
                f"{MARKER}R008_BARE_EXCEPT:CWE-396:warning",
                "    except:",
                "        return None",
            ],
            tags=["error-handling"],
        ),
        CaseSpec(
            case_id="PR-009",
            title="路径穿越",
            imports=imports,
            body=[
                "",
                "",
                "def read_report(name):",
                f"{MARKER}R009_PATH_TRAVERSAL:CWE-22:warning",
                '    with open("data/" + name + "/../../etc/passwd") as handle:',
                "        return handle.read()",
            ],
            tags=["traversal"],
        ),
        CaseSpec(
            case_id="PR-010",
            title="弱哈希",
            imports="import hashlib",
            body=[
                "",
                "",
                "def digest(payload):",
                f"{MARKER}R010_WEAK_HASH:CWE-327:warning",
                '    return hashlib.md5(payload.encode()).hexdigest()',
            ],
            tags=["crypto"],
        ),
        CaseSpec(
            case_id="PR-011",
            title="资源未关闭",
            imports=imports,
            body=[
                "",
                "",
                "def read_all(path):",
                f"{MARKER}R011_UNCLOSED_RESOURCE:CWE-772:warning",
                "    handle = open(path)",
                "    return handle.read()",
            ],
            tags=["resource"],
        ),
        CaseSpec(
            case_id="PR-012",
            title="使用 assert 校验",
            imports=imports,
            body=[
                "",
                "",
                "def withdraw(balance, amount):",
                f"{MARKER}R012_ASSERT_VALIDATION:CWE-617:info",
                "    assert amount <= balance",
                "    return balance - amount",
            ],
            tags=["validation"],
        ),
        CaseSpec(
            case_id="PR-013",
            title="关闭 TLS 校验",
            imports="import requests",
            body=[
                "",
                "",
                "def fetch(url):",
                f"{MARKER}R014_TLS_VERIFY_DISABLED:CWE-295:critical",
                "    return requests.get(url, verify=False)",
            ],
            tags=["tls"],
        ),
        CaseSpec(
            case_id="PR-014",
            title="安全场景使用非加密随机",
            imports="import random",
            body=[
                "",
                "",
                "def new_session_token():",
                f"{MARKER}R015_INSECURE_RANDOM:CWE-338:warning",
                "    token = str(random.random())",
                "    return token",
            ],
            tags=["random"],
        ),
        CaseSpec(
            case_id="PR-015",
            title="多缺陷：调试模式 + CORS 通配",
            imports=imports,
            body=[
                "",
                "",
                f"{MARKER}R016_DEBUG_MODE_ENABLED:CWE-489:warning",
                "DEBUG = True",
                "",
                "",
                f'{MARKER}R017_CORS_WILDCARD:CWE-942:warning',
                'allow_origins = ["*"]',
            ],
            tags=["config"],
        ),
        CaseSpec(
            case_id="PR-016",
            title="多缺陷：子进程返回码未检查 + 权限过宽",
            imports=imports,
            body=[
                "",
                "",
                "def deploy(artifact):",
                f"{MARKER}R018_SUBPROCESS_MISSING_CHECK:CWE-754:info",
                '    subprocess.run(["deploy", artifact])',
                "",
                "",
                "def publish(path):",
                f"{MARKER}R021_WORLD_WRITABLE_PERMISSION:CWE-732:warning",
                "    os.chmod(path, 0o777)",
            ],
            tags=["subprocess", "permissions"],
        ),
        CaseSpec(
            case_id="PR-017",
            title="多缺陷：不安全临时文件 + XML 外部实体",
            imports="import tempfile\nimport xml.etree.ElementTree as ET",
            body=[
                "",
                "",
                "def scratch_path():",
                f"{MARKER}R013_INSECURE_TEMP_FILE:CWE-377:warning",
                "    return tempfile.mktemp()",
                "",
                "",
                "def parse_document(raw):",
                f"{MARKER}R023_XML_UNSAFE_PARSE:CWE-611:warning",
                "    return ET.fromstring(raw)",
            ],
            tags=["tempfile", "xml"],
        ),
        CaseSpec(
            case_id="PR-018",
            title="多缺陷：不安全 YAML + 过宽异常捕获",
            imports="import yaml",
            body=[
                "",
                "",
                "def parse_config(raw):",
                f"{MARKER}R022_YAML_UNSAFE_LOAD:CWE-502:critical",
                "    return yaml.load(raw)",
                "",
                "",
                "def resilient(callback):",
                "    try:",
                "        return callback()",
                f"{MARKER}R019_BROAD_EXCEPT:CWE-396:warning",
                "    except Exception:",
                "        return None",
            ],
            tags=["deserialization", "error-handling"],
        ),
        CaseSpec(
            case_id="PR-019",
            title="多缺陷：硬编码口令 + SQL 拼接（修复链路用例）",
            # 密钥修复后使用 os.environ；SQL 修复不需要 import。
            imports="import os",
            body=[
                "",
                "",
                f"{MARKER}R002_HARDCODED_SECRET:CWE-798:critical",
                'DB_PASSWORD = "hunter2-secret-value"',
                "",
                "",
                "def lookup(cursor, name):",
                f"{MARKER}R001_SQL_CONCAT:CWE-89:critical",
                '    return cursor.execute("SELECT * FROM t WHERE n=\'" + name + "\'")',
            ],
            tags=["secret", "sql", "fixable"],
        ),
        CaseSpec(
            case_id="PR-020",
            title="多缺陷：明文 HTTP + 连接串内联凭据",
            imports=imports,
            body=[
                "",
                "",
                f"{MARKER}R020_INSECURE_URL_SCHEME:CWE-319:warning",
                'BASE_URL = "http://api.internal.example.net/v1"',
                "",
                "",
                f"{MARKER}R024_CREDENTIAL_IN_URL:CWE-798:critical",
                'DSN = "postgresql://svcuser:s3cr3tpass@db.internal:5432/app"',
            ],
            tags=["transport", "secret"],
        ),
    ]


FIXED_ZIP_TIME = (2026, 9, 13, 0, 0, 0)


def zip_payload(files: dict[str, str]) -> str:
    """确定性 ZIP：固定成员时间戳，保证黄金集生成结果可重复（NFR-005）。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for path, content in files.items():
            info = zipfile.ZipInfo(path, date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def build_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for spec in _specs():
        files, expectations = spec.build()
        cases.append(
            {
                "case_id": spec.case_id,
                "title": spec.title,
                "input": {
                    "diff": "",
                    "base_commit": DEFAULT_BASE_COMMIT,
                    "input_type": "zip",
                    "zip_base64": zip_payload(files),
                },
                "expected_findings": expectations,
                "expected_tools": ["read_file", "search_code"],
                "mode_expectations": {
                    "modes": ["single", "a2a", "offline"],
                    "expected_agents": ["review-agent", "impact-agent"],
                    "expected_artifacts": ["Finding", "ImpactReport"],
                    "max_child_tasks": 2,
                    "recovery_required": False,
                },
                "safety_constraints": {
                    "no_network": True,
                    "no_host_write": True,
                    "approval_required": True,
                },
                "tags": spec.tags,
            }
        )
    return cases


GOLDEN_PATH = pathlib.Path(__file__).resolve().parent / "golden-v1.json"


def write_dataset(path: pathlib.Path | None = None) -> pathlib.Path:
    target = path or GOLDEN_PATH
    cases = build_cases()
    target.write_text(json.dumps({"dataset": "golden-v1", "cases": cases}, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def load_cases(path: pathlib.Path | None = None) -> list[dict[str, Any]]:
    target = path or GOLDEN_PATH
    if not target.exists():
        write_dataset(target)
    payload = json.loads(target.read_text(encoding="utf-8"))
    return list(payload.get("cases", []))


if __name__ == "__main__":  # pragma: no cover
    written = write_dataset()
    print(f"已生成黄金集：{written}（{len(build_cases())} 个用例）")
