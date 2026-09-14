"""危险模式预审查（FR-059）。

进入沙箱之前先做一次确定性静态检查：命中危险模式的补丁或文件直接拦截，
不进入测试阶段。这属于确定性边界，LLM 不得改变结论（宪法第九条）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DANGEROUS_PATTERNS: tuple[tuple[str, str], ...] = (
    ("os.system", r"\bos\.system\s*\("),
    ("os.popen", r"\bos\.popen\s*\("),
    ("eval", r"(?<![\w.])(eval|exec)\s*\("),
    ("dynamic_import", r"__import__\s*\("),
    ("ctypes", r"\bctypes\b"),
    ("socket", r"\bsocket\.(socket|create_connection)\b"),
    ("network_client", r"\b(requests|urllib\.request|http\.client|httpx)\.(get|post|put|request|urlopen)\b"),
    ("subprocess_shell", r"subprocess\.\w+\([^)]*shell\s*=\s*True"),
    ("privilege_escalation", r"\b(setuid|seteuid|setgid|os\.setuid|os\.setgid)\s*\("),
    ("filesystem_destructive", r"\b(shutil\.rmtree|os\.removedirs|os\.unlink)\s*\("),
    ("sensitive_path", r"[\"'](/etc/(passwd|shadow)|/proc/self|/var/run/docker\.sock)[\"']"),
    ("pickle_load", r"\bpickle\.loads?\s*\("),
    ("fork_bomb", r"while\s+True\s*:\s*\n\s*(os\.fork|subprocess)"),
    ("container_escape", r"(/var/run/docker\.sock|nsenter|/proc/1/root)"),
)

_COMPILED = tuple((name, re.compile(pattern)) for name, pattern in DANGEROUS_PATTERNS)


@dataclass(slots=True)
class PreScanOutcome:
    ok: bool
    blocked_patterns: list[str]
    hits: list[dict[str, str]]

    def to_payload(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "blocked_patterns": list(self.blocked_patterns),
            "hits": list(self.hits),
        }


def prescan_text(text: str, *, source: str = "<patch>") -> PreScanOutcome:
    blocked: list[str] = []
    hits: list[dict[str, str]] = []
    for name, pattern in _COMPILED:
        for match in pattern.finditer(text):
            blocked.append(name)
            line = text.count("\n", 0, match.start()) + 1
            hits.append({"pattern": name, "source": source, "line": str(line)})
    unique = sorted(set(blocked))
    return PreScanOutcome(ok=not unique, blocked_patterns=unique, hits=hits)


def prescan_patch(patch: str) -> PreScanOutcome:
    """只检查补丁中新增的行：删除行里的危险模式不构成引入风险。"""
    added = [
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    return prescan_text("\n".join(added), source="<patch:added-lines>")


def prescan_files(files: dict[str, str]) -> PreScanOutcome:
    blocked: list[str] = []
    hits: list[dict[str, str]] = []
    for path, content in sorted(files.items()):
        if not path.endswith(".py"):
            continue
        outcome = prescan_text(content, source=path)
        blocked.extend(outcome.blocked_patterns)
        hits.extend(outcome.hits)
    unique = sorted(set(blocked))
    return PreScanOutcome(ok=not unique, blocked_patterns=unique, hits=hits)


__all__ = [
    "DANGEROUS_PATTERNS",
    "PreScanOutcome",
    "prescan_files",
    "prescan_patch",
    "prescan_text",
]
