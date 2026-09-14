"""合成仓库与任务分支写入（FR-041~FR-043、宪法第六条）。

MVP 不接入真实 Git 平台，因此"任务分支"是内容目录下的**合成仓库**：
- 只有通过审批的补丁才能写入，且只能写入 `codepilot/<task_id>` 分支；
- `main` / `master` / `develop` 在策略层被硬拒绝，任何角色都无法写入；
- 写入前先 `git apply --check`，失败即回滚工作区，绝不留下半成品；
- 每个动作都返回可审计的 commit、文件校验和与脱敏输出。
"""

from __future__ import annotations

import contextlib
import hashlib
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from domain.errors import CodePilotError, ErrorCode
from domain.safepath import ensure_allowed_path, normalize_path
from domain.sanitize import sanitize_text

PROTECTED_BRANCHES = frozenset({"main", "master", "develop", "head"})
TASK_BRANCH_PREFIX = "codepilot/"
GIT_TIMEOUT_SECONDS = 30


@dataclass(slots=True)
class BranchResult:
    branch: str
    commit: str
    changed_files: list[str] = field(default_factory=list)
    checksums: dict[str, str] = field(default_factory=dict)
    output: str = ""
    applied: bool = True

    def to_payload(self) -> dict[str, object]:
        return {
            "branch": self.branch,
            "commit": self.commit,
            "changed_files": list(self.changed_files),
            "checksums": dict(self.checksums),
            "applied": self.applied,
            "output": self.output,
        }


def task_branch_name(task_id: str) -> str:
    return f"{TASK_BRANCH_PREFIX}{task_id}"


def assert_writable_branch(branch: str, *, task_id: str) -> str:
    """分支白名单：只允许该任务自己的任务分支（FR-041）。"""
    candidate = branch.strip()
    if candidate.lower() in PROTECTED_BRANCHES:
        raise CodePilotError(
            ErrorCode.FORBIDDEN,
            f"禁止写入受保护分支 {candidate}（FR-041）",
            details={"branch": candidate},
        )
    expected = task_branch_name(task_id)
    if candidate != expected:
        raise CodePilotError(
            ErrorCode.FORBIDDEN,
            f"只允许写入本任务分支 {expected}",
            details={"branch": candidate, "expected": expected},
        )
    return candidate


