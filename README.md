# FeedSentry

FeedSentry 是一个自托管的个人 RSS 智能筛选推送服务。

它持续轮询所有已启用的 RSS、Atom 和 RSSHub 信息源，使用一套全局 AI 关注点筛选新条目，并将接受的内容通过 Apprise 或原生 Telegram 推送。

AstrBot 可以通过 MCP 管理信息源、修改全局关注点、查看状态和恢复失败事件。Claude、Codex 等其他 MCP 客户端也可以连接同一服务。

## 工作方式

FeedSentry 只有一条全局处理管线，不存在任务、监控器或任务级配置：

```text
所有启用的信息源
    -> 轮询 RSS / RSSHub
    -> 保存新条目与事件
    -> 使用全局关注点进行 AI 筛选
    -> 必要时由 Firecrawl 补充正文
    -> Apprise 或 Telegram 推送
```

每个信息源第一次成功抓取时只建立静默基线，不会推送源中已有的历史条目。之后发现的新条目才进入 AI 筛选和推送流程。

## 快速启动

要求：Docker、Docker Compose，以及可用的 OpenAI 兼容接口和 Apprise 服务。Firecrawl 仅在 AI 判断 RSS 摘要不足时使用。

```bash
git clone https://github.com/1797127235/FeedSentry.git
cd FeedSentry
mkdir -p config data
cp config.example.yaml config/config.yaml
```

在项目根目录创建 `.env`：

```dotenv
AI_BASE_URL=https://你的模型服务/v1
AI_API_KEY=你的模型密钥
AI_MODEL=模型名称

FIRECRAWL_BASE_URL=http://host.docker.internal:3002
FIRECRAWL_API_KEY=
APPRISE_BASE_URL=http://host.docker.internal:8000

FEEDSENTRY_PORT=8000
```

`AI_BASE_URL` 必须是以 `/v1` 结尾的基址。FeedSentry 会自动追加 `/chat/completions`。

启动并检查服务：

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8000/health/live
curl http://127.0.0.1:8000/health/ready
curl http://127.0.0.1:8000/status
docker compose logs -f feedsentry
```

## 主配置

`config/config.yaml` 是信息源列表、全局关注点和通知目标的唯一配置来源。

```yaml
integrations:
  firecrawl:
    base_url: ${FIRECRAWL_BASE_URL}
    api_key: ${FIRECRAWL_API_KEY:-}
  apprise:
    base_url: ${APPRISE_BASE_URL}
  rsshub:
    base_url: https://rsshub.antest.cc.cd

ai:
  base_url: ${AI_BASE_URL}
  api_key: ${AI_API_KEY}
  model: ${AI_MODEL}

storage:
  path: ./data/feedsentry.db

filter:
  goal: 只推送重要产品发布、安全更新和破坏性变更

sources:
  - id: example-feed
    kind: feed
    url: https://example.com/feed.xml
    enabled: false

destination:
  apprise_key: telegram
```

`destination.apprise_key` 引用 Apprise 中已经配置好的目的地。FeedSentry 不保存 Apprise 通知 URL。

也可以使用原生 Telegram。此时需要在 `integrations.telegram` 中配置 Bot Token 和 Chat ID，并将 `destination.kind` 设置为 `telegram`。密钥应使用环境变量占位符。

也可以通过 NapCat / Lagrange 等 OneBot v11 协议端接入 QQ 机器人。在 NapCat WebUI 开启「网络配置 → HTTP 服务」（默认端口 3000），然后在 `integrations.qq` 配置服务地址，并将 `destination.kind` 设置为 `qq`：

```yaml
integrations:
  qq:
    base_url: ${NAPCAT_BASE_URL}
    access_token: ${NAPCAT_TOKEN:-}
    target_type: group          # private（私聊 user_id）或 group（群 group_id）
    target_id: "123456789"
```

`access_token` 与 NapCat HTTP 服务里设置的 token 一致；未设 token 时留空。Token 必须通过 `${NAPCAT_TOKEN}` 环境变量提供，不要写入 `config.yaml`。FeedSentry 只通过 httpx 调用 `/send_private_msg` 或 `/send_group_msg`，不依赖 websocket。

## 信息源类型

直接 RSS 或 Atom 源：

```yaml
- id: v2ex-latest
  kind: feed
  url: https://www.v2ex.com/index.xml
  enabled: true
