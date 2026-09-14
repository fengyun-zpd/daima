"""Impact Agent（FR-026、FR-027、FR-032；SRS §4.3 理解层）。

职责：基于 AST 建立符号索引与直接调用图，评估变更影响范围，输出 ``ImpactReport``。

确定性约束：
- 只做静态可解析的调用关系，动态调用（getattr/__import__/eval/exec）必须标记
  ``uncertain=true``，不得假装完成解析（SRS §6.4）；
- AST 解析失败的文件降级为文本级符号推断并记录 ``analyzers``。
"""

from __future__ import annotations

import ast
import time
from dataclasses import dataclass, field
from typing import Any

from a2a.protocol import (
    A2AMessage,
    ArtifactEnvelope,
    CallEdge,
    ChangedSymbol,
    ImpactReport,
    ImpactStats,
)
from agents.base import AgentHandler, AgentRequest, AgentResult
from domain.clock import utcnow
from domain.enums import ArtifactType, RiskLevel
from domain.ids import new_artifact_id, new_message_id
from domain.impact_policy import ImpactPolicy

DYNAMIC_CALL_NAMES = frozenset({"getattr", "setattr", "__import__", "eval", "exec", "globals", "locals", "vars"})
LOW_MAX_FILES = 2
MEDIUM_MAX_FILES = 5


@dataclass(slots=True)
class Symbol:
    qualname: str
    kind: str
    file: str
    line: int
    end_line: int


@dataclass(slots=True)
class ModuleAnalysis:
    path: str
    module: str
    tree: ast.AST | None
    symbols: dict[str, Symbol] = field(default_factory=dict)
    imports: dict[str, str] = field(default_factory=dict)
    dynamic_calls: list[str] = field(default_factory=list)
    degraded: bool = False


def module_name(path: str) -> str:
    stem = path[:-3] if path.endswith(".py") else path
    stem = stem.replace("\\", "/")
    if stem.endswith("/__init__"):
        stem = stem[: -len("/__init__")]
    return stem.replace("/", ".")


