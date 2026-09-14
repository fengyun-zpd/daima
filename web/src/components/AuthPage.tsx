import React from "react";
import { ApiError, CodePilotClient, type Identity } from "../api";

export function AuthPage({ onAuthenticated }: { onAuthenticated: (identity: Identity) => void }): React.ReactElement {
  const [mode, setMode] = React.useState<"login" | "register">("login");
  const [account, setAccount] = React.useState("");
  const [employeeId, setEmployeeId] = React.useState("");
  const [username, setUsername] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [confirm, setConfirm] = React.useState("");
  const [error, setError] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError("");
    if (mode === "register" && password !== confirm) {
      setError("两次输入的密码不一致。");
      return;
    }
    setBusy(true);
    try {
      const client = new CodePilotClient();
      const result = mode === "login"
        ? await client.authLogin({ account: account.trim(), password })
        : await client.authRegister({ employee_id: employeeId.trim(), username: username.trim(), password });
      const identity: Identity = {
        actorId: result.user.employee_id,
        displayName: result.user.username,
        role: result.user.role,
        token: result.token,
      };
      window.localStorage.setItem("codepilot.identity", JSON.stringify(identity));
      onAuthenticated(identity);
    } catch (exc) {
      if (exc instanceof ApiError) setError(exc.payload.message);
      else setError("登录服务暂时不可用，请稍后重试。");
    } finally {
      setBusy(false);
    }
  };

  return React.createElement(
    "div",
    { className: "auth-page" },
    React.createElement("div", { className: "auth-card" },
      React.createElement("h1", null, "CodePilot 代码审查"),
      React.createElement("p", { className: "hint" }, "登录后管理审查任务、查看 A2A 协作过程和历史记录。"),
      React.createElement("div", { className: "auth-tabs" },
        React.createElement("button", { type: "button", className: mode === "login" ? "active" : "", onClick: () => setMode("login") }, "登录"),
        React.createElement("button", { type: "button", className: mode === "register" ? "active" : "", onClick: () => setMode("register") }, "注册新账号"),
      ),
      React.createElement("form", { onSubmit: submit },
        mode === "login"
          ? React.createElement(React.Fragment, null,
              React.createElement("label", null, "用户名或工号"),
              React.createElement("input", { value: account, required: true, onChange: (e: React.ChangeEvent<HTMLInputElement>) => setAccount(e.target.value), placeholder: "请输入用户名或工号", autoComplete: "username" }),
            )
          : React.createElement(React.Fragment, null,
              React.createElement("label", null, "工号"),
              React.createElement("input", { value: employeeId, required: true, onChange: (e: React.ChangeEvent<HTMLInputElement>) => setEmployeeId(e.target.value), placeholder: "例如 BJ-001", autoComplete: "username" }),
              React.createElement("label", null, "用户名"),
              React.createElement("input", { value: username, required: true, onChange: (e: React.ChangeEvent<HTMLInputElement>) => setUsername(e.target.value), placeholder: "用于显示和登录", autoComplete: "nickname" }),
            ),
        React.createElement("label", null, "密码"),
        React.createElement("input", { type: "password", value: password, required: true, minLength: 8, onChange: (e: React.ChangeEvent<HTMLInputElement>) => setPassword(e.target.value), placeholder: "至少 8 位字符", autoComplete: mode === "login" ? "current-password" : "new-password" }),
        mode === "register" ? React.createElement(React.Fragment, null,
          React.createElement("label", null, "再次输入密码"),
          React.createElement("input", { type: "password", value: confirm, required: true, minLength: 8, onChange: (e: React.ChangeEvent<HTMLInputElement>) => setConfirm(e.target.value), placeholder: "请再次输入密码", autoComplete: "new-password" }),
        ) : null,
        error ? React.createElement("div", { className: "error-box" }, error) : null,
        React.createElement("button", { className: "primary auth-submit", type: "submit", disabled: busy }, busy ? "正在处理…" : mode === "login" ? "登录工作台" : "创建账号并登录"),
      ),
      React.createElement("p", { className: "hint" }, "注册账号默认拥有开发者权限；审批和管理员权限由系统管理员分配。"),
    ),
  );
}