```

RSSHub 源保存平台页面和 Radar 匹配出的路由。最终订阅 URL 由 `integrations.rsshub.base_url` 与 `route` 组合生成：

```yaml
- id: bilibili-user-video-946974
  kind: rsshub
  page_url: https://space.bilibili.com/946974
  route: /bilibili/user/video/946974
  enabled: true
```

通常无需手工填写 RSSHub 路由。Claude 或 Codex 可以先调用 `discover_feeds(page_url)`，再使用返回的候选 ID 调用 `subscribe_feed(candidate_id)`。

## 状态与重试

SQLite 保存源基线、条目、事件状态、抓取缓存和投递记录。默认示例数据库位于 `data/feedsentry.db`。

处理失败后按 1 分钟、5 分钟、30 分钟和 2 小时退避重试。最终失败的事件仍保存在数据库中，可以通过 MCP 查看并手动恢复。

通知记录按事件和目的地保持幂等。已保存的抓取正文会复用；已经写入成功记录的通知不会在失败恢复时再次发送。

通知采用至少一次投递语义：若目的地已接收消息、但服务在成功记录写入 SQLite
前退出，恢复后可能再次发送。`storage.path` 是启动期配置，运行中修改会拒绝热加载并提示重启；其他 AI、集成、来源、关注点和通知目标配置可热加载。

## 启用 MCP 与运维控制台

未设置 `FEEDSENTRY_MCP_TOKEN` 时，MCP、`/api/*` 与 Web 运维控制台全部关闭；访问 `/mcp` 或 `/api/*` 会返回 404。后台 RSS 轮询管线不受影响。

先生成一个随机 Token：

```bash
openssl rand -hex 32
```

将结果写入 `.env`，不要写入 `config.yaml`：

```dotenv
FEEDSENTRY_MCP_TOKEN=生成的64位十六进制字符串
FEEDSENTRY_MCP_ALLOWED_HOSTS=feedsentry.example.com
```

重新创建容器：

```bash
docker compose up -d --build
```

### Web 运维控制台

Docker 多阶段构建会把 `web/` 前端打包进镜像，并由 FastAPI 在设置 Token 后提供静态资源。

1. 浏览器打开服务根路径，例如 `https://feedsentry.example.com/` 或本机 `http://127.0.0.1:8000/`。
2. 在登录页填入与 MCP 相同的 Token（`FEEDSENTRY_MCP_TOKEN`）。
3. 控制台请求 `/api/*` 时使用：

```http
Authorization: Bearer <FEEDSENTRY_MCP_TOKEN>
```

Token 只保存在浏览器 `localStorage` 中，用于同源 `/api` 调用；不要截图、导出或提交到仓库。泄漏后应立即轮换 Token 并重建容器。

控制台与 MCP 共用同一套 Control Services，**不是**第二条业务管线：只管理信息源、关注点、事件恢复与测试通知，后台筛选与推送逻辑不变。

本地前端开发（可选）：

```bash
# 终端 A：后端，需设置 FEEDSENTRY_MCP_TOKEN
uv run feedsentry

# 终端 B：Vite 开发服务器（将 /api 代理到后端）
cd web && npm install && npm run dev
```

### MCP 端点

MCP 使用 Streamable HTTP，固定端点为：

```text
https://feedsentry.example.com/mcp
```

客户端每次请求必须携带：

```http
Authorization: Bearer <FEEDSENTRY_MCP_TOKEN>
```

`FEEDSENTRY_MCP_ALLOWED_HOSTS` 填客户端实际访问 URL 中的主机名。如果使用非标准 HTTPS 端口，需要包含端口，例如 `feedsentry.example.com:8443`。

多个允许值使用英文逗号分隔：

```dotenv
FEEDSENTRY_MCP_ALLOWED_HOSTS=feedsentry.example.com,feedsentry.example.com:8443
```

## HTTPS 反向代理

正式环境应让 FeedSentry 只监听服务器本机端口，由 Nginx、Caddy 或其他反向代理提供 HTTPS。不要将容器端口直接暴露到公网。

FeedSentry 会限制 Feed 响应大小、逐跳检查重定向目标，并在把文章 URL 交给
Firecrawl 前拒绝已解析到私网的地址。这些应用层检查不能消除 DNS rebinding，
也无法约束 Firecrawl 自身后续的 DNS 解析和重定向。正式环境应同时通过容器
网络或防火墙限制 FeedSentry 和 Firecrawl 的出站访问，至少禁止云元数据地址、
宿主机管理端口和非必要私网网段；仅为配置的 RSSHub 完整 origin 开放例外。

当前运行模型是单进程、单实例。不要同时运行多个副本共享同一个 SQLite 文件；
跨实例事件 claim 和外部通知去重不在当前持久化协议的保证范围内。

反向代理必须透传 `Authorization`（控制台 `/api/*` 与 MCP `/mcp` 都依赖 Bearer Token）。大多数代理默认会保留；若启用了额外鉴权插件，需确认 Header 未被剥离。

Nginx 示例（控制台 SPA + API + MCP）：

```nginx
location / {
    proxy_pass http://127.0.0.1:18003;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Authorization $http_authorization;
    proxy_buffering off;
    proxy_read_timeout 300s;
}
```

若只需代理 MCP，可单独配置 `location /mcp`；完整使用控制台时建议将 `/`（含 `/api`、静态资源）一并反代到同一上游。

## AstrBot 配置

AstrBot 最新版本原生支持 Streamable HTTP MCP，不需要安装 FeedSentry 插件，也不需要在 AstrBot 容器中运行 FeedSentry。

AstrBot 的通用 MCP 使用方式见 [AstrBot 官方 MCP 文档](https://docs.astrbot.app/use/mcp.html)。FeedSentry 使用远程 Streamable HTTP，因此不需要官方文档中为本地 stdio 服务准备的 `uv` 或 Node.js。

在 AstrBot WebUI 中进入“函数工具”或“MCP 服务器”页面：

1. 点击“新增服务器”。
2. 服务器名称填写 `feedsentry`。
3. 点击“Streamable HTTP 模板”。
4. 将服务器配置替换为以下 JSON。

```json
{
  "transport": "streamable_http",
  "url": "https://feedsentry.example.com/mcp",
  "headers": {
    "Authorization": "Bearer 你的FEEDSENTRY_MCP_TOKEN"
  },
  "timeout": 30,
  "sse_read_timeout": 300
}
```

保持服务器为启用状态，点击“测试连接”。连接成功后应当显示 FeedSentry 的 14 个工具，然后保存配置。

必须使用 `transport: "streamable_http"`，不能使用 `sse`。URL 必须包含末尾的 `/mcp`。

如果 AstrBot 使用 Docker 部署，它必须能够解析并访问 FeedSentry 的 HTTPS 域名。FeedSentry 的 `FEEDSENTRY_MCP_ALLOWED_HOSTS` 仍填写该 URL 中的域名，不填写 AstrBot 容器名。

AstrBot 会将 `Authorization` Header 保存到自己的 MCP 配置中。该配置包含真实 Token，不要截图、导出或提交到代码仓库。Token 泄漏后应立即生成新 Token，更新 FeedSentry 和 AstrBot，并重新创建 FeedSentry 容器。

### AstrBot 中的使用方式

连接完成后，可以直接在 AstrBot 对话中发送：

```text
帮我订阅这个 B 站用户的视频：https://space.bilibili.com/946974
```

模型应调用 `discover_feeds` 获取候选项，再调用 `subscribe_feed` 完成订阅。直接 RSS 则调用 `add_feed`。

AstrBot 使用的模型必须支持函数调用，并且当前 Agent 执行器需要启用 MCP 工具。若服务器连接成功但模型不调用工具，应先检查模型的 Function Calling 能力和 AstrBot 的工具启用状态。

## 其他 MCP 客户端

### Codex 配置

不要把 Token 直接写进 Codex 配置。先在运行 Codex 的环境中设置：

```bash
export FEEDSENTRY_MCP_TOKEN=生成的Token
```

Windows PowerShell：

```powershell
$env:FEEDSENTRY_MCP_TOKEN = "生成的Token"
```

在 Codex 的 `config.toml` 中添加：

```toml
[mcp_servers.feedsentry]
url = "https://feedsentry.example.com/mcp"
bearer_token_env_var = "FEEDSENTRY_MCP_TOKEN"
```

重启 Codex 后，确认 `feedsentry` 服务已连接并能列出工具。

### Claude Code 配置

Claude Code 当前 CLI 支持为 HTTP MCP 配置自定义 Header。先在环境变量中保存 Token，再添加用户级 MCP 服务。

Bash：

```bash
export FEEDSENTRY_MCP_TOKEN=生成的Token
claude mcp add --transport http --scope user feedsentry \
  https://feedsentry.example.com/mcp \
  --header "Authorization: Bearer $FEEDSENTRY_MCP_TOKEN"
```

Windows PowerShell：

```powershell
$env:FEEDSENTRY_MCP_TOKEN = "生成的Token"
claude mcp add --transport http --scope user feedsentry `
  https://feedsentry.example.com/mcp `
  --header "Authorization: Bearer $env:FEEDSENTRY_MCP_TOKEN"
```

检查连接：

```bash
claude mcp get feedsentry
claude mcp list
```

这条命令会将展开后的 Header 保存到 Claude Code 的用户级配置中。该配置包含真实 Token，不要提交或分享。

Claude Desktop 或其他 MCP 客户端使用相同的连接参数：Streamable HTTP URL 加 `Authorization: Bearer <token>`。具体配置入口取决于客户端版本。

## MCP 工具

FeedSentry 暴露以下 14 个受控工具：

| 工具 | 作用 |
| --- | --- |
| `discover_feeds` | 使用配置的 RSSHub Radar 为平台页面发现候选订阅 |
| `subscribe_feed` | 订阅 `discover_feeds` 返回的候选项，并静默建立基线 |
| `add_feed` | 验证并添加直接 RSS 或 Atom URL，并静默建立基线 |
| `list_sources` | 查看所有信息源及当前健康状态 |
| `set_source_enabled` | 启用或停用指定信息源 |
| `remove_source` | 从配置删除信息源，保留历史处理记录 |
| `check_source_now` | 立即检查指定信息源 |
| `get_filter_goal` | 查看全局 AI 关注点 |
| `set_filter_goal` | 修改未来新条目使用的全局 AI 关注点 |
| `append_filter_goal` | 向全局 AI 关注点追加一段内容（换行分隔，幂等） |
| `get_status` | 查看系统、来源和事件状态 |
| `list_failed_events` | 列出最终失败的事件 |
| `retry_failed_event` | 从记录的失败阶段恢复指定事件 |
| `test_destination` | 发送带有 TEST 标记的测试通知 |

MCP 只能调用这些受控能力。它不能执行 Shell、直接操作数据库、任意编辑 YAML，也不能读取配置中的密钥。

## MCP 使用示例

连接成功后，可以直接对 Claude 或 Codex 表达目标：

```text
帮我订阅这个 B 站用户的视频：https://space.bilibili.com/946974
```

客户端应先调用 `discover_feeds`，从候选项中选择“UP 主投稿”，再调用 `subscribe_feed`。FeedSentry 会验证最终 RSS、保存配置并静默建立基线。

直接 RSS 可以这样处理：

```text
帮我添加这个 RSS：https://www.v2ex.com/index.xml
```

客户端调用 `add_feed` 即可，不需要经过 RSSHub Radar。

## 配置文件挂载要求

MCP 修改配置时会在 `config.yaml` 同目录创建临时文件，完成校验、`fsync` 后原子替换原文件。因此 Compose 必须挂载整个可写目录：

```yaml
volumes:
  - ./config:/config
```

不要只绑定单个只读的 `config.yaml` 文件，否则容器内无法可靠地原子更新配置。

## 本地开发

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

更详细的内部设计见 [架构文档](docs/architecture.md)。
