"""三类自动修复recipe（SRS §6.4：MVP 只承诺 3 类）。

确定性约束：
- 只处理能在**单行**内安全改写的形态；无法安全改写时返回 ``None``，绝不猜测；
- 只生成候选补丁文本，不写文件、不写分支（宪法第六条）；
- 数据库占位符由配置声明（默认 ``%s``，DB-API 标准），不支持未声明的 DB API。
"""

from __future__ import annotations

import ast
import re
import shlex
from dataclasses import dataclass, field

from a2a.protocol import FindingItem
from domain.enums import FixCategory
from domain.safepath import normalize_path
from domain.workspace import Workspace

ASSIGNMENT = re.compile(
    r"^(?P<indent>\s*)(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?:\s*:\s*[A-Za-z_][A-Za-z0-9_\[\]., ]*)?"
    r"\s*=\s*(?P<quote>['\"])(?P<value>(?:\\.|(?!\3).)*)(?P=quote)\s*$"
)
SUBPROCESS_CALL = re.compile(
    r"(?P<prefix>\b(?:subprocess\.(?:run|call|check_call|check_output|Popen)|os\.system|os\.popen)\s*\()"
    r"(?P<args>.*)\)\s*$"
)
EXECUTE_CALL = re.compile(
    r"^(?P<indent>\s*)(?P<prefix>(?:return\s+|await\s+|yield\s+)?)"
    r"(?P<head>[\w\.\[\]]*\.execute)\s*\((?P<args>.*)\)\s*$"
)
SQL_KEYWORDS = ("select", "insert", "update", "delete", "replace", "with ")
ENV_NAME = re.compile(r"[^A-Za-z0-9]+")


@dataclass(slots=True)
class PatchPlan:
    """一次候选补丁的计划（尚未写入任何文件）。"""

    category: FixCategory
    path: str
    original: str
    patched: str
    finding_refs: list[str] = field(default_factory=list)
    changed_functions: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.original != self.patched


def plan_fix(
    finding: FindingItem,
    workspace: Workspace,
    *,
    placeholder: str = "%s",
) -> PatchPlan | None:
    """为单条 Finding 生成补丁计划；不支持或无法安全改写时返回 ``None``。"""
    if not finding.auto_fixable or finding.fix_category is None:
        return None
    path = normalize_path(finding.file)
    if not workspace.has_file(path):
        return None
    content = workspace.files[path]
    lines = content.splitlines()

    category = FixCategory(finding.fix_category)
    if category is FixCategory.HARDCODED_SECRET:
        return _plan_hardcoded_secret(finding, path, content, lines)
    if category is FixCategory.SHELL_TRUE:
        return _plan_shell_true(finding, path, content, lines)
    if category is FixCategory.SQL_PARAMETERIZATION:
        return _plan_sql(finding, path, content, lines, placeholder=placeholder)
    return None


def finding_ref(finding: FindingItem) -> str:
    return f"{finding.rule_id}@{finding.file}:{finding.line}"


# ---------------------------------------------------------------------------
# 1. 硬编码密钥 → 环境变量
# ---------------------------------------------------------------------------


def _plan_hardcoded_secret(
    finding: FindingItem, path: str, content: str, lines: list[str]
) -> PatchPlan | None:
    index = finding.line - 1
    if not (0 <= index < len(lines)):
        return None
    match = ASSIGNMENT.match(lines[index])
    if not match:
        return None

    name = match.group("name")
    env_name = ENV_NAME.sub("_", name).upper().strip("_")
    indent = match.group("indent")
    replacement = f'{indent}{name} = os.environ["{env_name}"]'
    patched_lines = list(lines)
    patched_lines[index] = replacement

    notes = [f"用环境变量 {env_name} 替换硬编码密钥"]
    if not _has_os_import(patched_lines):
        insert_at = _import_insert_index(patched_lines)
        patched_lines.insert(insert_at, "import os")
        notes.append("补充 import os")

    patched = "\n".join(patched_lines) + ("\n" if content.endswith("\n") else "")
    return PatchPlan(
        category=FixCategory.HARDCODED_SECRET,
        path=path,
        original=content,
        patched=patched,
        finding_refs=[finding_ref(finding)],
        changed_functions=[finding.symbol] if finding.symbol else [],
        notes=notes,
    )


