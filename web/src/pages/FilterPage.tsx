import { useCallback, useEffect, useState, type FormEvent } from "react";
import { appendFilter, errorDetail, getFilter, setFilter } from "../api";
import { Toast, type ToastMessage } from "../components/Toast";

export function FilterPage() {
  const [goal, setGoal] = useState("");
  const [savedGoal, setSavedGoal] = useState("");
  const [appendText, setAppendText] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [appending, setAppending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<ToastMessage | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getFilter();
      setGoal(data.goal);
      setSavedGoal(data.goal);
    } catch (err) {
      setError(errorDetail(err, "加载筛选目标失败"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    const next = goal.trim();
    if (!next) {
      setError("关注目标不能为空");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const result = await setFilter(next);
      setSavedGoal(next);
      setGoal(next);
      setToast({
        kind: "success",
        text: result.changed ? "已保存筛选目标" : "目标未变化",
      });
    } catch (err) {
      setError(errorDetail(err, "保存筛选目标失败"));
    } finally {
      setSaving(false);
    }
  }

  async function onAppend(event: FormEvent) {
    event.preventDefault();
    const text = appendText.trim();
    if (!text) {
      setError("追加内容不能为空");
      return;
    }
    setAppending(true);
    setError(null);
    try {
      const result = await appendFilter(text);
      setToast({
        kind: "success",
        text: result.changed ? "已追加到筛选目标" : "该内容已存在，未变化",
      });
      setAppendText("");
      await load();
    } catch (err) {
      setError(errorDetail(err, "追加筛选目标失败"));
    } finally {
      setAppending(false);
    }
  }

  const dirty = goal !== savedGoal;

  return (
    <div>
      <header className="page-header">
        <div>
          <h1 className="page-title">关注点</h1>
          <p className="page-desc">全局筛选目标 filter.goal</p>
        </div>
        <div className="page-actions">
          <button
            type="button"
            className="btn"
            onClick={() => void load()}
            disabled={loading || saving || appending}
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

      <section className="panel form-panel">
        {loading && !savedGoal && !goal ? (
          <p className="empty-state">加载中…</p>
        ) : (
          <form className="stack" onSubmit={onSubmit}>
            <div className="field">
              <label htmlFor="filter-goal">筛选目标</label>
              <textarea
                id="filter-goal"
                name="goal"
                value={goal}
                onChange={(e) => setGoal(e.target.value)}
                disabled={saving}
                spellCheck={false}
                rows={10}
              />
            </div>
            <p className="help-text">
              仅影响之后新条目。已创建事件继续使用其 goal_snapshot，不会随本次修改重算。
            </p>
            <div className="page-actions">
              <button
                type="submit"
                className="btn btn-primary"
                disabled={saving || !dirty}
              >
                {saving ? "保存中…" : "保存"}
              </button>
              <button
                type="button"
                className="btn"
                disabled={saving || !dirty}
                onClick={() => setGoal(savedGoal)}
              >
                重置
              </button>
            </div>
          </form>
        )}
      </section>

      <section className="panel form-panel">
        <form className="stack" onSubmit={onAppend}>
          <div className="field">
            <label htmlFor="filter-append">追加关注点</label>
            <input
              id="filter-append"
              name="text"
              type="text"
              value={appendText}
              onChange={(e) => setAppendText(e.target.value)}
              placeholder="输入要追加的一段内容，按换行拼接到现有目标末尾"
              disabled={appending}
              spellCheck={false}
            />
          </div>
          <p className="help-text">
            以换行拼接到现有目标末尾。已存在的整行内容幂等无变化。
          </p>
          <div className="page-actions">
            <button
              type="submit"
              className="btn btn-primary"
              disabled={appending || !appendText.trim()}
            >
              {appending ? "追加中…" : "追加"}
            </button>
          </div>
        </form>
      </section>

      <Toast message={toast} onClose={() => setToast(null)} />
    </div>
  );
}
