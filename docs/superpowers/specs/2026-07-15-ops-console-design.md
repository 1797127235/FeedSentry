# FeedSentry 运维控制台设计

日期：2026-07-15

## 1. 目标

在现有全局 RSS 管线与 MCP 控制面之上，增加一套**同源控制**的 Web 运维控制台：

- **巡检**：系统是否健康、源是否失败、积压多少、上次调度何时。
- **排障**：失败事件原因、按失败阶段重试、立即检查源、测试通知。
- **AI 审计**：浏览 accept / discard / fetch 相关决策字段与输出摘要。
- **管理**：与 MCP 14 个工具对齐的写操作（源、关注点、恢复、测通知）。

控制台是第二入口，不是第二套业务逻辑。HTTP 与 MCP 都必须只调用 Control
Services，不得直接编辑 YAML、操作 ORM 或执行 Shell。

未设置 `FEEDSENTRY_MCP_TOKEN` 时，控制台与 `/api/*` 关闭；后台轮询管线不受影响。

## 2. 用户结果

用户在浏览器中（经 HTTPS 反向代理访问本机绑定端口）可以：

1. 使用与 MCP 相同的 Bearer Token 登录控制台。
2. 在概览页看到 pending / failed、启用源数量、`last_tick_at`、配置错误与异常源摘要。
3. 查看每个源的健康字段（上次成功、连续失败、`last_error`、下次检查），并启停、删除、立即检查。
4. 添加直接 RSS/Atom，或通过页面 URL 发现 RSSHub 候选后订阅（与 MCP 两步流一致）。
5. 分页浏览事件流，按状态 / 源筛选，打开详情查看决策原因、输出摘要与投递记录。
6. 对终端失败事件从失败阶段重试。
7. 查看与修改全局 `filter.goal`（仅影响之后新条目）。
8. 发送带 TEST 标记的测试通知。

## 3. 系统结构

```text
Browser (React SPA)
      |
      | Authorization: Bearer <FEEDSENTRY_MCP_TOKEN>
      v
FastAPI
  |- /health/*          公开
  |- /status            公开精简计数（兼容现有运维脚本）
  |- /api/*             需 Bearer；未设 token 时不挂载
  |- /                  SPA 静态资源（与 /api 同开同关）
  `- /mcp               现有 MCP Streamable HTTP（不变）
         |
         v
Control Services（唯一写路径与状态聚合）
  |- SourceService
  |- FilterService
  |- StatusService（扩展 last_tick / status 分布）
  |- RecoveryService
  |- DestinationService
  `- 事件查询方法（list_events / get_event；可落在 Status 或 Observability 辅助）
         |
         +-> ConfigStore -> config.yaml
         +-> Repository  -> SQLite（只读查询增量，无 schema 迁移）
         +-> PollCoordinator / RSSHub / Apprise|Telegram

后台管线：Scheduler -> RSS -> AI -> Firecrawl -> 通知（不变）
```

### 硬约束

- 全局单管线；不引入任务、监控器或任务级规则 / 通知目标。
- 配置写入必须保留环境变量占位符，经 ConfigStore 原子替换。
- 事件状态机与幂等投递规则不变；重试必须从已保存失败阶段恢复。
- 不向 API 响应暴露 AI / Firecrawl / Telegram / Apprise 等密钥。

## 4. REST API

前缀：`/api`。除特别说明外均需：

```http
Authorization: Bearer <FEEDSENTRY_MCP_TOKEN>
```

Handler 只做参数校验与 JSON 映射；业务全部在 control。

### 4.1 只读（监控与 AI 审计）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/status` | `StatusService.get_status` 完整结构，外加 `last_tick_at` 与按 `EventStatus` 的计数分布 |
| `GET` | `/api/sources` | 对齐 `list_sources` |
| `GET` | `/api/filter` | `{ "goal": "..." }`，对齐 `get_filter_goal` |
| `GET` | `/api/events` | **新增**分页列表；查询参数：`status`、`source_id`、`q`、`limit`、`cursor` |
| `GET` | `/api/events/failed` | 对齐 `list_failed_events`（须注册在 `/{id}` 之前，避免被当成 id） |
| `GET` | `/api/events/{id}` | **新增**详情：entry 元数据 + decision/output + deliveries |

**事件列表项最小字段：**

