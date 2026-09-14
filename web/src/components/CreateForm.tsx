/** 创建审查任务表单：支持 diff、项目 ZIP 与本机单个 Python 文件。 */

import React from "react";

import {
  buildPythonFileDiff,
  DEMO_CASES,
  MAX_DIFF_CHARS,
  MAX_PY_FILE_BYTES,
} from "../review-input";

export interface CreateFormValues {
  inputType: "diff" | "zip";
  content: string;
  baseCommit: string;
  contextPolicy: string;
  mode: string;
}

type InputMode = "diff" | "zip" | "python";

/** 前端限制：ZIP 原文件大小与 base64 后的文本长度（后端另有解压体积与条目数限制）。 */
export const MAX_ZIP_BYTES = 5 * 1024 * 1024;
export const MAX_ZIP_BASE64_CHARS = 7 * 1024 * 1024;
export { MAX_DIFF_CHARS, MAX_PY_FILE_BYTES } from "../review-input";

const DEFAULT_DIFF = DEMO_CASES[0].diff;

async function encodeBase64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  return btoa(binary);
}

export function CreateForm({
  onSubmit,
  busy,
}: {
  onSubmit: (values: CreateFormValues) => void;
  busy: boolean;
}): React.ReactElement {
  const [inputMode, setInputMode] = React.useState<InputMode>("diff");
  const [content, setContent] = React.useState(DEFAULT_DIFF);
  const [pythonPath, setPythonPath] = React.useState("app/review_target.py");
  const [pythonSource, setPythonSource] = React.useState("");
  const [baseCommit, setBaseCommit] = React.useState("synthetic-base-001");
  const [contextPolicy, setContextPolicy] = React.useState("function");
  const [mode, setMode] = React.useState("a2a");
  const [formError, setFormError] = React.useState<string | null>(null);
  const [fileInfo, setFileInfo] = React.useState<string | null>(null);

  const onZipFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    if (file.size > MAX_ZIP_BYTES) {
      setFormError(
        `ZIP 文件 ${(file.size / 1024 / 1024).toFixed(2)} MB 超过前端上限 ${(MAX_ZIP_BYTES / 1024 / 1024).toFixed(0)} MB，请拆分后再提交`,
      );
      setFileInfo(null);
      return;
    }
    try {
      const encoded = await encodeBase64(file);
      if (encoded.length > MAX_ZIP_BASE64_CHARS) {
        setFormError("base64 编码后超过上限，请拆分后再提交");
        setFileInfo(null);
        return;
      }
      setContent(encoded);
      setFormError(null);
      setFileInfo(`${file.name} · ${(file.size / 1024).toFixed(1)} KB → base64 ${encoded.length} 字符`);
    } catch {
      setFormError("读取 ZIP 文件失败，请重新选择文件");
      setFileInfo(null);
    }
  };

  const onPythonFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    if (!file.name.toLowerCase().endsWith(".py")) {
      setFormError("只能选择 .py 文件");
      setFileInfo(null);
      return;
    }
    if (file.size > MAX_PY_FILE_BYTES) {
      setFormError(`Python 文件超过前端上限 ${(MAX_PY_FILE_BYTES / 1024).toFixed(0)} KB，请改用 ZIP 项目包`);
      setFileInfo(null);
      return;
    }
    try {
      const source = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
      if (!source.trim()) throw new Error("Python 文件内容不能为空");
      setPythonPath(file.name);
      setPythonSource(source);
      setFormError(null);
      setFileInfo(`${file.name} · ${(file.size / 1024).toFixed(1)} KB · 浏览器内转换为新增文件 diff`);
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "读取 Python 文件失败，请使用 UTF-8 编码");
      setFileInfo(null);
    }
  };

  const applyDemo = (id: string) => {
    const selected = DEMO_CASES.find((item) => item.id === id);
    if (!selected) return;
    setInputMode("diff");
    setContent(selected.diff);
    setFormError(null);
    setFileInfo(`已载入演示用例：${selected.label}`);
  };

  const validate = (): string | null => {
    if (!baseCommit.trim()) return "base_commit 不能为空";
    if (inputMode === "python") {
      try {
        buildPythonFileDiff(pythonPath, pythonSource);
      } catch (error) {
        return error instanceof Error ? error.message : "Python 文件无法转换为审查输入";
      }
      return null;
    }
    if (!content.trim()) return "内容不能为空";
    if (inputMode === "diff" && content.length > MAX_DIFF_CHARS) {
      return `diff 文本 ${content.length} 字符超过上限 ${MAX_DIFF_CHARS}`;
    }
    if (inputMode === "zip" && content.length > MAX_ZIP_BASE64_CHARS) return "base64(ZIP) 超过上限";
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
        const submittedContent = inputMode === "python" ? buildPythonFileDiff(pythonPath, pythonSource) : content;
        setFormError(null);
        onSubmit({
          inputType: inputMode === "zip" ? "zip" : "diff",
          content: submittedContent,
          baseCommit,
          contextPolicy,
          mode,
        });
      },
    },
    React.createElement("h2", null, "1. 选择要审查的代码"),
    React.createElement(
      "div",
      { className: "hint" },
      "选择一个本机 .py 文件即可开始；也可以提交多个文件组成的项目 ZIP，或粘贴 Git diff。",
    ),
    React.createElement("label", null, "代码来源"),
    React.createElement(
      "select",
      {
        value: inputMode,
        onChange: (e: React.ChangeEvent<HTMLSelectElement>) => {
          setInputMode(e.target.value as InputMode);
          setFormError(null);
        },
      },
      React.createElement("option", { value: "diff" }, "粘贴代码改动（Diff）"),
      React.createElement("option", { value: "zip" }, "Python 项目 ZIP"),
      React.createElement("option", { value: "python" }, "本机 Python 文件（.py）"),
    ),
    React.createElement("label", null, "没有代码时，先试一个演示用例"),
    React.createElement(
      "select",
      { defaultValue: "", onChange: (e: React.ChangeEvent<HTMLSelectElement>) => applyDemo(e.target.value) },
      React.createElement("option", { value: "", disabled: true }, "选择后直接载入可审查的 diff"),
      ...DEMO_CASES.map((item) => React.createElement("option", { key: item.id, value: item.id }, item.label)),
    ),
    React.createElement("h2", { style: { marginTop: 18 } }, "2. 选择审查方式"),
    React.createElement("label", null, "协作方式"),
    React.createElement(
      "select",
      { value: mode, onChange: (e: React.ChangeEvent<HTMLSelectElement>) => setMode(e.target.value) },
      React.createElement("option", { value: "a2a" }, "A2A 多 Agent 协作（推荐）"),
      React.createElement("option", { value: "single" }, "单 Agent 快速审查"),
      React.createElement("option", { value: "offline" }, "离线规则扫描"),
    ),
    mode === "a2a"
      ? React.createElement(
          "div",
          { className: "a2a-callout" },
          React.createElement("strong", null, "A2A 多 Agent 协作会做什么？"),
          React.createElement(
            "div",
            null,
            "代码审查 Agent 负责找问题，影响分析 Agent 负责判断改动范围；两者分别完成任务后，系统汇总结果。后续生成补丁时，修复 Agent 和验证 Agent 还会继续接力。",
          ),
        )
      : null,
    mode === "offline"
      ? React.createElement("div", { className: "hint" }, "规则扫描只运行本地确定性检查，不进行多 Agent 协作，也不生成修复建议。")
      : null,
    React.createElement("label", null, "对比基线（不确定时保持默认）"),
    React.createElement("input", {
      value: baseCommit,
      onChange: (e: React.ChangeEvent<HTMLInputElement>) => setBaseCommit(e.target.value),
    }),
    React.createElement("label", null, "阅读代码范围"),
    React.createElement(
      "select",
      { value: contextPolicy, onChange: (e: React.ChangeEvent<HTMLSelectElement>) => setContextPolicy(e.target.value) },
      React.createElement("option", { value: "function" }, "改动附近的函数（推荐）"),
      React.createElement("option", { value: "minimal" }, "最少必要代码"),
      React.createElement("option", { value: "module" }, "相关的整个模块"),
    ),
    inputMode === "python"
      ? React.createElement(
          React.Fragment,
          null,
          React.createElement("label", null, "从此电脑选择 .py 文件"),
          React.createElement("input", {
            type: "file",
            accept: ".py,text/x-python,application/x-python",
            onChange: onPythonFile,
          }),
          React.createElement("label", null, "项目内相对路径（不会提交本机绝对路径）"),
          React.createElement("input", {
            value: pythonPath,
            onChange: (e: React.ChangeEvent<HTMLInputElement>) => setPythonPath(e.target.value),
            placeholder: "app/review_target.py",
          }),
          React.createElement(
            "div",
            { className: "hint" },
            `单文件上限 ${(MAX_PY_FILE_BYTES / 1024).toFixed(0)} KB，将作为新增文件进行审查；需要修复验证时请上传含 tests/ 的 ZIP 项目包。`,
          ),
        )
      : React.createElement(
          React.Fragment,
          null,
          React.createElement("label", null, inputMode === "zip" ? "选择 Python 项目 ZIP" : "Diff 内容"),
          inputMode === "diff"
            ? React.createElement("textarea", {
                value: content,
                onChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => setContent(e.target.value),
                spellCheck: false,
              })
            : React.createElement("input", { type: "file", accept: ".zip", onChange: onZipFile }),
          inputMode === "zip"
            ? React.createElement(
                "div",
                { className: "hint" },
                `ZIP 前端上限 ${(MAX_ZIP_BYTES / 1024 / 1024).toFixed(0)} MB（解压体积与条目数由后端限制）`,
              )
            : null,
        ),
    fileInfo ? React.createElement("div", { className: "hint mono" }, fileInfo) : null,
    formError ? React.createElement("div", { className: "error-box" }, formError) : null,
    React.createElement("h2", { style: { marginTop: 18 } }, "3. 创建并查看结果"),
    React.createElement(
      "div",
      { className: "row", style: { marginTop: 10 } },
      React.createElement("button", { className: "primary", type: "submit", disabled: busy }, busy ? "提交中…" : "创建任务"),
      React.createElement("span", { className: "hint" }, "创建后右侧会自动显示 A2A 协作进度和审查结论。"),
    ),
  );
}
