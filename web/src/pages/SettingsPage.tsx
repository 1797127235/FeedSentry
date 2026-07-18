import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { errorDetail, testDestination } from "../api";
import { useAuth } from "../auth-context";
import { Toast, type ToastMessage } from "../components/Toast";

export function SettingsPage() {
  const { login, logout } = useAuth();
  const navigate = useNavigate();
  const [tokenInput, setTokenInput] = useState("");
  const [tokenError, setTokenError] = useState<string | null>(null);
  const [tokenBusy, setTokenBusy] = useState(false);
  const [testing, setTesting] = useState(false);
  const [toast, setToast] = useState<ToastMessage | null>(null);

  async function onChangeToken(event: FormEvent) {
    event.preventDefault();
    const token = tokenInput.trim();
    if (!token) {
      setTokenError("请输入新的 Bearer Token");
      return;
    }
    setTokenBusy(true);
    setTokenError(null);
    try {
      const response = await fetch("/api/status", {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (response.status === 401) {
        setTokenError("Token 无效或已过期");
        return;
      }
      if (!response.ok) {
        let detail = `验证失败 (${response.status})`;
        try {
          const body = (await response.json()) as { detail?: unknown };
          if (typeof body.detail === "string") {
            detail = body.detail;
          }
        } catch {
          /* keep default */
        }
        setTokenError(detail);
        return;
      }
      login(token);
      setTokenInput("");
      setToast({ kind: "success", text: "Token 已更新" });
    } catch (err) {
      setTokenError(errorDetail(err, "更新 Token 失败"));
    } finally {
      setTokenBusy(false);
    }
  }

  function onClearToken() {
    const ok = window.confirm("确定清除本机 Token 并退出登录？");
    if (!ok) {
      return;
    }
    logout();
    navigate("/login", { replace: true });
  }

  async function onTestDestination() {
    setTesting(true);
    try {
      const result = await testDestination();
      setToast({
        kind: "success",
        text: `测试通知已发送：${result.response}`,
      });
    } catch (err) {
      setToast({
        kind: "error",
        text: errorDetail(err, "测试通知失败"),
      });
    } finally {
      setTesting(false);
    }
  }

  return (
    <div>
      <header className="page-header">
        <div>
          <h1 className="page-title">设置</h1>
          <p className="page-desc">Token 与通知目的地测试</p>
        </div>
      </header>

      <section className="panel form-panel">
        <div className="panel-header">
          <h2 className="panel-title">访问令牌</h2>
        </div>
        <p className="help-text">
          Token 仅保存在本机 localStorage，不会写入 URL 或 cookie。
        </p>
        {tokenError ? (
          <p className="error-banner" role="alert">
            {tokenError}
          </p>
        ) : null}
        <form className="stack" onSubmit={onChangeToken}>
          <div className="field">
            <label htmlFor="settings-token">更换 Bearer Token</label>
            <textarea
              id="settings-token"
              name="token"
              autoComplete="off"
              spellCheck={false}
              placeholder="粘贴新令牌"
              value={tokenInput}
              onChange={(e) => setTokenInput(e.target.value)}
              disabled={tokenBusy}
              rows={4}
            />
          </div>
          <div className="page-actions">
            <button
              type="submit"
              className="btn btn-primary"
              disabled={tokenBusy}
            >
              {tokenBusy ? "验证中…" : "保存 Token"}
            </button>
            <button
              type="button"
              className="btn btn-danger"
              onClick={onClearToken}
            >
              清除 Token
            </button>
          </div>
        </form>
      </section>

      <section className="panel form-panel">
        <div className="panel-header">
          <h2 className="panel-title">通知测试</h2>
        </div>
        <p className="help-text">
          向当前配置的 Apprise / Telegram 目的地发送带 TEST 标记的探测消息。
        </p>
        <button
          type="button"
          className="btn btn-primary"
          disabled={testing}
          onClick={() => void onTestDestination()}
        >
          {testing ? "发送中…" : "测试通知"}
        </button>
      </section>

      <Toast message={toast} onClose={() => setToast(null)} />
    </div>
  );
}
