import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  errorDetail,
  listEvents,
  listSources,
  type EventView,
  type SourceView,
} from "../api";
import { Toast, type ToastMessage } from "../components/Toast";
import { formatDateTime } from "../format";

const STATUS_TABS: { value: string; label: string }[] = [
  { value: "", label: "全部" },
  { value: "discovered", label: "已发现" },
  { value: "screening", label: "筛选中" },
  { value: "filtered", label: "已过滤" },
  { value: "fetching", label: "抓取中" },
  { value: "summarizing", label: "摘要中" },
  { value: "delivery_pending", label: "待投递" },
  { value: "delivering", label: "投递中" },
  { value: "delivered", label: "已投递" },
  { value: "retry_wait", label: "等待重试" },
  { value: "failed", label: "失败" },
];

function statusBadgeClass(status: string): string {
  if (status === "delivered") {
    return "badge badge-ok";
  }
  if (status === "failed") {
    return "badge badge-danger";
  }
  if (status === "filtered") {
    return "badge badge-muted";
  }
  if (status === "retry_wait") {
    return "badge badge-warn";
  }
  return "badge";
}

function statusLabel(status: string): string {
  return STATUS_TABS.find((tab) => tab.value === status)?.label ?? status;
}

export function EventsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const status = searchParams.get("status") ?? "";
  const sourceId = searchParams.get("source_id") ?? "";
  const qParam = searchParams.get("q") ?? "";

  const [qInput, setQInput] = useState(qParam);
  const [items, setItems] = useState<EventView[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [sources, setSources] = useState<SourceView[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<ToastMessage | null>(null);

  useEffect(() => {
    setQInput(qParam);
  }, [qParam]);

  useEffect(() => {
    void listSources()
      .then((data) => setSources(data.sources))
      .catch(() => {
        /* optional filter source */
      });
  }, []);

  const load = useCallback(
    async (cursor?: string | null, append = false) => {
      if (append) {
        setLoadingMore(true);
      } else {
        setLoading(true);
      }
      setError(null);
      try {
        const data = await listEvents({
          status: status || undefined,
          source_id: sourceId || undefined,
          q: qParam || undefined,
          limit: 50,
          cursor: cursor || undefined,
        });
        setItems((prev) => (append ? [...prev, ...data.items] : data.items));
        setNextCursor(data.next_cursor);
      } catch (err) {
        const detail = errorDetail(err, "加载事件失败");
        setError(detail);
        if (!append) {
          setItems([]);
          setNextCursor(null);
        }
        setToast({ kind: "error", text: detail });
      } finally {
        setLoading(false);
        setLoadingMore(false);
      }
    },
    [status, sourceId, qParam],
  );

  useEffect(() => {
    void load();
  }, [load]);

  function updateParams(patch: {
    status?: string;
    source_id?: string;
    q?: string;
  }) {
    const next = new URLSearchParams(searchParams);
    const apply = (key: string, value: string | undefined) => {
      if (value === undefined) {
        return;
      }
      if (value) {
        next.set(key, value);
      } else {
        next.delete(key);
      }
    };
    apply("status", patch.status);
    apply("source_id", patch.source_id);
    apply("q", patch.q);
    setSearchParams(next, { replace: true });
  }

  function onSearch(event: FormEvent) {
    event.preventDefault();
    updateParams({ q: qInput.trim() });
  }

  return (
    <div>
      <header className="page-header">
        <div>
          <h1 className="page-title">事件</h1>
          <p className="page-desc">事件流、状态筛选与 AI 审计入口</p>
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
        </div>
      </header>

      {error ? (
        <p className="error-banner" role="alert">
          {error}
        </p>
      ) : null}

      <div className="tabs tabs-wrap" role="tablist" aria-label="事件状态">
        {STATUS_TABS.map((tab) => (
          <button
            key={tab.value || "all"}
            type="button"
            role="tab"
            aria-selected={status === tab.value}
            className={status === tab.value ? "tab active" : "tab"}
            onClick={() => updateParams({ status: tab.value })}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <form className="filter-bar" onSubmit={onSearch}>
        <div className="field field-inline">
          <label htmlFor="event-source">源</label>
          <select
            id="event-source"
            value={sourceId}
            onChange={(e) => updateParams({ source_id: e.target.value })}
          >
            <option value="">全部源</option>
            {sources.map((source) => (
              <option key={source.id} value={source.id}>
                {source.title?.trim() || source.id}
              </option>
            ))}
          </select>
        </div>
        <div className="field field-inline field-grow">
          <label htmlFor="event-q">搜索标题</label>
          <input
            id="event-q"
            type="search"
            value={qInput}
            onChange={(e) => setQInput(e.target.value)}
            placeholder="按条目标题关键字"
          />
        </div>
        <button type="submit" className="btn btn-primary">
          搜索
        </button>
      </form>

      <section className="panel">
        {loading && items.length === 0 ? (
          <p className="empty-state">加载中…</p>
        ) : items.length === 0 ? (
          <p className="empty-state">暂无匹配事件</p>
        ) : (
          <div className="table-wrap">
            <table className="data-table">
              <thead>
                <tr>
                  <th>标题</th>
                  <th>状态</th>
                  <th>源</th>
                  <th>决策</th>
                  <th>更新时间</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.event_id}>
                    <td>
                      <div className="cell-primary">
                        <Link to={`/events/${item.event_id}`}>
                          {item.output_title || item.title}
                        </Link>
                      </div>
                      <div className="cell-muted mono">#{item.event_id}</div>
                    </td>
                    <td>
                      <span className={statusBadgeClass(item.status)}>
                        {statusLabel(item.status)}
                      </span>
                      {item.failure_count > 0 ? (
                        <div className="cell-error cell-error-sm">
                          失败 {item.failure_count}
                          {item.last_error ? `：${item.last_error}` : ""}
                        </div>
                      ) : null}
                    </td>
                    <td>
                      <div className="cell-primary">
                        {item.source_id || "—"}
                      </div>
                      <div className="cell-muted mono">{item.source_url}</div>
                    </td>
                    <td className="cell-muted">
                      {item.decision_reason || "—"}
                    </td>
                    <td>{formatDateTime(item.updated_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {nextCursor ? (
          <div className="load-more">
            <button
              type="button"
              className="btn"
              disabled={loadingMore}
              onClick={() => void load(nextCursor, true)}
            >
              {loadingMore ? "加载中…" : "加载更多"}
            </button>
          </div>
        ) : null}
      </section>

      <Toast message={toast} onClose={() => setToast(null)} />
    </div>
  );
}
