import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  errorDetail,
  getStatus,
  type SourceView,
  type SystemStatus,
} from "../api";
import { Toast, type ToastMessage } from "../components/Toast";
import { formatDateTime, sourceLabel } from "../format";

function isUnhealthy(source: SourceView): boolean {
  return source.consecutive_failures > 0 || Boolean(source.last_error);
}

export function DashboardPage() {
  const [status, setStatus] = useState<SystemStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<ToastMessage | null>(null);

  const load = useCallback(async (showToast = false) => {
    setLoading(true);
    setError(null);
    try {
      const next = await getStatus();
      setStatus(next);
      if (showToast) {
        setToast({ kind: "success", text: "已刷新" });
      }
    } catch (err) {
      const detail = errorDetail(err, "加载概览失败");
      setError(detail);
      if (showToast) {
        setToast({ kind: "error", text: detail });
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const unhealthy = (status?.source_statuses ?? []).filter(isUnhealthy);

  return (
    <div>
      <header className="page-header">
        <div>
          <h1 className="page-title">概览</h1>
          <p className="page-desc">系统健康、积压与调度摘要</p>
        </div>
        <div className="page-actions">
          <button
            type="button"
            className="btn"
            onClick={() => void load(true)}
            disabled={loading}
          >
            {loading ? "刷新中…" : "刷新"}
          </button>
          <Link className="btn btn-primary" to="/sources/add">
            添加源
          </Link>
        </div>
      </header>

      {error ? (
        <p className="error-banner" role="alert">
          {error}
        </p>
      ) : null}

      {status?.config_error ? (
        <p className="error-banner" role="alert">
          配置错误：{status.config_error}
        </p>
      ) : null}

      <section className="stat-grid" aria-label="系统计数">
        <article className="stat-card">
          <div className="stat-label">待处理事件</div>
          <div className="stat-value">{status?.pending_events ?? "—"}</div>
        </article>
        <article className="stat-card">
          <div className="stat-label">失败事件</div>
          <div className="stat-value danger">
            {status?.failed_events ?? "—"}
          </div>
        </article>
        <article className="stat-card">
          <div className="stat-label">已启用源</div>
          <div className="stat-value">
            {status
              ? `${status.enabled_sources} / ${status.sources}`
              : "—"}
          </div>
        </article>
        <article className="stat-card">
          <div className="stat-label">最近调度</div>
          <div className="stat-value stat-value-sm">
            {formatDateTime(status?.last_tick_at)}
          </div>
        </article>
      </section>

      <section className="panel">
        <div className="panel-header">
          <h2 className="panel-title">异常源</h2>
          <span className="panel-meta">
            连续失败 &gt; 0 或存在最近错误
          </span>
        </div>
        {loading && !status ? (
          <p className="empty-state">加载中…</p>
        ) : unhealthy.length === 0 ? (
          <p className="empty-state">当前没有异常源</p>
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>源</th>
                  <th>状态</th>
                  <th>连续失败</th>
                  <th>最近错误</th>
                  <th>上次成功</th>
                </tr>
              </thead>
              <tbody>
                {unhealthy.map((source) => (
                  <tr key={source.id}>
                    <td>
                      <div className="cell-primary">
                        {sourceLabel(source)}
                      </div>
                      <div className="cell-muted mono">{source.feed_url}</div>
                    </td>
                    <td>
                      <span
                        className={
                          source.enabled
                            ? "badge badge-ok"
                            : "badge badge-muted"
                        }
                      >
                        {source.enabled ? "启用" : "停用"}
                      </span>
                    </td>
                    <td>
                      <span className="badge badge-danger">
                        {source.consecutive_failures}
                      </span>
                    </td>
                    <td className="cell-error">
                      {source.last_error || "—"}
                    </td>
                    <td>{formatDateTime(source.last_success_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <Toast message={toast} onClose={() => setToast(null)} />
    </div>
  );
}
