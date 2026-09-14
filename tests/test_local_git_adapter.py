from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.review_local_repo import LocalRepoError, build_review_input, resolve_repo


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "demo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "test")
    (repo / "app.py").write_text("print('base')\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    (repo / "app.py").write_text("print('changed')\n", encoding="utf-8")
    git(repo, "commit", "-qam", "change")
    return repo


def test_build_review_input_returns_diff_and_base_sha(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    diff, base_commit = build_review_input(repo, base="HEAD~1", head="HEAD")
    assert base_commit == git(repo, "rev-parse", "HEAD~1").strip()
    assert "diff --git a/app.py b/app.py" in diff
    assert "-print('base')" in diff
    assert "+print('changed')" in diff


def test_resolve_repo_returns_git_root_from_nested_path(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    nested = repo / "nested"
    nested.mkdir()
    assert resolve_repo(nested) == repo.resolve()


def test_empty_diff_is_rejected(tmp_path: Path) -> None:
    repo = make_repo(tmp_path)
    with pytest.raises(LocalRepoError, match="没有文件变更"):
        build_review_input(repo, base="HEAD", head="HEAD")


def test_non_git_directory_is_rejected(tmp_path: Path) -> None:
    candidate = tmp_path / "not-a-directory.txt"
    candidate.write_text("not a repo", encoding="utf-8")
    with pytest.raises(LocalRepoError, match="仓库目录不存在"):
        resolve_repo(candidate)
