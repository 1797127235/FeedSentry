import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  errorDetail,
  getEvent,
  retryEvent,
  type EventDetailView,
} from "../api";
import { Toast, type ToastMessage } from "../components/Toast";
import { formatDateTime } from "../format";

export function EventDetailPage() {
  const { id } = useParams();
  const eventId = Number(id);
  const [detail, setDetail] = useState<EventDetailView | null>(null);
  const [loading, setLoading] = useState(true);
  const [retrying, setRetrying] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<ToastMessage | null>(null);

  const load = useCallback(async () => {
    if (!Number.isFinite(eventId) || eventId <= 0) {
      setError("无效的事件 ID");
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const next = await getEvent(eventId);
      setDetail(next);
    } catch (err) {
      setDetail(null);
      setError(errorDetail(err, "加载事件详情失败"));
    } finally {
      setLoading(false);
    }
  }, [eventId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function onRetry() {
    setRetrying(true);
    try {
      const result = await retryEvent(eventId);
      setToast({
        kind: "success",
        text: result.retried ? "已提交重试" : "未触发重试（可能已非失败状态）",
      });
      await load();
    } catch (err) {
      setToast({
        kind: "error",
        text: errorDetail(err, "重试失败"),
      });
    } finally {
      setRetrying(false);
    }
  }

  const event = detail?.event;
  const failed = event?.status === "failed";

  return (
    <div>
      <header className="page-header">
        <div>
          <p className="breadcrumb">
            <Link to="/events">事件</Link>
            <span aria-hidden> / </span>
            <span>#{Number.isFinite(eventId) ? eventId : "—"}</span>
          </p>
          <h1 className="page-title">
            {event ? event.output_title || event.title : "事件详情"}
          </h1>
          <p className="page-desc">决策审计、投递记录与失败重试</p>
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
          {failed ? (
            <button
              type="button"
              className="btn btn-primary"
              disabled={retrying}
              onClick={() => void onRetry()}
            >
              {retrying ? "重试中…" : "重试"}
            </button>
          ) : null}
        </div>
      </header>

      {error ? (
        <p className="error-banner" role="alert">
          {error}
        </p>
      ) : null}

      {loading && !detail ? (
        <p className="empty-state">加载中…</p>
      ) : detail && event ? (
        <>
          <section className="stat-grid stat-grid-3" aria-label="事件摘要">
            <article className="stat-card">
              <div className="stat-label">状态</div>
              <div className="stat-value stat-value-sm">{event.status}</div>
            </article>
            <article className="stat-card">
              <div className="stat-label">失败次数</div>
              <div
                className={
                  event.failure_count > 0
                    ? "stat-value danger"
                    : "stat-value"
                }
              >
                {event.failure_count}
              </div>
            </article>
            <article className="stat-card">
              <div className="stat-label">更新时间</div>
              <div className="stat-value stat-value-sm">
                {formatDateTime(event.updated_at)}
              </div>
            </article>
          </section>

          <section className="panel">
            <div className="panel-header">
              <h2 className="panel-title">条目</h2>
            </div>
            <dl className="detail-list">
              <div>
                <dt>标题</dt>
                <dd>{event.title}</dd>
              </div>
              <div>
                <dt>链接</dt>
                <dd>
                  <a href={event.link} target="_blank" rel="noreferrer">
                    {event.link}
                  </a>
                </dd>
              </div>
              <div>
                <dt>源</dt>
                <dd>
                  {event.source_id ? `${event.source_id} · ` : ""}
                  <span className="mono">{event.source_url}</span>
                </dd>
              </div>
              <div>
                <dt>作者</dt>
                <dd>{detail.author || "—"}</dd>
              </div>
              <div>
                <dt>发布时间</dt>
                <dd>{formatDateTime(detail.published_at)}</dd>
              </div>
              <div>
                <dt>entry_id</dt>
                <dd className="mono">{event.entry_id}</dd>
              </div>
              <div>
                <dt>resume_stage</dt>
                <dd>{event.resume_stage || "—"}</dd>
              </div>
              <div>
                <dt>下次尝试</dt>
                <dd>{formatDateTime(event.next_attempt_at)}</dd>
              </div>
              {event.last_error ? (
                <div>
                  <dt>最近错误</dt>
                  <dd className="cell-error">{event.last_error}</dd>
                </div>
              ) : null}
            </dl>
          </section>

          <section className="panel">
            <div className="panel-header">
              <h2 className="panel-title">AI 决策</h2>
            </div>
            <dl className="detail-list">
              <div>
                <dt>decision_reason</dt>
                <dd>{event.decision_reason || "—"}</dd>
              </div>
              <div>
                <dt>output_title</dt>
                <dd>{event.output_title || "—"}</dd>
              </div>
              <div>
                <dt>output_summary</dt>
                <dd className="pre-wrap">{event.output_summary || "—"}</dd>
              </div>
            </dl>
          </section>

          <section className="panel">
            <div className="panel-header">
              <h2 className="panel-title">goal_snapshot</h2>
              <span className="panel-meta">事件创建时的筛选目标快照</span>
            </div>
            <pre className="code-block">{detail.goal_snapshot || "—"}</pre>
          </section>

          <section className="panel">
            <div className="panel-header">
              <h2 className="panel-title">投递</h2>
              <span className="panel-meta">
                {detail.deliveries.length} 条记录
              </span>
            </div>
            {detail.deliveries.length === 0 ? (
              <p className="empty-state">暂无投递记录</p>
            ) : (
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>目的地</th>
                      <th>状态</th>
                      <th>尝试</th>
                      <th>响应</th>
                      <th>更新时间</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detail.deliveries.map((delivery, index) => (
                      <tr
                        key={`${delivery.destination_key}-${delivery.created_at}-${index}`}
                      >
                        <td className="mono">{delivery.destination_key}</td>
                        <td>
                          <span
                            className={
                              delivery.status === "delivered"
                                ? "badge badge-ok"
                                : "badge"
                            }
                          >
                            {delivery.status}
                          </span>
                        </td>
                        <td>{delivery.attempts}</td>
                        <td className="cell-muted">
                          {delivery.response_summary || "—"}
                        </td>
                        <td>{formatDateTime(delivery.updated_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        </>
      ) : null}

      <Toast message={toast} onClose={() => setToast(null)} />
    </div>
  );
}
