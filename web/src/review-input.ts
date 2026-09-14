/** 浏览器本地文件输入：把单个 Python 文件安全地转换成服务端支持的 unified diff。 */

/** 与服务端工作区限制保持一致；生成 unified diff 后可能因每行前缀而接近两倍。 */
export const MAX_PY_FILE_BYTES = 100 * 1024 * 1024;
export const MAX_DIFF_CHARS = 200 * 1024 * 1024;

function normalizePythonPath(rawPath: string): string {
  const path = rawPath.trim().replace(/\\/g, "/");
  if (!path) throw new Error("请输入项目内 Python 文件路径");
  if (path.startsWith("/") || path.startsWith("//") || /^[A-Za-z]:/.test(path)) {
    throw new Error("文件路径必须是项目内相对路径");
  }
  if (path.split("/").some((part) => part === ".." || part === "" || part === ".")) {
    throw new Error("文件路径不能包含空目录、. 或 ..");
  }
  if (!path.toLowerCase().endsWith(".py")) {
    throw new Error("只能审查 .py 文件");
  }
  return path;
}

/**
 * 单文件没有 Git 基线，因此在客户端表达为“新增文件”的标准 unified diff。
 * 服务端仍按普通 diff 审查，且不会收到浏览器真实磁盘路径。
 */
export function buildPythonFileDiff(path: string, source: string): string {
  const safePath = normalizePythonPath(path);
  const normalized = source.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  if (!normalized.trim()) throw new Error("Python 文件内容不能为空");

  const lines = normalized.split("\n");
  if (normalized.endsWith("\n")) lines.pop();
  if (lines.length === 0) throw new Error("Python 文件内容不能为空");

  const diff = [
    `diff --git a/${safePath} b/${safePath}`,
    "new file mode 100644",
    "--- /dev/null",
    `+++ b/${safePath}`,
    `@@ -0,0 +1,${lines.length} @@`,
    ...lines.map((line) => `+${line}`),
    "",
  ].join("\n");

  if (diff.length > MAX_DIFF_CHARS) {
    throw new Error(`生成的 diff 超过前端上限 ${MAX_DIFF_CHARS} 字符，请改用 ZIP 项目包`);
  }
  return diff;
}

export interface DemoCase {
  id: string;
  label: string;
  diff: string;
}

export const DEMO_CASES: readonly DemoCase[] = [
  {
    id: "secret-shell",
    label: "高危：硬编码密钥 + shell=True",
    diff: [
      "diff --git a/app/config.py b/app/config.py",
      "--- a/app/config.py",
      "+++ b/app/config.py",
      "@@ -1,3 +1,5 @@",
      " import os",
      " import subprocess",
      " ",
      '+API_KEY = "sk-live-abcdef123456"',
      '+subprocess.run("ls " + "/tmp", shell=True)',
      "",
    ].join("\n"),
  },
  {
    id: "sql-concat",
    label: "高危：SQL 字符串拼接",
    diff: [
      "diff --git a/app/users.py b/app/users.py",
      "--- a/app/users.py",
      "+++ b/app/users.py",
      "@@ -1,2 +1,3 @@",
      " def find_user(conn, user_id):",
      '-    return conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()',
      '+    query = "SELECT * FROM users WHERE id = " + user_id',
      '+    return conn.execute(query).fetchone()',
      "",
    ].join("\n"),
  },
  {
    id: "clean-python",
    label: "基线：无已知高危问题",
    diff: [
      "diff --git a/app/maths.py b/app/maths.py",
      "--- a/app/maths.py",
      "+++ b/app/maths.py",
      "@@ -0,0 +1,4 @@",
      "+def add(left: int, right: int) -> int:",
      "+    return left + right",
      "+",
      "+",
    ].join("\n"),
  },
];

export { normalizePythonPath };
