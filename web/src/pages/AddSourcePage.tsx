import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  addFeed,
  discoverFeeds,
  errorDetail,
  subscribeFeed,
  type CandidateView,
} from "../api";
import { Toast, type ToastMessage } from "../components/Toast";

type Tab = "direct" | "discover";

export function AddSourcePage() {
  const navigate = useNavigate();
  const [tab, setTab] = useState<Tab>("direct");
  const [feedUrl, setFeedUrl] = useState("");
  const [pageUrl, setPageUrl] = useState("");
  const [candidates, setCandidates] = useState<CandidateView[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<ToastMessage | null>(null);
  const [subscribingId, setSubscribingId] = useState<string | null>(null);

  async function onAddDirect(event: FormEvent) {
    event.preventDefault();
    const url = feedUrl.trim();
    if (!url) {
      setError("请输入 RSS / Atom 地址");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const result = await addFeed(url);
      const name = result.source.title || result.source.id;
      setToast({
        kind: "success",
        text: result.created
          ? `已添加 ${name}${result.baseline_initialized ? "（已建立基线）" : ""}`
          : `${name} 已存在`,
      });
      window.setTimeout(() => navigate("/sources"), 700);
    } catch (err) {
      setError(errorDetail(err, "添加源失败"));
    } finally {
      setLoading(false);
    }
  }

  async function onDiscover(event: FormEvent) {
    event.preventDefault();
    const url = pageUrl.trim();
    if (!url) {
      setError("请输入页面 URL");
      return;
    }
    setLoading(true);
    setError(null);
    setCandidates([]);
    try {
      const result = await discoverFeeds(url);
      setCandidates(result.candidates);
      if (result.candidates.length === 0) {
        setError("未发现可用订阅候选");
      } else {
        setToast({
          kind: "success",
          text: `发现 ${result.candidates.length} 个候选`,
        });
      }
    } catch (err) {
      setError(errorDetail(err, "发现订阅失败"));
    } finally {
      setLoading(false);
    }
  }

  async function onSubscribe(candidate: CandidateView) {
    setSubscribingId(candidate.candidate_id);
    setError(null);
    try {
      const result = await subscribeFeed(candidate.candidate_id);
      const name = result.source.title || result.source.id;
      setToast({
        kind: "success",
        text: result.created
          ? `已订阅 ${name}`
          : `${name} 已存在`,
      });
      window.setTimeout(() => navigate("/sources"), 700);
    } catch (err) {
      setError(errorDetail(err, "订阅失败"));
    } finally {
      setSubscribingId(null);
    }
  }

  return (
    <div>
      <header className="page-header">
        <div>
          <h1 className="page-title">添加源</h1>
          <p className="page-desc">直接填写 RSS，或从页面发现候选后订阅</p>
        </div>
        <div className="page-actions">
          <Link className="btn" to="/sources">
            返回列表
          </Link>
        </div>
      </header>

      <div className="tabs" role="tablist" aria-label="添加方式">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "direct"}
          className={tab === "direct" ? "tab active" : "tab"}
          onClick={() => {
            setTab("direct");
            setError(null);
          }}
        >
          直接 RSS
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "discover"}
          className={tab === "discover" ? "tab active" : "tab"}
          onClick={() => {
            setTab("discover");
            setError(null);
          }}
        >
          页面发现
        </button>
      </div>

      {error ? (
        <p className="error-banner" role="alert">
          {error}
        </p>
      ) : null}

      {tab === "direct" ? (
        <form className="panel form-panel" onSubmit={onAddDirect}>
          <div className="field">
            <label htmlFor="feed-url">RSS / Atom URL</label>
            <input
              id="feed-url"
              name="feed-url"
              type="url"
              placeholder="https://example.com/feed.xml"
              value={feedUrl}
              onChange={(e) => setFeedUrl(e.target.value)}
              disabled={loading}
              autoComplete="off"
              spellCheck={false}
            />
          </div>
          <p className="help-text">
            首次成功轮询仅建立基线，不会为历史条目发送通知。
          </p>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={loading}
          >
            {loading ? "提交中…" : "添加源"}
          </button>
        </form>
      ) : (
        <div className="stack">
          <form className="panel form-panel" onSubmit={onDiscover}>
            <div className="field">
              <label htmlFor="page-url">页面 URL</label>
              <input
                id="page-url"
                name="page-url"
                type="url"
                placeholder="https://example.com/"
                value={pageUrl}
                onChange={(e) => setPageUrl(e.target.value)}
                disabled={loading}
                autoComplete="off"
                spellCheck={false}
              />
            </div>
            <p className="help-text">
              使用已配置的 RSSHub Radar 规则发现可订阅候选。
            </p>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={loading}
            >
              {loading ? "发现中…" : "发现订阅"}
            </button>
          </form>

          {candidates.length > 0 ? (
            <section className="panel">
              <div className="panel-header">
                <h2 className="panel-title">候选列表</h2>
                <span className="panel-meta">{candidates.length} 个</span>
              </div>
              <div className="table-wrap">
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>标题</th>
                      <th>Feed</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {candidates.map((candidate) => {
                      const busy =
                        subscribingId === candidate.candidate_id;
                      return (
                        <tr key={candidate.candidate_id}>
                          <td>
                            <div className="cell-primary">
                              {candidate.title}
                            </div>
                            <div className="cell-muted mono">
                              {candidate.page_url}
                            </div>
                          </td>
                          <td className="mono cell-muted">
                            {candidate.feed_url}
                          </td>
                          <td>
                            <button
                              type="button"
                              className="btn btn-sm btn-primary"
                              disabled={busy || loading}
                              onClick={() => void onSubscribe(candidate)}
                            >
                              {busy ? "订阅中…" : "订阅"}
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </section>
          ) : null}
        </div>
      )}

      <Toast message={toast} onClose={() => setToast(null)} />
    </div>
  );
}
