import type { ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth";
import { Shell } from "./layout/Shell";
import { LoginPage } from "./pages/LoginPage";
import { PlaceholderPage } from "./pages/PlaceholderPage";

function RequireAuth({ children }: { children: ReactNode }) {
  const { isAuthenticated } = useAuth();
  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }
  return children;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireAuth>
            <Shell />
          </RequireAuth>
        }
      >
        <Route
          index
          element={
            <PlaceholderPage
              title="概览"
              description="系统健康、积压与调度摘要"
            />
          }
        />
        <Route
          path="sources"
          element={
            <PlaceholderPage title="源" description="信息源列表与健康状态" />
          }
        />
        <Route
          path="sources/add"
          element={
            <PlaceholderPage title="添加源" description="直接 RSS 或页面发现" />
          }
        />
        <Route
          path="events"
          element={
            <PlaceholderPage title="事件" description="事件流与 AI 审计" />
          }
        />
        <Route
          path="events/:id"
          element={
            <PlaceholderPage title="事件详情" description="决策、投递与重试" />
          }
        />
        <Route
          path="filter"
          element={
            <PlaceholderPage
              title="关注点"
              description="全局筛选目标 filter.goal"
            />
          }
        />
        <Route
          path="settings"
          element={
            <PlaceholderPage title="设置" description="Token 与测试通知" />
          }
        />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
