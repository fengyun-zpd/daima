"""工件内容存储（docs/01 §4：大内容通过 Artifact 引用传递）。

数据库保存元数据、哈希和引用；大内容（工作区快照、补丁、沙箱输出）落到内容目录。
``data_ref`` 使用相对键，禁止绝对路径与穿越。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from domain.errors import CodePilotError, ErrorCode
from domain.safepath import normalize_path
from domain.sanitize import payload_hash

DEFAULT_DATA_ROOT = Path(os.environ.get("CODEPILOT_DATA_ROOT", "./var"))


class ContentStore:
    """基于文件系统的内容存储，所有键都是仓库根下的相对路径。"""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else DEFAULT_DATA_ROOT
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- 路径 -------------------------------------------------------------------
    def _resolve(self, ref: str) -> Path:
        key = ref.strip().replace("\\", "/").lstrip("/")
        try:
            safe = normalize_path(key)
        except CodePilotError as exc:
            raise CodePilotError(
                ErrorCode.INVALID_INPUT, f"非法内容引用：{ref}", details={"ref": ref}
            ) from exc
        target = (self.root / safe).resolve()
        root = self.root.resolve()
        if root != target and root not in target.parents:
            raise CodePilotError(
                ErrorCode.INVALID_INPUT, f"内容引用越界：{ref}", details={"ref": ref}
            )
        return target

    # ---- 读写 -------------------------------------------------------------------
    def write_json(self, ref: str, payload: Any, *, sanitize: bool = False) -> str:
        if sanitize:
            from domain.sanitize import sanitize_structured

            payload = sanitize_structured(payload)
        target = self._resolve(ref)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        return ref

    def write_text(self, ref: str, text: str) -> str:
        target = self._resolve(ref)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return ref

    def read_json(self, ref: str) -> Any:
        target = self._resolve(ref)
        if not target.exists():
            raise CodePilotError(
                ErrorCode.RESOURCE_NOT_FOUND, f"内容不存在：{ref}", details={"ref": ref}
            )
        return json.loads(target.read_text(encoding="utf-8"))

    def read_text(self, ref: str) -> str:
        target = self._resolve(ref)
        if not target.exists():
            raise CodePilotError(
                ErrorCode.RESOURCE_NOT_FOUND, f"内容不存在：{ref}", details={"ref": ref}
            )
        return target.read_text(encoding="utf-8")

    def exists(self, ref: str) -> bool:
        return self._resolve(ref).exists()

    def delete_tree(self, prefix: str) -> None:
        """清理临时目录（FR-053：无残留临时文件）。"""
        target = self._resolve(prefix)
        if target.exists() and target.is_dir():
            import shutil

            shutil.rmtree(target, ignore_errors=True)

    # ---- 约定键 -----------------------------------------------------------------
    @staticmethod
    def workspace_ref(task_id: str) -> str:
        return f"workspaces/{task_id}.json"

    @staticmethod
    def patch_ref(task_id: str, patch_version: int) -> str:
        return f"patches/{task_id}/v{patch_version}.diff"

    @staticmethod
    def sandbox_ref(sandbox_run_id: str) -> str:
        return f"sandbox/{sandbox_run_id}.json"

    @staticmethod
    def report_ref(kind: str, identifier: str) -> str:
        return f"reports/{kind}/{identifier}.json"

    def store_workspace(self, task_id: str, payload: dict[str, Any]) -> str:
        ref = self.workspace_ref(task_id)
        return self.write_json(ref, payload)

    def load_workspace(self, task_id: str) -> dict[str, Any] | None:
        ref = self.workspace_ref(task_id)
        if not self.exists(ref):
            return None
        return self.read_json(ref)

    def store_sandbox_output(self, sandbox_run_id: str, payload: dict[str, Any]) -> str:
        ref = self.sandbox_ref(sandbox_run_id)
        return self.write_json(ref, payload, sanitize=True)

    def hash_ref(self, ref: str) -> str:
        return payload_hash(self.read_text(ref))


__all__ = ["DEFAULT_DATA_ROOT", "ContentStore"]
