/** 创建审查任务表单：支持 unified diff 与 base64(ZIP)，并选择运行模式。 */

import React from "react";

export interface CreateFormValues {
  inputType: "diff" | "zip";
  content: string;
  baseCommit: string;
  contextPolicy: string;
  mode: string;
}

/** 前端限制：ZIP 原文件大小与 base64 后的文本长度（后端另有解压体积与条目数限制）。 */
export const MAX_ZIP_BYTES = 5 * 1024 * 1024;
export const MAX_ZIP_BASE64_CHARS = 7 * 1024 * 1024;
/** diff 文本长度上限（后端要求非空且为合法 unified diff）。 */
export const MAX_DIFF_CHARS = 1024 * 1024;

const SAMPLE_DIFF = [
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
].join("\n");

export function CreateForm({
  onSubmit,
  busy,
}: {
  onSubmit: (values: CreateFormValues) => void;
  busy: boolean;
}): React.ReactElement {
  const [inputType, setInputType] = React.useState<"diff" | "zip">("diff");
  const [content, setContent] = React.useState(SAMPLE_DIFF);
  const [baseCommit, setBaseCommit] = React.useState("synthetic-base-001");
  const [contextPolicy, setContextPolicy] = React.useState("function");
  const [mode, setMode] = React.useState("a2a");
  const [formError, setFormError] = React.useState<string | null>(null);
  const [fileInfo, setFileInfo] = React.useState<string | null>(null);

  const onFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    if (file.size > MAX_ZIP_BYTES) {
      setFormError(
        `ZIP 文件 ${(file.size / 1024 / 1024).toFixed(2)} MB 超过前端上限 ${(MAX_ZIP_BYTES / 1024 / 1024).toFixed(0)} MB，请拆分后再提交`,
      );
      setFileInfo(null);
      return;
    }
    const buffer = await file.arrayBuffer();
    const bytes = new Uint8Array(buffer);
    let binary = "";
    bytes.forEach((byte) => {
      binary += String.fromCharCode(byte);
    });
    const encoded = btoa(binary);
    if (encoded.length > MAX_ZIP_BASE64_CHARS) {
      setFormError("base64 编码后超过上限，请拆分后再提交");
      setFileInfo(null);
      return;
    }
    setContent(encoded);
    setInputType("zip");
    setFormError(null);
    setFileInfo(`${file.name} · ${(file.size / 1024).toFixed(1)} KB → base64 ${encoded.length} 字符`);
  };

  const validate = (): string | null => {
    if (!content.trim()) return "内容不能为空";
    if (inputType === "diff" && content.length > MAX_DIFF_CHARS) {
      return `diff 文本 ${content.length} 字符超过上限 ${MAX_DIFF_CHARS}`;
    }
    if (inputType === "zip" && content.length > MAX_ZIP_BASE64_CHARS) {
      return "base64(ZIP) 超过上限";
    }
    if (!baseCommit.trim()) return "base_commit 不能为空";
    return null;
  };

  return React.createElement(
    "form",
    {
      className: "panel",
      onSubmit: (event: React.FormEvent) => {
        event.preventDefault();
        const problem = validate();
        if (problem) {
          setFormError(problem);
          return;
        }
        setFormError(null);
        onSubmit({ inputType, content, baseCommit, contextPolicy, mode });
      },
    },
    React.createElement("h2", null, "创建审查任务"),
    React.createElement("label", null, "输入类型"),
    React.createElement(
      "select",
      {
        value: inputType,
        onChange: (e: React.ChangeEvent<HTMLSelectElement>) =>
          setInputType(e.target.value as "diff" | "zip"),
      },
      React.createElement("option", { value: "diff" }, "unified diff"),
      React.createElement("option", { value: "zip" }, "base64(ZIP)"),
    ),
    React.createElement("label", null, "运行模式（single / a2a / offline）"),
    React.createElement(
      "select",
      {
        value: mode,
        onChange: (e: React.ChangeEvent<HTMLSelectElement>) => setMode(e.target.value),
      },
      React.createElement("option", { value: "a2a" }, "a2a"),
      React.createElement("option", { value: "single" }, "single"),
      React.createElement("option", { value: "offline" }, "offline"),
    ),
    mode === "offline"
      ? React.createElement(
          "div",
          { className: "hint" },
          "offline 只运行确定性规则与 AST，不调用模型，也不生成补丁。",
        )
      : null,
    React.createElement("label", null, "base_commit"),
    React.createElement("input", {
      value: baseCommit,
      onChange: (e: React.ChangeEvent<HTMLInputElement>) => setBaseCommit(e.target.value),
    }),
    React.createElement("label", null, "上下文策略"),
    React.createElement(
      "select",
      {
        value: contextPolicy,
        onChange: (e: React.ChangeEvent<HTMLSelectElement>) => setContextPolicy(e.target.value),
      },
      React.createElement("option", { value: "function" }, "function"),
      React.createElement("option", { value: "minimal" }, "minimal"),
      React.createElement("option", { value: "module" }, "module"),
    ),
    React.createElement("label", null, "Diff 内容 / 选择 ZIP 文件"),
    React.createElement("textarea", {
      value: content,
      onChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => setContent(e.target.value),
      spellCheck: false,
    }),
    React.createElement("input", { type: "file", accept: ".zip", onChange: onFile }),
    fileInfo ? React.createElement("div", { className: "hint mono" }, fileInfo) : null,
    React.createElement(
      "div",
      { className: "hint" },
      `ZIP 前端上限 ${(MAX_ZIP_BYTES / 1024 / 1024).toFixed(0)} MB（解压体积与条目数由后端限制）`,
    ),
    formError ? React.createElement("div", { className: "error-box" }, formError) : null,
    React.createElement(
      "div",
      { className: "row", style: { marginTop: 10 } },
      React.createElement(
        "button",
        { className: "primary", type: "submit", disabled: busy },
        busy ? "提交中…" : "创建任务",
      ),
      React.createElement(
        "span",
        { className: "hint" },
        "写请求自动携带 Idempotency-Key；重复提交（同一次操作的重试）返回原任务。",
      ),
    ),
  );
}
