/** 创建审查任务表单：让首次使用者从“放入代码”开始。 */

import React from "react";

import {
  buildPythonFileDiff,
  DEMO_CASES,
  MAX_DIFF_CHARS,
} from "../review-input";

export interface CreateFormValues {
  inputType: "diff" | "zip";
  content: string;
  baseCommit: string;
  contextPolicy: string;
  mode: string;
}

type InputMode = "diff" | "zip" | "python";

/** 文件原始大小最多 100 MiB；ZIP 编码和单文件 diff 封装后的请求由网关放行至 220 MiB。 */
export const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;
export const MAX_ZIP_BYTES = MAX_UPLOAD_BYTES;
export const MAX_ZIP_BASE64_CHARS = Math.ceil((MAX_ZIP_BYTES / 3) * 4);
export { MAX_DIFF_CHARS, MAX_PY_FILE_BYTES } from "../review-input";

const DEFAULT_BASE_COMMIT = "本次上传内容";

function sizeLabel(bytes: number): string {
  return `${(bytes / 1024 / 1024).toFixed(bytes >= 1024 * 1024 ? 1 : 2)} MB`;
}

async function encodeBase64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  return btoa(binary);
}

const CONTEXT_HINT: Record<string, string> = {
  minimal: "只看这次改动的代码行。速度最快，但上下文最少。",
  function: "额外读取改动所在函数，通常足以判断问题，建议保持此选项。",
  module: "额外读取改动所在的整个 Python 文件。适合需要理解更多关联逻辑的改动。",
};

