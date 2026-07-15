import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  checkSource,
  errorDetail,
  listSources,
  removeSource,
  setSourceEnabled,
  type SourceView,
} from "../api";
import { Toast, type ToastMessage } from "../components/Toast";
import { formatDateTime, sourceLabel } from "../format";

export function SourcesPage() {
  const [sources, setSources] = useState<SourceView[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [toast, setToast] = useState<ToastMessage | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await listSources();
      setSources(data.sources);
    } catch (err) {
      setError(errorDetail(err, "加载源列表失败"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function onToggle(source: SourceView) {
    setBusyId(source.id);
    try {
      await setSourceEnabled(source.id, !source.enabled);
      setSources((prev) =>
        prev.map((item) =>
          item.id === source.id
            ? { ...item, enabled: !source.enabled }
            : item,
        ),
      );
      setToast({
        kind: "success",
        text: source.enabled
          ? `已停用 ${sourceLabel(source)}`
          : `已启用 ${sourceLabel(source)}`,
      });
    } catch (err) {
      setToast({
        kind: "error",
        text: errorDetail(err, "更新源状态失败"),
      });
    } finally {
      setBusyId(null);
    }
  }

  async function onCheck(source: SourceView) {
    setBusyId(source.id);
    try {
      const result = await checkSource(source.id);
      setToast({
        kind: "success",
        text: `检查完成：新建 ${result.created_events} 个事件`,
      });
      await load();
    } catch (err) {
      setToast({
        kind: "error",
        text: errorDetail(err, "立即检查失败"),
      });
    } finally {
      setBusyId(null);
    }
  }

  async function onDelete(source: SourceView) {
    const ok = window.confirm(
      `确定删除源「${sourceLabel(source)}」？\n${source.feed_url}`,
    );
    if (!ok) {
      return;
    }
    setBusyId(source.id);
    try {
      await removeSource(source.id);
      setSources((prev) => prev.filter((item) => item.id !== source.id));
      setToast({ kind: "success", text: `已删除 ${sourceLabel(source)}` });
    } catch (err) {
      setToast({
        kind: "error",
        text: errorDetail(err, "删除源失败"),
      });
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div>
      <header className="page-header">
        <div>
          <h1 className="page-title">源</h1>
          <p className="page-desc">信息源列表、启停、检查与删除</p>
        </div>
        <div className="page-actions">
          <button
            type="button"
            className="btn"
            onClick={() => void load()}
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

      <section className="panel">
        {loading && sources.length === 0 ? (
          <p className="empty-state">加载中…</p>
        ) : sources.length === 0 ? (
          <p className="empty-state">
            暂无信息源。{" "}
            <Link to="/sources/add">添加第一个源</Link>
          </p>
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>源</th>
                  <th>类型</th>
                  <th>启用</th>
                  <th>健康</th>
                  <th>下次检查</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {sources.map((source) => {
                  const busy = busyId === source.id;
                  const unhealthy =
                    source.consecutive_failures > 0 ||
                    Boolean(source.last_error);
                  return (
                    <tr key={source.id}>
                      <td>
                        <div className="cell-primary">
                          {sourceLabel(source)}
                        </div>
                        <div className="cell-muted mono">
                          {source.feed_url}
                        </div>
                      </td>
                      <td>
                        <span className="badge badge-muted">
                          {source.kind}
                        </span>
                      </td>
                      <td>
                        <label className="toggle">
                          <input
                            type="checkbox"
                            checked={source.enabled}
                            disabled={busy}
                            onChange={() => void onToggle(source)}
                            aria-label={
                              source.enabled
                                ? `停用 ${sourceLabel(source)}`
                                : `启用 ${sourceLabel(source)}`
                            }
                          />
                          <span className="toggle-track" />
                        </label>
                      </td>
                      <td>
                        {unhealthy ? (
                          <div>
                            <span className="badge badge-danger">
                              失败 {source.consecutive_failures}
                            </span>
                            {source.last_error ? (
                              <div className="cell-error cell-error-sm">
                                {source.last_error}
                              </div>
                            ) : null}
                          </div>
                        ) : (
                          <span className="badge badge-ok">正常</span>
                        )}
                      </td>
                      <td>{formatDateTime(source.next_check_at)}</td>
                      <td>
                        <div className="row-actions">
                          <button
                            type="button"
                            className="btn btn-sm"
                            disabled={busy}
                            onClick={() => void onCheck(source)}
                          >
                            立即检查
                          </button>
                          <button
                            type="button"
                            className="btn btn-sm btn-danger"
                            disabled={busy}
                            onClick={() => void onDelete(source)}
                          >
                            删除
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <Toast message={toast} onClose={() => setToast(null)} />
    </div>
  );
}
