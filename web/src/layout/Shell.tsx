import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useAuth } from "../auth";

const NAV = [
  { to: "/", label: "概览", end: true },
  { to: "/sources", label: "源" },
  { to: "/events", label: "事件" },
  { to: "/filter", label: "关注点" },
  { to: "/settings", label: "设置" },
] as const;

export function Shell() {
  const { logout } = useAuth();
  const navigate = useNavigate();

  function handleLogout() {
    logout();
    navigate("/login", { replace: true });
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="topbar-brand">
          <span className="topbar-mark" aria-hidden />
          <span>FeedSentry 运维控制台</span>
        </div>
        <div className="topbar-actions">
          <span className="topbar-meta">同源控制 · Bearer</span>
          <button type="button" className="btn btn-ghost" onClick={handleLogout}>
            退出
          </button>
        </div>
      </header>

      <nav className="sidebar" aria-label="主导航">
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={"end" in item ? item.end : false}
            className={({ isActive }) =>
              isActive ? "nav-link active" : "nav-link"
            }
          >
            <span className="nav-dot" aria-hidden />
            {item.label}
          </NavLink>
        ))}
      </nav>

      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
