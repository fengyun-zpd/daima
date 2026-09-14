"""确定性规则基础类型（FR-020~FR-023）。

规则引擎属于确定性边界：命中、严重级别和证据不受 LLM 影响（宪法第九条）。
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from domain.enums import FixCategory, Severity

RULE_SET_VERSION = "rules-v1"


class RuleKind(StrEnum):
    REGEX = "regex"
    AST = "ast"
    AST_DATAFLOW = "ast_dataflow"


@dataclass(slots=True, frozen=True)
class Rule:
    rule_id: str
    title: str
    cwe: str
    severity: Severity
    confidence_base: float
    kind: RuleKind = RuleKind.REGEX
    pattern: str | None = None
    check: str | None = None
    auto_fixable: bool = False
    fix_category: FixCategory | None = None
    description: str = ""
    references: tuple[str, ...] = ()

    def compiled(self) -> re.Pattern[str]:
        if not self.pattern:
            raise ValueError(f"规则 {self.rule_id} 没有正则表达式")
        return re.compile(self.pattern)


@dataclass(slots=True)
class RuleHit:
    line: int
    evidence: str
    message: str
    symbol: str | None = None
    context_confirmed: bool = False
    severity: Severity | None = None
    historical_only: bool = False
    dynamic_uncertain: bool = False


@dataclass(slots=True)
class FunctionIndex:
    """函数/类区间索引，用于把命中行映射到符号。"""

    ranges: list[tuple[int, int, str]] = field(default_factory=list)

    def add(self, start: int, end: int, qualname: str) -> None:
        self.ranges.append((start, end, qualname))

    def symbol_for(self, line: int) -> str | None:
        best: tuple[int, str] | None = None
        for start, end, name in self.ranges:
            if start <= line <= end and (best is None or start > best[0]):
                best = (start, name)
        return best[1] if best else None

    def range_for(self, name: str) -> tuple[int, int] | None:
        for start, end, candidate in self.ranges:
            if candidate == name:
                return start, end
        return None


@dataclass(slots=True)
class FileContext:
    """单个文件的扫描上下文。"""

    path: str
    lines: list[str]
    changed_lines: set[int]
    tree: ast.AST | None
    functions: FunctionIndex
    parse_error: str | None = None

    @classmethod
    def build(cls, path: str, content: str, changed_lines: set[int]) -> FileContext:
        lines = content.splitlines()
        tree: ast.AST | None = None
        parse_error: str | None = None
        try:
            tree = ast.parse(content, filename=path)
        except SyntaxError as exc:
            parse_error = f"{exc.msg} (line {exc.lineno})"
        index = FunctionIndex()
        if tree is not None:
            _index_functions(tree, index, prefix="")
        return cls(path=path, lines=lines, changed_lines=set(changed_lines), tree=tree,
                   functions=index, parse_error=parse_error)

    def line_text(self, line: int) -> str:
        if 1 <= line <= len(self.lines):
            return self.lines[line - 1]
        return ""

    def in_changed_lines(self, line: int) -> bool:
        return line in self.changed_lines

    def symbol_for(self, line: int) -> str | None:
        return self.functions.symbol_for(line)


def _index_functions(tree: ast.AST, index: FunctionIndex, *, prefix: str) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            index.add(node.lineno, getattr(node, "end_lineno", node.lineno) or node.lineno, node.name)
        elif isinstance(node, ast.ClassDef):
            qualname = f"{prefix}{node.name}" if not prefix else f"{prefix}.{node.name}"
            index.add(node.lineno, getattr(node, "end_lineno", node.lineno) or node.lineno, qualname)


AstCheck = Callable[[FileContext, Rule], Iterable[RuleHit]]

#: AST 检查函数注册表（由 python_rules 填充）。
AST_CHECKS: dict[str, AstCheck] = {}


def register_check(name: str) -> Callable[[AstCheck], AstCheck]:
    def decorator(function: AstCheck) -> AstCheck:
        AST_CHECKS[name] = function
        return function

    return decorator


def node_line(node: ast.AST) -> int:
    return getattr(node, "lineno", 1)


def call_name(node: ast.Call) -> str:
    """把调用表达式规约为 ``模块.属性`` 或函数名。"""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts = [func.attr]
        current = func.value
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if isinstance(current, ast.Name):
            parts.append(current.id)
        return ".".join(reversed(parts))
    return ""


def is_string_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def string_value(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def format_source(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001 - 反解析失败时退化为节点名
        return type(node).__name__


__all__ = [
    "AST_CHECKS",
    "RULE_SET_VERSION",
    "AstCheck",
    "FileContext",
    "FunctionIndex",
    "Rule",
    "RuleHit",
    "RuleKind",
    "call_name",
    "format_source",
    "is_string_literal",
    "node_line",
    "register_check",
    "string_value",
]