export function CreateForm({
  onSubmit,
  busy,
}: {
  onSubmit: (values: CreateFormValues) => void;
  busy: boolean;
}): React.ReactElement {
  const [inputMode, setInputMode] = React.useState<InputMode>("python");
  const [content, setContent] = React.useState("");
  const [pythonPath, setPythonPath] = React.useState("");
  const [pythonSource, setPythonSource] = React.useState("");
  const [baseCommit, setBaseCommit] = React.useState(DEFAULT_BASE_COMMIT);
  const [contextPolicy, setContextPolicy] = React.useState("function");
  const [mode, setMode] = React.useState("a2a");
  const [formError, setFormError] = React.useState<string | null>(null);
  const [fileInfo, setFileInfo] = React.useState<string | null>(null);
  const [dragging, setDragging] = React.useState(false);
  const fileInputRef = React.useRef<HTMLInputElement>(null);

  const clearForm = () => {
    setInputMode("python");
    setContent("");
    setPythonPath("");
    setPythonSource("");
    setBaseCommit(DEFAULT_BASE_COMMIT);
    setContextPolicy("function");
    setMode("a2a");
    setFormError(null);
    setFileInfo(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const receiveFile = async (file: File) => {
    const lowerName = file.name.toLowerCase();
    if (!lowerName.endsWith(".py") && !lowerName.endsWith(".zip")) {
      setFormError("请放入 .py 文件或 Python 项目 .zip 文件。");
      setFileInfo(null);
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES || (lowerName.endsWith(".zip") && file.size > MAX_ZIP_BYTES)) {
      setFormError(`文件 ${sizeLabel(file.size)} 超过前端上限 100 MB，请拆分后再提交。`);
      setFileInfo(null);
      return;
    }

    try {
      if (lowerName.endsWith(".zip")) {
        const encoded = await encodeBase64(file);
        if (encoded.length > MAX_ZIP_BASE64_CHARS) throw new Error("ZIP 编码后超过可提交大小，请拆分后再试。");
        setInputMode("zip");
        setContent(encoded);
        setPythonPath("");
        setPythonSource("");
        setFileInfo(`${file.name} · ${sizeLabel(file.size)} · 已准备好审查项目中的 Python 文件`);
      } else {
        const source = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
        if (!source.trim()) throw new Error("Python 文件内容不能为空。");
        setInputMode("python");
        setPythonPath(file.name);
        setPythonSource(source);
        setContent("");
        setFileInfo(`${file.name} · ${sizeLabel(file.size)} · 已准备好审查`);
      }
      setFormError(null);
    } catch (error) {
      setFormError(error instanceof Error ? error.message : "读取文件失败，请使用 UTF-8 编码后重试。");
      setFileInfo(null);
    }
  };

  const onPythonFile = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (file) await receiveFile(file);
  };

  const onDrop = async (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    const file = event.dataTransfer.files?.[0];
    if (file) await receiveFile(file);
  };

  const applyDemo = (id: string) => {
    const selected = DEMO_CASES.find((item) => item.id === id);
    if (!selected) return;
    setInputMode("diff");
    setContent(selected.diff);
    setPythonPath("");
    setPythonSource("");
    setFormError(null);
    setFileInfo(`已载入演示用例：${selected.label}`);
  };

  const validate = (): string | null => {
    if (inputMode === "python") {
      try {
        buildPythonFileDiff(pythonPath, pythonSource);
      } catch (error) {
        return error instanceof Error ? error.message : "Python 文件无法转换为审查输入。";
      }
      return null;
    }
    if (!content.trim()) return inputMode === "zip" ? "请先拖入或选择 ZIP 文件。" : "请粘贴要审查的代码改动。";
    if (inputMode === "diff" && content.length > MAX_DIFF_CHARS) {
      return `代码改动文本超过 ${sizeLabel(MAX_DIFF_CHARS)} 上限，请拆分后再提交。`;
    }
    if (inputMode === "zip" && content.length > MAX_ZIP_BASE64_CHARS) return "ZIP 文件超过 100 MB 上限。";
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
        // 后端字段名为 base_commit；界面把它称作版本备注，避免让新用户误以为会读取本机 Git。
        onSubmit({
          inputType: inputMode === "zip" ? "zip" : "diff",
          content: submittedContent,
          baseCommit: baseCommit.trim() || DEFAULT_BASE_COMMIT,
          contextPolicy,
          mode,
        });
      },
    },
    React.createElement("h2", null, "1. 放入要审查的代码"),
    React.createElement("div", { className: "hint" }, "把文件拖到下方，或点击选择文件。支持 .py 和 Python 项目 .zip，单个文件最大 100 MB。"),
    React.createElement(
      "div",
      {
        className: `drop-zone${dragging ? " dragging" : ""}`,
        onDragOver: (event: React.DragEvent<HTMLDivElement>) => {
          event.preventDefault();
          setDragging(true);
        },
        onDragLeave: () => setDragging(false),
        onDrop,
      },
      React.createElement("strong", null, "拖入 .py 或 .zip 文件"),
      React.createElement("span", { className: "muted" }, "也可以从此电脑选择文件"),
      React.createElement("button", { type: "button", onClick: () => fileInputRef.current?.click() }, "选择本机文件"),
      React.createElement("input", {
        ref: fileInputRef,
        className: "visually-hidden",
        type: "file",
        accept: ".py,.zip,text/x-python,application/x-python,application/zip",
        onChange: onPythonFile,
      }),
    ),
    fileInfo ? React.createElement("div", { className: "file-ready" }, fileInfo) : null,
    React.createElement("label", null, "没有文件时，可选择其他输入方式"),
    React.createElement(
      "select",
      {
        value: inputMode,
        onChange: (event: React.ChangeEvent<HTMLSelectElement>) => {
          setInputMode(event.target.value as InputMode);
          setFormError(null);
        },
      },
      React.createElement("option", { value: "python" }, "本机 Python 文件（推荐）"),
      React.createElement("option", { value: "zip" }, "Python 项目 ZIP"),
      React.createElement("option", { value: "diff" }, "粘贴代码改动（Diff）"),
    ),
    inputMode === "diff"
      ? React.createElement(
          React.Fragment,
          null,
          React.createElement("label", null, "粘贴代码改动"),
          React.createElement("textarea", {
            value: content,
            onChange: (event: React.ChangeEvent<HTMLTextAreaElement>) => setContent(event.target.value),
            placeholder: "粘贴 git diff 内容，例如以 diff --git 开头的代码改动。",
            spellCheck: false,
          }),
        )
      : null,
    React.createElement("label", null, "没有代码时，先试一个演示用例"),
    React.createElement(
      "select",
      { defaultValue: "", onChange: (event: React.ChangeEvent<HTMLSelectElement>) => applyDemo(event.target.value) },
      React.createElement("option", { value: "", disabled: true }, "选择一个示例后即可创建审查"),
      ...DEMO_CASES.map((item) => React.createElement("option", { key: item.id, value: item.id }, item.label)),
    ),
    React.createElement("h2", { style: { marginTop: 18 } }, "2. 选择审查方式"),
    React.createElement("label", null, "审查方式"),
    React.createElement(
      "select",
      { value: mode, onChange: (event: React.ChangeEvent<HTMLSelectElement>) => setMode(event.target.value) },
      React.createElement("option", { value: "a2a" }, "多 Agent 协作审查（推荐）"),
      React.createElement("option", { value: "single" }, "单 Agent 快速审查"),
      React.createElement("option", { value: "offline" }, "本地规则扫描"),
    ),
    mode === "a2a"
      ? React.createElement(
          "div",
          { className: "a2a-callout" },
          React.createElement("strong", null, "A2A 多 Agent 协作审查会做什么？"),
          React.createElement("div", null, "代码审查 Agent 找问题，影响分析 Agent 判断改动会影响哪些地方；完成后系统汇总结果。生成修复建议时，修复 Agent 和验证 Agent 会继续接力。当前本地演示不需要填写 API Key。"),
        )
      : null,
    mode === "offline"
      ? React.createElement("div", { className: "hint" }, "本地规则扫描只运行固定检查，不使用多 Agent，也不会生成修复建议。")
      : null,
    React.createElement(
      "details",
      { className: "technical-details" },
      React.createElement("summary", null, "高级设置（一般不用改）"),
      React.createElement("label", null, "审查时额外读取多少相关代码"),
      React.createElement(
        "select",
        { value: contextPolicy, onChange: (event: React.ChangeEvent<HTMLSelectElement>) => setContextPolicy(event.target.value) },
        React.createElement("option", { value: "function" }, "读取改动所在函数（推荐）"),
        React.createElement("option", { value: "minimal" }, "只读取改动代码（更快）"),
        React.createElement("option", { value: "module" }, "读取改动所在整个文件（更完整）"),
      ),
      React.createElement("div", { className: "hint" }, CONTEXT_HINT[contextPolicy]),
      React.createElement("label", null, "本次审查的版本备注（可不填）"),
      React.createElement("input", {
        value: baseCommit,
        onChange: (event: React.ChangeEvent<HTMLInputElement>) => setBaseCommit(event.target.value),
        placeholder: DEFAULT_BASE_COMMIT,
      }),
      React.createElement("div", { className: "hint" }, "这里只是给历史记录看的版本说明。上传本机文件时保持默认即可；它不会读取或对比你电脑上的 Git 仓库。"),
    ),
    formError ? React.createElement("div", { className: "error-box" }, formError) : null,
    React.createElement("h2", { style: { marginTop: 18 } }, "3. 创建审查并查看结果"),
    React.createElement(
      "div",
      { className: "row", style: { marginTop: 10 } },
      React.createElement("button", { className: "primary", type: "submit", disabled: busy }, busy ? "正在创建…" : "创建审查任务"),
      React.createElement("button", { type: "button", disabled: busy, onClick: clearForm }, "清空，准备另一份代码"),
    ),
    React.createElement("div", { className: "hint" }, "创建后，右侧会自动显示协作过程和审查结论。"),
  );
}
