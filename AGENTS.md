# FeedSentry 项目指南

## 项目目标

FeedSentry 是一个自托管 Python 服务：轮询 RSS/RSSHub 源，通过
OpenAI 兼容模型筛选新条目，按需用 Firecrawl 补充正文，并通过 Apprise
或原生 Telegram 发送已接受条目的摘要。

服务以可持久化的 SQLite 事件状态机为核心。不要将持久化状态替换为内存
队列，也不要绕过幂等性检查。

## 本地开发

在仓库根目录使用 `uv`：

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

新增行为必须有测试。先运行聚焦测试，再在提交前运行完整测试集。保持
`src/feedsentry/` 与 `tests/` 的模块结构对应。

代码按层分子包：`core/`（domain、database、repository）、`config/`（models、
store）、`clients/`（ai、feeds、feed_validation、rsshub、firecrawl、apprise、
telegram、qq）、`pipeline/`（ingestion、processor、polling、scheduler）、
`interfaces/`（api、mcp、auth、control、serialize）。`app.py` 是组合根。
新增模块放入对应子包，并在 `tests/` 下保持相同目录结构。

## 重要架构约束

- `config.yaml` 是全局筛选规则、信息源列表和通知目标的唯一来源；SQLite
  保存源基线、条目、事件状态、抓取缓存和投递记录。
- 系统只有一条全局管线。不要重新引入任务、监控器、任务级筛选规则或
  任务级通知目标。
- 每个源 URL 的第一次成功轮询只建立基线，不能为历史条目发送通知。
- 每个新条目只创建一个事件，并使用发现时的全局筛选规则快照处理。
- 保留事件状态机和仓储层的受保护状态转换。重试必须从失败阶段恢复，不能
  重复已经完成的 AI、Firecrawl 或通知调用。
- 投递记录必须按事件和通知目的地保持幂等。
- 所有外部客户端保持异步且可注入，便于确定性测试。
- RSS 文本、抓取正文和模型输出都属于不可信输入。不得移除 AI 提示注入防护。
- SQLite 中读取出的时间戳必须保持 UTC-aware。

## 配置与密钥

- 不要提交 `config.yaml`、`.env`、API key、Bot token、通知 URL、数据库文件
  或运行日志。
- `config.example.yaml` 说明了必需的环境变量。
- MCP Token 只通过 `FEEDSENTRY_MCP_TOKEN` 环境变量提供，不得写入 `config.yaml`
  或日志。RSSHub 实例配置在 `integrations.rsshub.base_url`。
- `AI_BASE_URL` 是以 `/v1` 结尾的基址；应用会自动追加
  `/chat/completions`。
- Firecrawl 与 Apprise 部署在宿主机上；容器通过
  `host.docker.internal` 访问，Compose 已配置 host gateway 映射。
- Apprise 目的地配置在 Apprise 本身中；FeedSentry 仅通过
  `destination.apprise_key` 引用目的地。

## 正式部署

当前正式服务仅绑定在服务器本机：

```text
服务器：38.246.112.19
目录：/home/anya/feedsentry
Compose 服务：feedsentry
主机端口：127.0.0.1:18003 -> 容器端口 8000
数据文件：/home/anya/feedsentry/data/feedsentry.db
```

通过 SSH 管理：

```bash
ssh 38.246.112.19
cd /home/anya/feedsentry
docker compose up -d --build
docker compose ps
docker compose logs -f feedsentry
curl http://127.0.0.1:18003/health/ready
curl http://127.0.0.1:18003/status
```

容器以 UID `10001` 的非 root 用户运行。宿主机 `data/` 目录必须允许该 UID
写入；不要为了规避权限错误而将服务改为 root 运行。

修改正式配置前，使用以下命令验证 Compose，且不要输出密钥：

```bash
docker compose config -q
```

## 验证要求

- 本地：完整 pytest、Ruff lint、Ruff format 检查。
- 容器：`docker build -t feedsentry:test .` 与 `docker compose config -q`。
- 运行时：检查 `/health/live`、`/health/ready` 和 `/status`。
- 新源第一次轮询必须静默建立基线。投递测试使用可控的新条目或一次性源。
- 真实通知测试前，确认目标 Apprise 配置 key，并在消息中标记为测试。

## 数据库兼容

当前全局管线 schema 不兼容旧的 monitor-based schema，也不提供自动迁移。
升级到该版本时删除或移走旧数据库，让每个源重新静默建立基线。

后续改动保持聚焦。避免无关依赖升级和大范围格式化。若未来再次修改数据库
schema，必须先明确是新库重建还是提供升级路径。

## MCP 控制面

- MCP handler 只能调用控制服务，不能直接编辑 YAML、操作 ORM、执行 Shell。
- 配置写入必须保留环境变量占位符，并通过同目录临时文件原子替换。
- Compose 挂载可写的 `config/` 目录，不要把单个配置文件作为只读 bind mount。
- 来源立即检查与 Scheduler 共用每来源锁；失败事件只能从已保存失败阶段恢复。
- RSSHub Radar 使用配置实例的 `/api/radar/rules`，不复制路由规则到项目。

## Web 运维控制台

- 控制台是 MCP 的第二入口，不是第二套管线：HTTP `/api/*` 与 MCP 都只调用 Control Services。
- 未设置 `FEEDSENTRY_MCP_TOKEN` 时不挂载 `/api/*` 与 SPA；公开 health/精简 status 与后台管线照常。
- Docker 多阶段构建：Node 构建 `web/` → 复制到 `/app/web/dist`；运行时
  `FEEDSENTRY_WEB_DIST=/app/web/dist`（`app.py` 亦支持该环境变量覆盖）。
- 不要重新引入任务、监控器或任务级筛选/通知配置。