def _has_os_import(lines: list[str]) -> bool:
    return any(re.match(r"^\s*(import os|from os import)\b", line) for line in lines)


def _import_insert_index(lines: list[str]) -> int:
    index = 0
    for position, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")) or stripped.startswith(("'''", '"""', "#")):
            index = position + 1
        elif stripped and not stripped.startswith("#"):
            break
    return index


# ---------------------------------------------------------------------------
# 2. shell=True → 参数列表
# ---------------------------------------------------------------------------


def _plan_shell_true(
    finding: FindingItem, path: str, content: str, lines: list[str]
) -> PatchPlan | None:
    index = finding.line - 1
    if not (0 <= index < len(lines)):
        return None
    line = lines[index]
    if "shell=True" not in line:
        return None
    match = SUBPROCESS_CALL.search(line)
    if not match:
        return None

    arguments = _split_arguments(match.group("args"))
    if arguments is None:
        return None
    command_expr, keywords = arguments
    tokens = _literal_command_tokens(command_expr)
    if tokens is None:
        return None

    remaining = [(key, value) for key, value in keywords if key != "shell"]
    if not any(key == "check" for key, _ in remaining):
        remaining.append(("check", "True"))
    new_kwargs = "".join(f", {key}={value}" for key, value in remaining)
    new_line = f"{match.group('prefix')}{tokens}{new_kwargs})"
    patched_lines = list(lines)
    patched_lines[index] = line[: match.start()] + new_line + line[match.end() :]

    return PatchPlan(
        category=FixCategory.SHELL_TRUE,
        path=path,
        original=content,
        patched="\n".join(patched_lines) + ("\n" if content.endswith("\n") else ""),
        finding_refs=[finding_ref(finding)],
        changed_functions=[finding.symbol] if finding.symbol else [],
        notes=["移除 shell=True，改为参数列表调用并开启 check=True"],
    )


def _split_arguments(text: str) -> tuple[str, list[tuple[str, str]]] | None:
    """把调用参数拆成"第一个位置参数"与关键字参数（仅支持单层、无嵌套括号歧义的情形）。"""
    depth = 0
    quote: str | None = None
    first_end = None
    for position, char in enumerate(text):
        if quote:
            if char == quote and text[position - 1] != "\\":
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            first_end = position
            break
    if first_end is None:
        return None
    command = text[:first_end].strip()
    rest = text[first_end + 1 :]
    keywords: list[tuple[str, str]] = []
    for item in _split_top_level(rest):
        if not item:
            continue
        if "=" not in item:
            return None  # 存在额外位置参数：无法安全改写
        key, _, value = item.partition("=")
        keywords.append((key.strip(), value.strip()))
    return command, keywords


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    for position, char in enumerate(text):
        if quote:
            current.append(char)
            if char == quote and text[position - 1] != "\\":
                quote = None
            continue
        if char in "\"'":
            quote = char
            current.append(char)
        elif char in "([{":
            depth += 1
            current.append(char)
        elif char in ")]}":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current).strip())
    return parts


def _literal_command_tokens(expression: str) -> str | None:
    """把命令表达式转成 Python 参数列表字面量；无法确定边界时返回 ``None``。"""
    expression = expression.strip()
    try:
        node = ast.parse(expression, mode="eval").body
    except SyntaxError:
        return None

    tokens: list[str] = []
    parts: list[str] = []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            tokens = shlex.split(node.value)
        except ValueError:
            return None
        parts = [f'"{token}"' for token in tokens]
        return f"[{', '.join(parts)}]"

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = node.left
        right = node.right
        if isinstance(left, ast.Constant) and isinstance(left.value, str):
            try:
                tokens = shlex.split(left.value)
            except ValueError:
                return None
            if not tokens:
                return None
            parts = [f'"{token}"' for token in tokens]
            parts.append(ast.unparse(right))
            return f"[{', '.join(parts)}]"
    return None


# ---------------------------------------------------------------------------
# 3. SQL 字符串拼接 → 参数化
# ---------------------------------------------------------------------------


def _plan_sql(
    finding: FindingItem, path: str, content: str, lines: list[str], *, placeholder: str
) -> PatchPlan | None:
    index = finding.line - 1
    if not (0 <= index < len(lines)):
        return None
    line = lines[index]
    match = EXECUTE_CALL.match(line)
    if not match:
        return None
    rewritten = _rewrite_sql_expression(match.group("args"), placeholder=placeholder)
    if rewritten is None:
        return None
    sql, params = rewritten
    patched_lines = list(lines)
    patched_lines[index] = (
        f"{match.group('indent')}{match.group('prefix')}{match.group('head')}({sql}, {params})"
    )
    return PatchPlan(
        category=FixCategory.SQL_PARAMETERIZATION,
        path=path,
        original=content,
        patched="\n".join(patched_lines) + ("\n" if content.endswith("\n") else ""),
        finding_refs=[finding_ref(finding)],
        changed_functions=[finding.symbol] if finding.symbol else [],
        notes=[f"改写为参数化查询（占位符 {placeholder}）"],
    )


def _rewrite_sql_expression(expression: str, *, placeholder: str) -> tuple[str, str] | None:
    """把拼接/格式化构造的 SQL 表达式改写为 (SQL 字面量, 参数元组字面量)。"""
    text = expression.strip().rstrip(",")
    try:
        node = ast.parse(text, mode="eval").body
    except SyntaxError:
        return None

    sql_parts: list[str] = []
    params: list[str] = []

    def walk(current: ast.AST) -> bool:
        if isinstance(current, ast.BinOp) and isinstance(current.op, (ast.Add, ast.Mod)):
            if isinstance(current.op, ast.Add):
                return walk(current.left) and walk(current.right)
            # % 格式化：左侧必须是 SQL 字符串字面量
            literal = _string_literal(current.left)
            if literal is None or not _mentions_sql(literal):
                return False
            placeholders = re.findall(r"%[sd]", literal)
            values = (
                list(current.right.elts)
                if isinstance(current.right, ast.Tuple)
                else [current.right]
            )
            if len(placeholders) != len(values) or not placeholders:
                return False
            sql_parts.append(re.sub(r"%[sd]", placeholder, literal))
            params.extend(ast.unparse(value) for value in values)
            return True
        if isinstance(current, ast.JoinedStr):
            literal_parts: list[str] = []
            for value in current.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    literal_parts.append(value.value)
                elif isinstance(value, ast.FormattedValue):
                    literal_parts.append(placeholder)
                    params.append(ast.unparse(value.value))
                else:
                    return False
            rendered = "".join(literal_parts)
            if not _mentions_sql(rendered):
                return False
            sql_parts.append(rendered)
            return True
        if isinstance(current, ast.Call) and isinstance(current.func, ast.Attribute):
            if current.func.attr == "format":
                literal = _string_literal(current.func.value)
                if literal is None or not _mentions_sql(literal):
                    return False
                count = literal.count("{}") + len(re.findall(r"\{\d+\}", literal))
                if count == 0 or count != len(current.args):
                    return False
                sql_parts.append(re.sub(r"\{\d*\}", placeholder, literal))
                params.extend(ast.unparse(arg) for arg in current.args)
                return True
            return False
        if isinstance(current, ast.Constant) and isinstance(current.value, str):
            if current.value:
                sql_parts.append(current.value)
            return True
        if isinstance(current, (ast.Name, ast.Attribute, ast.Subscript, ast.Call, ast.BinOp)):
            # 拼接链中的非字面量操作数按"值"处理：插入占位符并收集参数。
            # 注意：若该表达式本身承载 SQL 片段，则不属于本规则的可安全改写形态。
            sql_parts.append(placeholder)
            params.append(ast.unparse(current))
            return True
        return False

    if not walk(node) or not params:
        return None
    sql_text = "".join(sql_parts)
    if not _mentions_sql(sql_text):
        return None
    sql_literal = json_literal(sql_text)
    params_literal = params[0] if len(params) == 1 else f"({', '.join(params)}{',' if len(params) == 1 else ''})"
    if len(params) == 1:
        params_literal = f"({params[0]},)"
    return sql_literal, params_literal


def _string_literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _mentions_sql(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in SQL_KEYWORDS)


def json_literal(text: str) -> str:
    """把文本渲染成 Python 字符串字面量（使用双引号并转义）。"""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


__all__ = [
    "ENV_NAME",
    "PatchPlan",
    "finding_ref",
    "json_literal",
    "plan_fix",
]