- `event_id`, `status`, `resume_stage`（可空）
- `title`, `link`, `source_url`, `source_id`（能从当前配置反查则填）
- `decision_reason`, `output_title`, `output_summary`
- `failure_count`, `last_error`, `next_attempt_at`
- `created_at`, `updated_at`

**详情额外字段：**

- `goal_snapshot`（可截断）、`entry_id`、`author`、`published_at`
- `deliveries[]`：`destination_key`（沿用 deliveries.apprise_key 列语义）、`status`、`attempts`、`response_summary`、时间戳
- **不**默认返回完整 scrape markdown 或 entry `raw_json`

**分页：**

- `limit` 默认 50，最大 100
- 不透明 `cursor`（基于 `updated_at,id` 降序）
- 响应：`{ "items": [...], "next_cursor": "..." | null }`

### 4.2 写操作（对齐 MCP）

| 方法 | 路径 | MCP 工具 | Body / 说明 |
| --- | --- | --- | --- |
| `POST` | `/api/feeds/discover` | `discover_feeds` | `{ "page_url": "..." }` |
| `POST` | `/api/feeds/subscribe` | `subscribe_feed` | `{ "candidate_id": "..." }` |
| `POST` | `/api/feeds` | `add_feed` | `{ "url": "..." }` |
| `PATCH` | `/api/sources/{id}` | `set_source_enabled` | `{ "enabled": true \| false }` |
| `DELETE` | `/api/sources/{id}` | `remove_source` | |
| `POST` | `/api/sources/{id}/check` | `check_source_now` | 返回新建事件数等既有结果 |
| `PUT` | `/api/filter` | `set_filter_goal` | `{ "goal": "..." }` |
| `POST` | `/api/events/{id}/retry` | `retry_failed_event` | |
| `POST` | `/api/destination/test` | `test_destination` | 发送 TEST 标记消息 |

### 4.3 错误约定

| 状态 | 含义 |
| --- | --- |
| 401 | 缺少或错误 Bearer |
| 404 | 资源不存在；或控制面未启用时访问 `/api/*` |
| 400 | 校验失败（非法 URL、feed 验证失败、非法状态等） |
| 503 | 配置未加载等暂时不可用（与 ready 语义一致时） |

错误体遵循 FastAPI：`{ "detail": "..." }` 或结构化 detail。写操作成功响应与 MCP 工具同类结构化结果（如 `AddSourceResult` 字段）。

### 4.4 现有公开端点

- `GET /health/live`、`GET /health/ready`：不变，公开。
- `GET /status`：保留**精简公开**响应（源计数、pending、failed、`last_tick_at`、`config_error`），供现有脚本使用；详细源列表与事件审计一律走 `/api/*`。

## 5. 数据层

**不修改 SQLite schema。** 现有 `feed_state` / `entries` / `events` / `deliveries` / `scrape_cache` 足够。

### Repository 增量

- `list_events(status?, source_url?, q?, limit, cursor?) -> (items, next_cursor)`
- `get_event_detail(event_id) -> EventBundle + deliveries`（或等价结构）
- `status_breakdown() -> dict[str, int]`（按 `EventStatus` 计数值）

既有 `list_feed_states`、`list_failed_events`、`retry_failed_event`、`status_counts` 复用。

### Control 增量

- 扩展 `StatusService.get_status`（或薄封装）：并入 `last_tick_at`（来自 Scheduler）、`status_breakdown`。
- 事件查询视图模型：将 `source_url` 映射为配置中的 `source_id`（配置中已删除的源可仅返回 url）。
- 所有写 API 直接调用现有 Source / Filter / Recovery / Destination 方法，不复制业务分支。

## 6. Web UI

### 6.1 技术选型

- **React + Vite** 单页应用，源码目录 `web/`。
- 生产：Dockerfile **多阶段构建**（Node 构建 → 复制 `dist` 进 Python 镜像），由 FastAPI 托管静态资源与 SPA fallback。
- 本地开发：`vite` dev server 将 `/api` 代理到 `http://127.0.0.1:8000`。
- 仍为 **Compose 单服务** `feedsentry`；不增加独立前端容器。

### 6.2 路由与页面

