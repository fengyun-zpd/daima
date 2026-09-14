"""Dashboard 交付自检：前端源码、代理配置与后端 API 契约保持一致。

用法：
    python scripts/web_check.py            # 静态检查（无 Node 依赖，可离线运行）
    python scripts/web_check.py --build    # 额外执行 npm run build（需要已安装 node_modules）

检查项：
1. web/ 关键源码文件存在；
2. web/src/api.ts 中调用的每个路径都真实存在于后端 OpenAPI；
3. vite dev 代理与 nginx 生产代理覆盖同一组路径前缀；
4. 写请求统一携带 Idempotency-Key 与身份头；
5. --build 时真实执行 tsc --noEmit + vite build。

该脚本只做只读检查，不修改任何文件。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
WEB = ROOT / "web"
API_SRC = WEB / "src" / "api.ts"
VITE_CONFIG = WEB / "vite.config.ts"
NGINX_CONF = ROOT / "docker" / "web" / "nginx.conf"

REQUIRED_SOURCES = [
    "package.json",
    "tsconfig.json",
    "vite.config.ts",
    "index.html",
    "src/main.tsx",
    "src/App.tsx",
    "src/api.ts",
    "src/types.ts",
    "src/styles.css",
    "src/components/Common.tsx",
    "src/components/CreateForm.tsx",
    "src/components/TaskView.tsx",
    "src/components/AuditPanel.tsx",
]

PROXY_PREFIXES = ["/api", "/internal", "/healthz", "/readyz"]

# 前端源码中以字面量/模板串形式出现的后端路径。
PATH_LITERAL = re.compile(r"[`\"']((?:/(?:api|internal|healthz|readyz)[^`\"']*)?)[`\"']")

# 前端必须覆盖的工作台能力 -> 后端路径（用于证明不是"营销页"）
REQUIRED_CAPABILITIES = {
    "create_review": "POST /api/v1/reviews",
    "get_review": "GET /api/v1/reviews/{task_id}",
    "trigger_fix": "POST /api/v1/reviews/{task_id}/fixes",
    "list_patches": "GET /api/v1/reviews/{task_id}/patches",
    "approval": "POST /api/v1/fixes/{patch_id}/approval",
    "merge": "POST /api/v1/fixes/{patch_id}/merge",
    "audit": "GET /api/v1/audit",
    "readyz": "GET /readyz",
}


class CheckError(RuntimeError):
    """自检失败。"""


def _read(path: pathlib.Path) -> str:
    if not path.exists():
        raise CheckError(f"缺少文件：{path.relative_to(ROOT)}")
    return path.read_text(encoding="utf-8")


def check_sources() -> list[str]:
    missing = [name for name in REQUIRED_SOURCES if not (WEB / name).exists()]
    if missing:
        raise CheckError(f"Dashboard 源码缺失：{missing}")
    return REQUIRED_SOURCES


def _template_to_openapi(path: str) -> str:
    """把前端模板串中的 ${taskId} 归一化成 OpenAPI 的 {task_id} 占位符。"""
    return re.sub(r"\$\{[^}]+\}", "{param}", path)


def frontend_paths() -> set[str]:
    source = _read(API_SRC)
    found: set[str] = set()
    # 只取以 / 开头的路径字面量（含模板串），忽略 URLSearchParams 等。
    for raw in PATH_LITERAL.findall(source):
        if not raw:
            continue
        literal = raw.split("?")[0]
        if literal.startswith(("/api", "/internal", "/healthz", "/readyz")):
            found.add(_template_to_openapi(literal))
    if not found:
        raise CheckError("未能在 web/src/api.ts 中解析出任何后端路径")
    return found


def backend_paths() -> set[str]:
    from apps.api.main import create_app

    app = create_app()
    return set(app.openapi()["paths"])


def check_contract() -> tuple[set[str], set[str]]:
    used = frontend_paths()
    declared = backend_paths()
    normalized = {re.sub(r"\{[^}]+\}", "{param}", path) for path in declared}
    unknown = sorted(path for path in used if path not in normalized)
    if unknown:
        raise CheckError(f"前端调用了后端不存在的路径：{unknown}")

    used_methods = frontend_methods()
    for capability, spec in REQUIRED_CAPABILITIES.items():
        method, path = spec.split(" ", 1)
        key = re.sub(r"\{[^}]+\}", "{param}", path)
        if key not in normalized:
            raise CheckError(f"能力缺失：{capability} 需要 {spec}，后端 OpenAPI 中不存在")
        if method not in used_methods.get(key, set()):
            raise CheckError(f"能力缺失：Dashboard 未调用 {spec}（{capability}）")
    return used, declared


def frontend_methods() -> dict[str, set[str]]:
    """粗略地把 api.ts 中的 `method: "POST"` 与其最近的路径字面量配对。"""
    source = _read(API_SRC)
    pairs: dict[str, set[str]] = {}
    # 以 `{ method: "X" }` 出现位置为锚点，向前回溯最近的路径字面量。
    for match in re.finditer(r"method:\s*\"(GET|POST|PUT|DELETE)\"", source):
        method = match.group(1)
        prefix = source[: match.start()]
        candidates = PATH_LITERAL.findall(prefix)
        path = None
        for candidate in reversed(candidates):
            if candidate.startswith("/"):
                path = candidate.split("?")[0]
                break
        if path is None:
            continue
        pairs.setdefault(_template_to_openapi(path), set()).add(method)
    return pairs


def check_proxies() -> dict[str, list[str]]:
    vite = _read(VITE_CONFIG)
    nginx = _read(NGINX_CONF)
    vite_missing = [p for p in PROXY_PREFIXES if f'"{p}"' not in vite]
    if vite_missing:
        raise CheckError(f"vite dev 代理缺少前缀：{vite_missing}")
    nginx_missing = [p for p in PROXY_PREFIXES if f"location {p}" not in nginx and f"location = {p}" not in nginx]
    if nginx_missing:
        raise CheckError(f"nginx 生产代理缺少前缀：{nginx_missing}")
    return {"vite": PROXY_PREFIXES, "nginx": PROXY_PREFIXES}


def check_idempotency_headers() -> None:
    source = _read(API_SRC)
    if 'headers["Idempotency-Key"]' not in source:
        raise CheckError("web/src/api.ts 未在所有写请求注入 Idempotency-Key")
    if "newIdempotencyKey" not in source:
        raise CheckError("web/src/api.ts 缺少幂等键生成函数")
    for header in ("X-Actor-Id", "X-Actor-Role"):
        if header not in source:
            raise CheckError(f"web/src/api.ts 未注入身份头 {header}")


# Dashboard 必须覆盖的界面能力 -> 判定用的（文件, 必须出现的片段）
FEATURE_CONTRACT: dict[str, tuple[str, tuple[str, ...]]] = {
    "transport_显示": ("src/App.tsx", ("ready.transport",)),
    "transport_类型": ("src/types.ts", ("transport:",)),
    "运行模式选择": ("src/components/CreateForm.tsx", ('"single"', '"a2a"', '"offline"')),
    "base_commit_输入": ("src/components/CreateForm.tsx", ("base_commit", "baseCommit")),
    "上下文策略选择": ("src/components/CreateForm.tsx", ('"function"', '"minimal"', '"module"')),
    "ZIP_前端大小限制": ("src/components/CreateForm.tsx", ("MAX_ZIP_BYTES", "MAX_ZIP_BASE64_CHARS")),
    "父任务状态展示": ("src/components/TaskView.tsx", ("StatusBadge", "detail.task.status")),
    "父子任务时间线": ("src/components/Common.tsx", ("TaskTimeline", "child.transport", "attempt")),
    "Finding_展示": ("src/components/TaskView.tsx", ("FindingsPanel", "severity")),
    "风险等级展示": ("src/components/TaskView.tsx", ("risk_level",)),
    "影响文件展示": ("src/components/TaskView.tsx", ("affected_files",)),
    "Artifact_展示": ("src/components/TaskView.tsx", ("artifact_type", "content_hash")),
    "PatchCandidate_展示": ("src/components/TaskView.tsx", ("patch_version", "patch_hash")),
    "unified_diff_展示": ("src/components/TaskView.tsx", ("unified diff", "latest.diff")),
    "VerifyEvidence_展示": ("src/components/TaskView.tsx", ("VerifyEvidence", "sanitized_output")),
    "审批按钮权限门禁": ("src/components/TaskView.tsx", ("canApprove", "canDecide", "canMerge")),
    "NEEDS_HUMAN_恢复入口": ("src/components/TaskView.tsx", ("ResumePanel", "NEEDS_HUMAN", "expected_version")),
    "审计事件展示": ("src/components/AuditPanel.tsx", ("AuditEvent", "event.event_type", "actor_role")),
    "错误_code_message_trace": ("src/components/Common.tsx", ("error.code", "error.message", "trace_id")),
    "轮询至终态": ("src/App.tsx", ("setInterval", "clearInterval", "TERMINAL")),
    "手动加载任务": ("src/App.tsx", ("任务 ID（可粘贴已有任务）", "refresh(taskId)")),
    "操作级幂等键复用": ("src/api.ts", ("class OperationKeys", "keyFor")),
    "生成补丁门禁": ("src/App.tsx", ('canFix: status === "REVIEWED"',)),
}


def check_feature_contract() -> list[str]:
    """Dashboard 功能契约：缺少任一项都视为不合格（防止退化为空壳页面）。"""
    missing: list[str] = []
    for feature, (relative, fragments) in FEATURE_CONTRACT.items():
        source = _read(WEB / relative)
        for fragment in fragments:
            if fragment not in source:
                missing.append(f"{feature}（{relative} 缺少 {fragment!r}）")
    if missing:
        raise CheckError("Dashboard 功能缺失：" + "；".join(missing))
    return sorted(FEATURE_CONTRACT)



def run_build() -> dict[str, object]:
    if not (WEB / "node_modules").exists():
        raise CheckError("web/node_modules 不存在，请先执行 npm install")
    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    completed = subprocess.run(
        [npm, "run", "build"],
        cwd=WEB,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        raise CheckError(f"npm run build 失败：\n{output[-4000:]}")
    dist = WEB / "dist" / "index.html"
    if not dist.exists():
        raise CheckError("npm run build 未产出 web/dist/index.html")
    assets = sorted((WEB / "dist" / "assets").glob("*")) if (WEB / "dist" / "assets").exists() else []
    js = [p for p in assets if p.suffix == ".js"]
    if not js:
        raise CheckError("web/dist/assets 下没有 JS 产物")
    return {
        "dist_index": str(dist.relative_to(ROOT)),
        "js_bytes": sum(p.stat().st_size for p in js),
        "asset_count": len(assets),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CodePilot Dashboard 自检")
    parser.add_argument("--build", action="store_true", help="额外执行 npm run build")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args(argv)

    report: dict[str, object] = {}
    try:
        report["sources"] = len(check_sources())
        used, declared = check_contract()
        report["frontend_paths"] = sorted(used)
        report["backend_path_count"] = len(declared)
        report["capabilities"] = sorted(REQUIRED_CAPABILITIES)
        report["proxies"] = check_proxies()
        check_idempotency_headers()
        report["headers"] = "ok"
        report["features"] = check_feature_contract()
        if args.build:
            report["build"] = run_build()
    except CheckError as exc:
        report["ok"] = False
        report["error"] = str(exc)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"[FAIL] web-check: {exc}")
        return 1

    report["ok"] = True
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("[ok  ] web-check: 源码齐全，前端路径全部存在于后端 OpenAPI")
        print(f"       前端调用路径 {len(used)} 个 / 后端声明路径 {len(declared)} 个")
        print(f"       代理前缀：{PROXY_PREFIXES}")
        print("       写请求身份头与 Idempotency-Key：ok")
        print(f"       功能契约：{len(report['features'])} 项全部满足")
        if args.build:
            print(f"       npm run build：ok -> {report['build']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