class ImpactAgent(AgentHandler):
    agent_id = "impact-agent"

    def __init__(self, policy: ImpactPolicy | None = None) -> None:
        self.policy = policy or ImpactPolicy()

    def handle(self, request: AgentRequest) -> AgentResult:
        started = time.perf_counter()
        workspace = request.workspace

        analyses: dict[str, ModuleAnalysis] = {}
        for path in workspace.python_files:
            analyses[path] = self._analyse_module(path, workspace.files.get(path, ""))

        symbol_index: dict[str, Symbol] = {}
        for analysis in analyses.values():
            symbol_index.update(analysis.symbols)

        changed_symbols, symbol_details = self._changed_symbols(workspace, analyses, symbol_index)
        edges, uncertain = self._call_graph(analyses, symbol_index)

        changed_set = set(changed_symbols)
        direct_callers = sorted({edge.caller for edge in edges if edge.callee in changed_set and edge.caller not in changed_set})
        direct_callees = sorted({edge.callee for edge in edges if edge.caller in changed_set and edge.callee not in changed_set})

        affected_files = set(workspace.changed_files)
        for edge in edges:
            if edge.callee in changed_set or edge.caller in changed_set:
                affected_files.add(edge.file)
        for name in direct_callers:
            symbol = symbol_index.get(name)
            if symbol:
                affected_files.add(symbol.file)

        dynamic_calls = sorted(
            {call for analysis in analyses.values() for call in analysis.dynamic_calls}
        )
        uncertain = uncertain or bool(dynamic_calls)
        degraded = any(analysis.degraded for analysis in analyses.values())

        risk_level = self._risk_level(
            affected_file_count=len(affected_files),
            changed_file_paths=sorted(affected_files),
            has_critical_symbol=bool(dynamic_calls) or degraded,
        )

        report = ImpactReport(
            changed_symbols=changed_symbols,
            direct_callers=direct_callers,
            direct_callees=direct_callees,
            affected_files=sorted(affected_files),
            risk_level=risk_level,
            uncertain=uncertain,
            dynamic_calls=dynamic_calls,
            symbol_details=symbol_details,
            call_graph=edges,
            stats=ImpactStats(
                changed_symbol_count=len(changed_symbols),
                affected_file_count=len(affected_files),
                caller_count=len(direct_callers),
                callee_count=len(direct_callees),
            ),
            analyzers=self._analyzers(degraded),
        )

        artifact = ArtifactEnvelope.build(
            artifact_id=new_artifact_id(),
            task_id=request.task_id,
            artifact_type=ArtifactType.IMPACT_REPORT,
            data=report.model_dump(mode="json"),
        )
        message = A2AMessage(
            message_id=new_message_id(),
            task_id=request.task_id,
            type="task.completed",
            role="agent",
            correlation_id=request.task.correlation_id,
            artifact_refs=[artifact.artifact_id],
            error=None,
            created_at=utcnow(),
        )
        return AgentResult(
            artifacts=[artifact],
            messages=[message],
            stats={
                "changed_symbols": len(changed_symbols),
                "affected_files": len(affected_files),
                "call_edges": len(edges),
                "risk_level": str(risk_level),
                "uncertain": uncertain,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "tool_calls": len(request.tools.trace()),
            },
        )

    # ---- 内部 -------------------------------------------------------------------
    def _analyse_module(self, path: str, content: str) -> ModuleAnalysis:
        analysis = ModuleAnalysis(path=path, module=module_name(path), tree=None)
        try:
            tree = ast.parse(content, filename=path)
        except SyntaxError:
            analysis.degraded = True
            return analysis
        analysis.tree = tree

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                parent = _enclosing_class(tree, node)
                qualname = f"{analysis.module}.{parent}.{node.name}" if parent else f"{analysis.module}.{node.name}"
                analysis.symbols[qualname] = Symbol(
                    qualname=qualname,
                    kind="method" if parent else "function",
                    file=path,
                    line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno) or node.lineno,
                )
            elif isinstance(node, ast.ClassDef):
                qualname = f"{analysis.module}.{node.name}"
                analysis.symbols[qualname] = Symbol(
                    qualname=qualname,
                    kind="class",
                    file=path,
                    line=node.lineno,
                    end_line=getattr(node, "end_lineno", node.lineno) or node.lineno,
                )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    analysis.imports[alias.asname or alias.name.split(".")[0]] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    analysis.imports[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            elif isinstance(node, ast.Call):
                name = _call_name(node)
                if name in DYNAMIC_CALL_NAMES:
                    analysis.dynamic_calls.append(f"{path}:{node.lineno}:{name}")
        return analysis

    def _changed_symbols(
        self,
        workspace,
        analyses: dict[str, ModuleAnalysis],
        symbol_index: dict[str, Symbol],
    ) -> tuple[list[str], list[ChangedSymbol]]:
        details: dict[str, ChangedSymbol] = {}
        for path, lines in workspace.changed_lines.items():
            analysis = analyses.get(path)
            if analysis is None:
                continue
            for line in sorted(lines):
                symbol = self._innermost_symbol(analysis, line)
                if symbol is None:
                    continue
                existing = details.get(symbol.qualname)
                if existing is None:
                    details[symbol.qualname] = ChangedSymbol(
                        name=symbol.qualname,
                        kind=symbol.kind,
                        file=symbol.file,
                        line=symbol.line,
                        changed_lines=[line],
                    )
                elif line not in existing.changed_lines:
                    existing.changed_lines.append(line)

            if not details and lines:
                module_symbol = f"{analysis.module}.<module>"
                details[module_symbol] = ChangedSymbol(
                    name=module_symbol,
                    kind="module",
                    file=path,
                    line=min(lines),
                    changed_lines=sorted(lines),
                )
        return sorted(details), [details[key] for key in sorted(details)]

    def _innermost_symbol(self, analysis: ModuleAnalysis, line: int) -> Symbol | None:
        best: Symbol | None = None
        for symbol in analysis.symbols.values():
            inside = symbol.line <= line <= symbol.end_line
            if inside and (best is None or symbol.line > best.line):
                best = symbol
        return best

    def _call_graph(
        self, analyses: dict[str, ModuleAnalysis], symbol_index: dict[str, Symbol]
    ) -> tuple[list[CallEdge], bool]:
        edges: list[CallEdge] = []
        uncertain = False
        for analysis in analyses.values():
            if analysis.tree is None:
                uncertain = True
                continue
            local_modules = self._local_module_aliases(analysis, analyses)
            for node in ast.walk(analysis.tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                parent = _enclosing_class(analysis.tree, node)
                caller = (
                    f"{analysis.module}.{parent}.{node.name}" if parent else f"{analysis.module}.{node.name}"
                )
                for call in [item for item in ast.walk(node) if isinstance(item, ast.Call)]:
                    callee = self._resolve_callee(call, analysis, local_modules, symbol_index)
                    if callee:
                        edges.append(
                            CallEdge(caller=caller, callee=callee, file=analysis.path, line=call.lineno)
                        )
        # 去重并稳定排序，保证可重复性
        unique = {(edge.caller, edge.callee, edge.file, edge.line): edge for edge in edges}
        return [unique[key] for key in sorted(unique)], uncertain

    def _local_module_aliases(
        self, analysis: ModuleAnalysis, analyses: dict[str, ModuleAnalysis]
    ) -> dict[str, str]:
        by_module = {item.module: item for item in analyses.values()}
        aliases: dict[str, str] = {}
        for alias, target in analysis.imports.items():
            if target in by_module:
                aliases[alias] = target
                continue
            if "." in target:
                head, _, tail = target.rpartition(".")
                if head in by_module:
                    aliases[alias] = f"{head}.{tail}"
        return aliases

    def _resolve_callee(
        self,
        call: ast.Call,
        analysis: ModuleAnalysis,
        local_modules: dict[str, str],
        symbol_index: dict[str, Symbol],
    ) -> str | None:
        name = _call_name(call)
        if not name:
            return None
        if isinstance(call.func, ast.Name):
            candidate = f"{analysis.module}.{name}"
            if candidate in symbol_index:
                return candidate
            if name in local_modules:
                return None
            return None
        if isinstance(call.func, ast.Attribute):
            parts = name.split(".")
            head = parts[0]
            if head in local_modules:
                candidate = ".".join([local_modules[head], *parts[1:]])
                if candidate in symbol_index:
                    return candidate
                parent = ".".join([local_modules[head], *parts[1:-1]])
                if parent in symbol_index:
                    return parent
            candidate = f"{analysis.module}.{name}"
            if candidate in symbol_index:
                return candidate
        return None

    def _risk_level(
        self, *, affected_file_count: int, changed_file_paths: list[str], has_critical_symbol: bool
    ) -> RiskLevel:
        """风险级别由 ``ImpactPolicy`` 统一判定（确定性边界）。"""
        level = self.policy.classify(affected_files=changed_file_paths)
        if has_critical_symbol and level is RiskLevel.LOW:
            # 存在动态调用或降级分析时至少升到 medium，避免低估不确定性。
            return RiskLevel.MEDIUM
        return level

    def _analyzers(self, degraded: bool) -> list[str]:
        analyzers = ["ast", "callgraph"]
        if degraded:
            analyzers.append("text-degraded")
        return analyzers


def _call_name(node: ast.Call) -> str:
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


def _enclosing_class(tree: ast.AST, target: ast.AST) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if child is target:
                    return node.name
    return None


def impact_summary(report: ImpactReport) -> dict[str, Any]:
    return {
        "changed_symbols": list(report.changed_symbols),
        "affected_files": list(report.affected_files),
        "risk_level": str(report.risk_level),
        "uncertain": report.uncertain,
    }


__all__ = ["DYNAMIC_CALL_NAMES", "ImpactAgent", "ModuleAnalysis", "Symbol", "impact_summary", "module_name"]