| 路由 | 职责 |
| --- | --- |
| `/login` | 输入 Bearer Token，探测 `GET /api/status`，成功后写入 `localStorage` |
| `/` | 概览：计数卡片、`last_tick_at`、config_error、失败源摘要、刷新 |
| `/sources` | 源表格 + 启停 / 检查 / 删除 |
| `/sources/add` | Tab：直接 RSS；或页面发现 → 选候选 → 订阅 |
| `/events` | 事件流：状态筛选、源筛选、搜索、分页 |
| `/events/:id` | 详情：决策审计、投递、失败则 Retry |
| `/filter` | 查看/编辑 `filter.goal`；文案说明仅影响新条目 |
| `/settings` | 更换/清除 token；测试通知 |

### 6.3 视觉（克制深色运维风）

- 近黑背景、深 surface、细边框；冷青 accent；绿/琥珀/红状态色。
- 系统字体；表格密度优先；状态用圆点徽章；危险操作需确认。
- 无大 hero、无重图表库；第一版计数用数字卡片。
- 文案默认中文。

### 6.4 前端行为

- Token 键名如 `feedsentry_token`，仅 `localStorage`，不进 URL/cookie。
- 任意 API 401 → 清 token 并回登录页。
- 写操作成功 toast；失败展示 `detail`。
- 进入页面拉取数据；概览提供手动刷新；第一版不做 WebSocket。

## 7. 安全

- 鉴权密钥与 MCP 共用：`FEEDSENTRY_MCP_TOKEN`。
- 未设置 token：不挂载 `/api/*` 与 SPA；公开 health/精简 status 与后台管线照常。
- 日志与错误响应不得回显 token 或集成密钥。
- 加源 / discover 继续走 `FeedValidator` SSRF 与大小/超时限制。
- 同域托管 SPA，默认不开放宽松 CORS。
- 正式部署继续本机绑定 + 反向代理 HTTPS；代理必须透传 `Authorization`。
- 第一版不单独提供 `FEEDSENTRY_WEB_ENABLED`；Web 与 MCP 控制面同开同关。若未来需要「只 MCP 不开 Web」再加开关。

## 8. 部署

```text
Dockerfile:
  stage web  — node:22，npm ci && npm run build → dist
  stage app  — 现有 Python 运行镜像，COPY dist 到如 /app/web/dist
  FastAPI    — StaticFiles + SPA fallback（排除 /api、/mcp、/health、/status）
```

- 挂载：`./config:/config`（可写目录）、`./data` 不变。
- 容器端口 8000；主机映射策略不变。
- 验证：`docker build`、`docker compose config -q`、登录后调用 `/api/status`。

## 9. 测试

| 层 | 要求 |
| --- | --- |
| Repository | `list_events` 过滤/分页/cursor；`get_event_detail`；`status_breakdown` |
| API | 无 token 访问 `/api` 不可用；错 token 401；正确 token 下只读与写操作 happy path |
| Control | 新查询映射单测；写路径复用既有 control 测试 |
| 回归 | 全量 pytest、ruff check/format；MCP 行为不回归 |
| 前端 | 第一版不强制自动化 E2E；手动清单：登录、源表、添加流、事件筛选、retry、测通知 |
| 容器 | 多阶段 `docker build` 成功 |

## 10. 非目标（第一版）

- 第二套状态机、任务级筛选/通知、多租户。
- WebSocket 实时推送；独立 Prometheus `/metrics`（可用 status 分布代替）。
- 响应中返回完整 scrape markdown 或 `raw_json`。
- 独立 API token、OAuth、RBAC、登录会话 cookie。
- 亮色主题切换、重型图表、i18n 框架。
- 浏览器作为 MCP Streamable HTTP 客户端直连 `/mcp`。
- 数据库 schema 迁移（本功能不需要）。

## 11. 成功标准

1. 配置 token 后，浏览器可完成巡检、源管理、事件审计与 MCP 对齐的写操作。
2. 未配置 token 时行为与当前一致（无 Web/API 控制面），管线不受影响。
3. MCP 工具语义与测试保持通过；HTTP 与 MCP 共用 control。
4. 仍为单 Compose 服务、单镜像多阶段构建。
5. 新增行为有后端测试；文档说明控制台启用方式与 token 安全。

## 12. 实现顺序建议

1. Repository 查询 + control 视图 + REST 鉴权与路由 + API 测试。
2. React 壳：登录、鉴权 fetch、深色布局、概览与源列表。
3. 事件列表/详情、filter、写操作表单与确认。
4. Dockerfile 多阶段与 SPA 挂载；README / AGENTS 补充。
5. 全量 pytest、ruff、docker build 验证。