class SyntheticRepo:
    """内容目录下的合成 Git 仓库，用于承载"任务分支"写入。"""

    def __init__(self, root: str | Path, task_id: str) -> None:
        self.root = Path(root)
        self.task_id = task_id
        self.path = self.root / "repos" / task_id
        self.branch = task_branch_name(task_id)

    # ---- 生命周期 -----------------------------------------------------------------
    def ensure(self, files: dict[str, str], *, base_commit: str = "synthetic-base-001") -> BranchResult:
        """创建（或复用）仓库，写入工作区文件并提交基线。"""
        if self.path.exists():
            return BranchResult(branch=self.branch, commit=self._rev_parse("HEAD"), applied=False,
                                changed_files=sorted(files), output="existing repository")

        self.path.mkdir(parents=True, exist_ok=True)
        for raw_path, content in sorted(files.items()):
            target = self.path / ensure_allowed_path(raw_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            # newline="\n"：禁止 Windows 平台把 \n 翻译成 \r\n，
            # 否则仓库内容与补丁上下文（LF）不一致，git apply 会失败。
            target.write_text(content, encoding="utf-8", newline="\n")

        self._git("init", "-q", "-b", "main", ".")
        self._git("config", "user.email", "codepilot@local")
        self._git("config", "user.name", "codepilot")
        self._git("add", "-A")
        self._git("commit", "-qm", f"base {base_commit}")
        # 任务分支：所有写入只发生在这里
        self._git("checkout", "-q", "-b", self.branch)
        return BranchResult(
            branch=self.branch,
            commit=self._rev_parse("HEAD"),
            changed_files=sorted(files),
            checksums={path: _checksum(content) for path, content in files.items()},
            applied=False,
            output="initialized",
        )

    def apply_patch(self, patch_text: str, *, expected_branch: str | None = None) -> BranchResult:
        """把补丁应用到任务分支：先 check，再 apply，再提交。"""
        assert_writable_branch(expected_branch or self.branch, task_id=self.task_id)
        if not self.path.exists():
            raise CodePilotError(ErrorCode.PATCH_INVALID, "合成仓库尚未初始化")

        patch_file = self.path.parent / f"{self.task_id}.patch"
        # 同上：补丁必须保持 LF，平台换行翻译会让上下文行无法匹配。
        patch_file.write_text(patch_text, encoding="utf-8", newline="\n")
        checksums_before = self._checksums()
        try:
            check, mode = self._apply_check(patch_file)
            if check.returncode != 0:
                raise CodePilotError(
                    ErrorCode.PATCH_INVALID,
                    f"补丁无法应用：{sanitize_text(check.stderr or check.stdout)}",
                    details={"exit_code": check.returncode, "mode": mode},
                )
            applied = self._git(
                "apply", "--whitespace=nowarn", *(["--unidiff-zero"] if mode == "unidiff-zero" else []),
                str(patch_file), check=False,
            )
            if applied.returncode != 0:
                # FR-043：失败立即回滚工作区，保留补丁前校验和
                self._git("checkout", "--", ".", check=False)
                raise CodePilotError(
                    ErrorCode.PATCH_INVALID,
                    f"补丁应用失败并已回滚：{sanitize_text(applied.stderr or applied.stdout)}",
                )
            self._git("add", "-A")
            self._git(
                "commit", "-qm", f"codepilot: apply patch for {self.task_id}"
            )
            changed = sorted(
                path for path, digest in self._checksums().items() if checksums_before.get(path) != digest
            )
            return BranchResult(
                branch=self.branch,
                commit=self._rev_parse("HEAD"),
                changed_files=changed,
                checksums={path: self._checksums()[path] for path in changed},
                output=sanitize_text(f"[apply mode: {mode}] " + applied.stdout + applied.stderr),
            )
        finally:
            with contextlib.suppress(OSError):
                patch_file.unlink()

    def _apply_check(self, patch_file: Path) -> tuple[subprocess.CompletedProcess[str], str]:
        """先用严格模式校验；仅当补丁缺少尾部上下文时退化为 ``--unidiff-zero``。

        ``git apply`` 默认要求 hunk 至少带一行上下文；位于文件末尾的修改天然没有尾部上下文，
        此时严格模式会误判为"无法应用"。``--unidiff-zero`` 只放宽上下文要求，
        被删除行仍必须逐字匹配，因此不会放过真正不匹配的补丁。
        """
        strict = self._git("apply", "--check", "--whitespace=nowarn", str(patch_file), check=False)
        if strict.returncode == 0:
            return strict, "strict"
        relaxed = self._git(
            "apply", "--check", "--whitespace=nowarn", "--unidiff-zero", str(patch_file), check=False
        )
        if relaxed.returncode == 0:
            return relaxed, "unidiff-zero"
        return strict, "strict"

    def rollback(self) -> BranchResult:
        """回滚到任务分支的上一个提交（FR-043）。"""
        self._git("reset", "--hard", "HEAD~1", check=False)
        return BranchResult(
            branch=self.branch,
            commit=self._rev_parse("HEAD"),
            applied=False,
            output="rolled back",
        )

    def cleanup(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    # ---- 查询 -------------------------------------------------------------------
    def current_branch(self) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def head_commit(self) -> str:
        return self._rev_parse("HEAD")

    def _checksums(self) -> dict[str, str]:
        result: dict[str, str] = {}
        if not self.path.exists():
            return result
        for item in sorted(self.path.rglob("*")):
            if not item.is_file() or ".git" in item.parts:
                continue
            relative = normalize_path(item.relative_to(self.path).as_posix())
            result[relative] = _checksum(item.read_text(encoding="utf-8", errors="replace"))
        return result

    def _rev_parse(self, ref: str) -> str:
        return self._git("rev-parse", ref, check=False).stdout.strip()

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["git", *args],
            cwd=self.path,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
        if check and completed.returncode != 0:
            raise CodePilotError(
                ErrorCode.INTERNAL_ERROR,
                f"git {' '.join(args)} 失败：{sanitize_text(completed.stderr or completed.stdout)}",
                details={"exit_code": completed.returncode},
            )
        return completed


def _checksum(content: str) -> str:
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


__all__ = [
    "PROTECTED_BRANCHES",
    "TASK_BRANCH_PREFIX",
    "BranchResult",
    "SyntheticRepo",
    "assert_writable_branch",
    "task_branch_name",
]
