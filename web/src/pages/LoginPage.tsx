import { useState, type FormEvent } from "react";
import { Navigate, useNavigate } from "react-router-dom";
import { useAuth } from "../auth-context";
import { ApiError, clearToken, setToken } from "../api";

export function LoginPage() {
  const { isAuthenticated, login } = useAuth();
  const navigate = useNavigate();
  const [tokenInput, setTokenInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  if (isAuthenticated) {
    return <Navigate to="/" replace />;
  }

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const token = tokenInput.trim();
    if (!token) {
      setError("请输入 Bearer Token");
      return;
    }

    setLoading(true);
    setError(null);

    try {
      setToken(token);
      const response = await fetch("/api/status", {
        headers: { Authorization: `Bearer ${token}` },
      });

      if (response.status === 401) {
        clearToken();
        setError("Token 无效或已过期");
        return;
      }

      if (!response.ok) {
        clearToken();
        let detail = `验证失败 (${response.status})`;
        try {
          const body = (await response.json()) as { detail?: unknown };
          if (typeof body.detail === "string") {
            detail = body.detail;
          }
        } catch {
          /* keep default */
        }
        throw new ApiError(response.status, detail);
      }

      login(token);
      navigate("/", { replace: true });
    } catch (err) {
      clearToken();
      if (err instanceof ApiError) {
        setError(err.detail);
      } else if (err instanceof TypeError) {
        setError("无法连接后端，请确认服务已启动");
      } else {
        setError("登录失败，请重试");
      }
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="login-page">
      <form className="login-card" onSubmit={onSubmit}>
        <h1>登录</h1>
        <p>
          使用与 MCP 相同的 <code>FEEDSENTRY_MCP_TOKEN</code>。Token 仅保存在本机
          localStorage，不会写入 URL 或 cookie。
        </p>

        {error ? (
          <p className="error-banner" role="alert">
            {error}
          </p>
        ) : null}

        <div className="field">
          <label htmlFor="token">Bearer Token</label>
          <textarea
            id="token"
            name="token"
            autoComplete="off"
            spellCheck={false}
            placeholder="粘贴访问令牌"
            value={tokenInput}
            onChange={(e) => setTokenInput(e.target.value)}
            disabled={loading}
          />
        </div>

        <button
          type="submit"
          className="btn btn-primary full-width"
          disabled={loading}
        >
          {loading ? "验证中…" : "进入控制台"}
        </button>
      </form>
    </div>
  );
}
