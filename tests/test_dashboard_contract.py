"""Dashboard 交付契约测试。

大部分测试不依赖 Node（检查前端源码与后端契约、Compose 拓扑、nginx 代理的一致性，
防止 Dashboard 退化为"只有一个静态营销页"或"调用了不存在的接口"）；
另有真实 `npm run build` 与真实 Node 行为测试，在 `web/node_modules` 缺失时自动跳过。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"

sys.path.insert(0, str(ROOT))
from scripts import web_check  # noqa: E402


def _read(path: Path) -> str:
    assert path.exists(), f"缺少文件：{path}"
    return path.read_text(encoding="utf-8")


# --- 前端源码完整性 ---------------------------------------------------------


def test_dashboard_sources_are_present() -> None:
    assert web_check.check_sources() == web_check.REQUIRED_SOURCES


def test_dashboard_is_a_workbench_not_a_landing_page() -> None:
    app = _read(WEB / "src" / "App.tsx")
    # 工作台必须直接渲染任务视图，而不是 hero/宣传区块。
    assert "CreateForm" in app
    assert "FindingsPanel" in app
    assert "ImpactPanel" in app
    assert "PatchPanel" in app
    assert "AuditPanel" in app
    for banned in ("hero", "Hero", "landing", "pricing", "开始试用"):
        assert banned not in app, f"App.tsx 不应包含营销页元素：{banned}"


def test_dashboard_renders_every_required_surface() -> None:
    task_view = _read(WEB / "src" / "components" / "TaskView.tsx")
    common = _read(WEB / "src" / "components" / "Common.tsx")
    create_form = _read(WEB / "src" / "components" / "CreateForm.tsx")
    # 状态 / 模式 / 时间线 / findings / 影响 / patch / verify 证据 / 审批 / 审计
    assert "mode" in task_view, "任务头必须展示运行模式"
    assert "Timeline" in common and "TaskTimeline" in task_view
    assert "FindingsPanel" in task_view
    assert "ImpactPanel" in task_view
    assert "PatchPanel" in task_view and "验证结果" in task_view
    assert "approval" in _read(WEB / "src" / "api.ts")
    assert "AuditPanel" in common or "AuditPanel" in _read(WEB / "src" / "components" / "AuditPanel.tsx")
    # single / a2a / offline 三种模式都必须可选。
    for mode in ('"single"', '"a2a"', '"offline"'):
        assert mode in create_form, f"创建表单必须支持模式 {mode}"


def test_dashboard_polls_until_terminal_state() -> None:
    app = _read(WEB / "src" / "App.tsx")
    assert "setInterval" in app and "clearInterval" in app
    assert "TERMINAL" in app, "必须能识别终态并停止轮询"
    assert "2000" in app, "非终态任务需要轮询刷新"


def test_dashboard_surfaces_api_errors() -> None:
    api = _read(WEB / "src" / "api.ts")
    app = _read(WEB / "src" / "App.tsx")
    assert "class ApiError" in api and "payload.code" in api
    assert "ErrorBanner" in app, "API 错误必须渲染到界面上"


# --- 前后端契约一致性 -------------------------------------------------------


def test_frontend_paths_all_exist_in_backend_openapi() -> None:
    used = web_check.frontend_paths()
    declared = {re.sub(r"\{[^}]+\}", "{param}", path) for path in web_check.backend_paths()}
    unknown = sorted(path for path in used if path not in declared)
    assert unknown == [], f"前端调用了不存在的后端路径：{unknown}"


def test_dashboard_covers_required_capabilities() -> None:
    _, declared = web_check.check_contract()
    assert declared, "后端 OpenAPI 不应为空"


def test_dashboard_write_requests_carry_identity_and_idempotency() -> None:
    web_check.check_idempotency_headers()
    api = _read(WEB / "src" / "api.ts")
    # 按方法切分客户端类体，每个含 POST 的方法都必须以 write=true 调用 request。
    chunks = re.split(r"\n  (?:private |async )?[a-zA-Z]+\(", api)
    posting = [chunk for chunk in chunks if 'method: "POST"' in chunk]
    assert len(posting) >= 5, f"预期至少 5 个写方法，实际 {len(posting)}"
    for chunk in posting:
        name = chunk.split(")")[0][:40]
        assert re.search(r",\s*true,", chunk), f"POST 方法未以 write=true 调用：{name}"


def test_dashboard_feature_contract_is_satisfied() -> None:
    """23 项界面能力契约（transport / ZIP 限制 / NEEDS_HUMAN / 按钮门禁 ...）。"""
    features = web_check.check_feature_contract()
    assert len(features) >= 20
    assert "系统状态展示" in features


def test_dashboard_explains_system_status_without_exposing_transport_jargon() -> None:
    app = _read(WEB / "src" / "App.tsx")
    assert "系统状态：可用" in app
    assert "HTTP 协作通道" not in app


def test_dashboard_reuses_idempotency_key_for_retries() -> None:
    api = _read(WEB / "src" / "api.ts")
    app = _read(WEB / "src" / "App.tsx")
    assert "class OperationKeys" in api
    assert "keyFor(action)" in api and "clear(action)" in api
    # 成功后才清空；失败路径必须保留键（注释与代码都要能看出来）。
    assert "keys.clear(action)" in app
    assert "keys.keyFor(action)" in app
    assert "失败时保留幂等键" in app


def test_zip_upload_is_size_limited_in_frontend() -> None:
    form = _read(WEB / "src" / "components" / "CreateForm.tsx")
    assert "MAX_ZIP_BYTES" in form and "MAX_ZIP_BASE64_CHARS" in form
    assert "file.size > MAX_ZIP_BYTES" in form, "必须在上传前检查文件大小"
    assert "超过前端上限" in form


def test_dashboard_can_review_a_local_python_file_and_explain_a2a() -> None:
    form = _read(WEB / "src" / "components" / "CreateForm.tsx")
    timeline = _read(WEB / "src" / "components" / "Common.tsx")
    input_helpers = _read(WEB / "src" / "review-input.ts")
    assert 'accept: ".py,.zip,text/x-python,application/x-python,application/zip"' in form
    assert "onPythonFile" in form and "buildPythonFileDiff" in form
    assert "A2A 多 Agent 协作" in form and "代码审查 Agent" in form
    assert "A2A 协作过程" in timeline
    assert "review-agent" in timeline and "impact-agent" in timeline
    assert "DEMO_CASES" in input_helpers
    assert all(case in input_helpers for case in ('"secret-shell"', '"sql-concat"', '"clean-python"'))


def test_dashboard_gates_write_buttons_by_role_and_status() -> None:
    view = _read(WEB / "src" / "components" / "TaskView.tsx")
    app = _read(WEB / "src" / "App.tsx")
    assert 'patchStatus === "pending_approval"' in view, "审批按钮必须绑定补丁状态"
    assert 'patchStatus === "approved"' in view, "合并按钮必须绑定审批通过状态"
    assert 'canFix: status === "REVIEWED"' in app, "生成补丁按钮必须绑定 REVIEWED 状态"
    assert "ResumePanel" in view and 'detail.task.status !== "NEEDS_HUMAN"' in view


@pytest.mark.skipif(
    (ROOT / "web" / "node_modules").exists() is False,
    reason="未安装 web/node_modules，跳过真实前端行为测试",
)
def test_operation_keys_behavior_with_real_node(tmp_path: Path) -> None:
    """用真实 Node 执行 api.ts 的 OperationKeys：同键复用、成功后清空。"""
    import shutil

    node = shutil.which("node")
    esbuild = WEB / "node_modules" / "esbuild" / "bin" / "esbuild"
    if node is None or not esbuild.exists():
        pytest.skip("node 或 esbuild 不可用")
    bundle = tmp_path / "api.mjs"
    built = subprocess.run(
        [node, str(esbuild), str(WEB / "src" / "api.ts"), "--bundle", "--format=esm",
         f"--outfile={bundle}"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert built.returncode == 0, built.stderr
    script = tmp_path / "check.mjs"
    script.write_text(
        "\n".join(
            [
                f'import {{ OperationKeys, newIdempotencyKey }} from "{bundle.as_uri()}";',
                "const keys = new OperationKeys();",
                'const first = keys.keyFor("fix:task-1");',
                'const again = keys.keyFor("fix:task-1");',
                'if (first !== again) throw new Error("同一操作必须复用同一个幂等键");',
                'const other = keys.keyFor("merge:task-1");',
                'if (other === first) throw new Error("不同操作必须使用不同幂等键");',
                'keys.clear("fix:task-1");',
                'const third = keys.keyFor("fix:task-1");',
                'if (third === first) throw new Error("成功后必须生成新键");',
                'if (keys.peek("fix:task-1") !== third) throw new Error("peek 必须返回当前键");',
                'keys.clearAll();',
                'if (keys.peek("fix:task-1") !== null) throw new Error("clearAll 必须清空全部键");',
                'const a = newIdempotencyKey(); const b = newIdempotencyKey();',
                'if (a === b) throw new Error("自动生成的键必须唯一");',
                'if (a.length < 8) throw new Error("幂等键长度必须 >= 8");',
                'console.log("OPERATION_KEYS_OK");',
            ]
        ),
        encoding="utf-8",
    )
    run = subprocess.run(
        [node, str(script)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "OPERATION_KEYS_OK" in run.stdout


@pytest.mark.skipif(
    (ROOT / "web" / "node_modules").exists() is False,
    reason="未安装 web/node_modules，跳过真实前端行为测试",
)
def test_python_file_input_is_converted_to_safe_unified_diff(tmp_path: Path) -> None:
    """真实执行浏览器侧转换逻辑，覆盖 Windows 文件名、换行、非法路径和演示用例。"""
    import shutil

    node = shutil.which("node")
    esbuild = WEB / "node_modules" / "esbuild" / "bin" / "esbuild"
    if node is None or not esbuild.exists():
        pytest.skip("node 或 esbuild 不可用")
    bundle = tmp_path / "review-input.mjs"
    built = subprocess.run(
        [
            node,
            str(esbuild),
            str(WEB / "src" / "review-input.ts"),
            "--bundle",
            "--format=esm",
            f"--outfile={bundle}",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert built.returncode == 0, built.stderr
    script = tmp_path / "check-python-input.mjs"
    script.write_text(
        "\n".join(
            [
                f'import {{ buildPythonFileDiff, DEMO_CASES }} from "{bundle.as_uri()}";',
                'const diff = buildPythonFileDiff("app\\\\config.py", "API_KEY = \'demo\'\\r\\nprint(API_KEY)\\r\\n");',
                'if (!diff.includes("diff --git a/app/config.py b/app/config.py")) throw new Error("路径未规范化");',
                'if (!diff.includes("@@ -0,0 +1,2 @@")) throw new Error("hunk 行数错误");',
                'if (diff.includes("\\r")) throw new Error("未统一换行符");',
                'for (const bad of ["../escape.py", "C:\\\\work\\\\bad.py", "app/not-python.txt"]) {',
                '  let rejected = false; try { buildPythonFileDiff(bad, "print(1)"); } catch { rejected = true; }',
                '  if (!rejected) throw new Error(`未拒绝非法路径 ${bad}`);',
                '}',
                'let emptyRejected = false; try { buildPythonFileDiff("app/empty.py", "   "); } catch { emptyRejected = true; }',
                'if (!emptyRejected) throw new Error("未拒绝空文件");',
                'if (DEMO_CASES.length !== 3) throw new Error("演示用例数量不正确");',
                'console.log("PYTHON_FILE_INPUT_OK");',
            ]
        ),
        encoding="utf-8",
    )
    run = subprocess.run(
        [node, str(script)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "PYTHON_FILE_INPUT_OK" in run.stdout



def test_proxy_prefixes_cover_dev_and_production() -> None:
    proxies = web_check.check_proxies()
    assert proxies["vite"] == web_check.PROXY_PREFIXES
    assert proxies["nginx"] == web_check.PROXY_PREFIXES


# --- 容器化与文档 -----------------------------------------------------------


def test_web_service_is_declared_in_compose_and_proxies_the_api() -> None:
    compose = _read(ROOT / "docker-compose.yml")
    assert "docker/web/Dockerfile" in compose
    assert '"8080:80"' in compose, "Dashboard 端口必须被明确声明"
    dockerfile = _read(ROOT / "docker" / "web" / "Dockerfile")
    assert "npm run build" in dockerfile
    assert "nginx" in dockerfile
    nginx = _read(ROOT / "docker" / "web" / "nginx.conf")
    # 必须能在 api 容器重建（IP 变化）后重新解析，否则 nginx 会一直 502。
    assert "resolver 127.0.0.11" in nginx, "nginx 必须使用 Docker 内置 DNS 做请求时解析"
    assert "set $codepilot_api http://api:8099;" in nginx
    assert "proxy_pass $codepilot_api;" in nginx
    assert "proxy_buffering off" in nginx, "SSE 流必须关闭代理缓冲"


@pytest.mark.skipif(
    (ROOT / "web" / "node_modules").exists() is False,
    reason="未安装 web/node_modules，跳过真实构建",
)
def test_dashboard_builds_for_real() -> None:
    report = web_check.run_build()
    assert report["js_bytes"] > 10_000, "构建产物体积异常，可能是空壳页面"
    assert report["asset_count"] >= 2, "至少应有 JS 与 CSS 产物"


def test_readme_documents_dashboard_startup() -> None:
    readme = _read(ROOT / "README.md")
    assert "Dashboard" in readme or "工作台" in readme
    assert "npm install" in readme and "npm run dev" in readme
    assert "8080" in readme, "README 必须给出 Dashboard 的访问地址"
    for command in ("docker compose up -d postgres api web",):
        assert command in readme, f"README 缺少可执行命令：{command}"


def test_compose_config_is_valid() -> None:
    completed = subprocess.run(
        ["docker", "compose", "config", "--quiet"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0 and "docker" in (completed.stderr or "").lower():
        pytest.skip(f"Docker 不可用：{completed.stderr.strip()[:200]}")
    assert completed.returncode == 0, completed.stderr


def test_web_check_reports_json_for_ci() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/web_check.py", "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["headers"] == "ok"
