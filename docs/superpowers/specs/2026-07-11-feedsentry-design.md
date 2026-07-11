# FeedSentry 设计说明

日期：2026-07-11

## 1. 产品定位

FeedSentry 是一个个人自托管的智能消息监控服务。用户通过单个 YAML 配置文件定义多个监控任务；服务持续读取 RSS/Atom 或 RSSHub Feed，对新增条目进行 AI 初筛，在信息不足时通过 Firecrawl 抓取原文，生成摘要和关联理由，并通过 Apprise API 即时推送。

FeedSentry 本身不是对话机器人。它是一个持续运行、可恢复、可被其他智能体调用的后台服务。未来可以增加 MCP 控制层，让其他智能体安全地创建、修改、暂停和查询监控任务。

## 2. 第一版目标

- 通过一个 `config.yaml` 定义全局集成和多个监控任务。
- 支持原生 RSS/Atom 与 RSSHub 生成的 Feed URL。
- 使用 OpenAI 兼容接口进行自然语言目标驱动的相关性判断。
- Feed 信息不足时，仅通过 Firecrawl 抓取条目的原始链接。
- 对通过筛选的条目生成标题、简短摘要、关联理由和原文链接。
- 通过已有的 Apprise API 实例即时推送。
- 使用 SQLite 保存条目、处理阶段、抓取缓存、AI 结果和投递记录。
- 外部服务或进程故障时不丢消息，并避免重复分析和重复投递。
- 为未来 MCP 控制层保留稳定的领域服务接口。

## 3. 非目标

第一版不包含：

- Web 管理后台或对话式交互界面。
- 普通网页变化监控、Webhook 或专用平台连接器。
- Firecrawl 关联链接抓取、站内爬取或主动搜索。
- 定时摘要和日报；通过筛选后立即推送。
- 多用户、权限、租户和计费。
- 多模型路由或模型自动回退。
- FeedSentry 自带的 RSSHub、Firecrawl 或 Apprise 部署。
- MCP Server 实现。

## 4. 系统边界

外部依赖均使用用户已有的实例：

- RSSHub：将平台内容转换为 RSS/Atom，FeedSentry 最终只消费 Feed URL。
- Firecrawl：按需读取原始链接并返回清洗后的正文。
- OpenAI 兼容模型接口：执行初筛、最终判断和摘要。
- Apprise API：将统一消息适配到最终通知渠道。

FeedSentry 采用 Python 单进程服务。调度器、处理管道和轻量 HTTP 健康接口运行在同一进程中。核心组件包括：

1. `ConfigLoader`：环境变量插值、Pydantic 校验、热重载和敏感字段脱敏。
2. `Scheduler`：根据每个监控任务的间隔确定到期任务。
3. `FeedCollector`：条件请求、Feed 解析、规范化和条目去重。
4. `EventProcessor`：驱动单条消息的持久化状态机。
5. `AIClient`：调用 OpenAI 兼容接口并验证结构化响应。
6. `FirecrawlClient`：按需抓取原文，API Key 可选。
7. `AppriseClient`：按配置 Key 发送统一通知。
8. `Repository`：SQLite 事务、幂等约束和任务恢复。

这些组件通过明确接口通信，不直接依赖 YAML 的原始字典结构。

## 5. 配置模型

配置文件是用户意图的事实来源。SQLite 不反向生成配置。

```yaml
integrations:
  firecrawl:
    base_url: ${FIRECRAWL_BASE_URL}
    api_key: ${FIRECRAWL_API_KEY:-}
  apprise:
    base_url: ${APPRISE_BASE_URL}

ai:
  base_url: ${AI_BASE_URL}
  api_key: ${AI_API_KEY}
  model: ${AI_MODEL}

storage:
  path: ${FEEDSENTRY_DB_PATH:-./data/feedsentry.db}

monitors:
  - id: ai-open-source
    name: AI 开源动态
    goal: |
      关注重要 AI Agent 开源项目的新版本、关键能力和生态变化。
      忽略普通教程和营销内容。
    interval: 10m
    sources:
      - https://rsshub.example.com/github/repos/example/project/releases
      - https://example.com/feed.xml
    destination:
      apprise_key: personal-telegram
    enabled: true
```

配置规则：

- 允许直接填写值，也支持 `${NAME}` 和 `${NAME:-default}` 环境变量语法。
- 环境变量解析后的值优先于 YAML 中的默认值。
- `monitor.id` 必须唯一且稳定，后续修改名称不改变历史关联。
- 每个监控任务包含自然语言目标、一个或多个来源、检查间隔和一个 Apprise 配置 Key。
- Firecrawl `api_key` 可为空；为空时不发送 `Authorization` 请求头，有值时发送 Bearer Token。
- 进程启动时配置无效则立即失败并报告具体字段。
- 运行期间在每个调度周期前检查配置文件修改时间。新配置完整验证成功后原子替换内存快照；验证失败时继续使用最后一次有效配置并记录错误。
- 所有日志、健康信息和未来查询接口必须遮盖 API Key、Token 和密码。

## 6. Feed 采集与冷启动

- 使用 `ETag` 和 `Last-Modified` 发起条件请求。
- Feed 条目规范化为标题、摘要、链接、作者、发布时间、外部 ID 和原始载荷。
- 优先使用 Feed 的 GUID/ID；缺失时使用规范化链接；两者都缺失时使用稳定内容指纹。
- 同一来源条目由数据库唯一约束最终去重。
- 新来源第一次成功抓取时只建立基线，不为已有条目创建通知事件，避免首次启动产生历史消息风暴。
- 后续抓取只为基线之后出现的新条目创建事件。
- 单个 Feed 失败只更新该来源的错误和退避状态，不阻塞同一监控任务的其他来源。

## 7. AI 处理流程

每个新条目针对引用它的监控任务分别处理，因为不同任务拥有不同自然语言目标。

第一次 AI 调用使用 Feed 自带的标题、摘要和监控目标，返回严格结构化结果：

- `discard`：不相关，记录理由后结束。
- `accept`：信息已经足够，返回简短摘要和关联理由，进入投递。
- `fetch`：可能相关但信息不足，要求抓取原文。

`fetch` 分支调用 Firecrawl 的 scrape 能力，只抓取条目的原始链接。抓取结果按规范化 URL 缓存。随后进行第二次 AI 调用，返回 `discard` 或 `accept`，并在 `accept` 时产生：

- 标题。
- 简短摘要。
- 与监控目标的关联理由。
- 原文链接。

所有模型响应使用 Pydantic 模型验证。结构无效视为当前阶段失败并重试，不降级为未经筛选的原始推送。

## 8. 持久化状态机

事件主要状态为：

```text
DISCOVERED
  -> SCREENING
  -> FILTERED
  -> FETCHING -> SUMMARIZING
  -> DELIVERY_PENDING -> DELIVERING -> DELIVERED
```

任何可重试阶段失败时进入 `RETRY_WAIT`，同时保存失败阶段、尝试次数、错误摘要和 `next_attempt_at`。到期后从失败阶段继续，不重新执行已成功的前置阶段。

重试间隔固定为 1 分钟、5 分钟、30 分钟和 2 小时。第四次重试仍失败后标记为 `FAILED`，保留完整状态，未来可由 CLI 或 MCP 手动重试。

## 9. SQLite 数据模型

### `feed_state`

保存规范化 Feed URL、ETag、Last-Modified、最后成功时间、连续失败次数和退避时间。

### `entries`

保存来源条目的稳定 ID、规范化字段、内容指纹、首次发现时间和原始载荷。来源 ID 与外部 ID/指纹组成唯一约束。

### `monitor_events`

保存监控任务 ID、条目 ID、处理状态、处理时的目标文本及目标哈希、AI 决策、摘要、关联理由、失败阶段、尝试次数和下次重试时间。监控任务 ID 与条目 ID 组成唯一约束。

目标文本保留快照，使历史判断在配置修改后仍可解释。修改目标只影响之后的新条目；第一版不自动重新处理历史条目。

### `scrape_cache`

保存规范化原文 URL、清洗正文、内容哈希和抓取时间。相同原文被多个监控任务引用时只抓取一次。

### `deliveries`

保存事件 ID、Apprise 配置 Key、幂等键、状态、响应摘要、尝试次数和时间戳。事件与目标组成唯一约束。

SQLite 启用 WAL 模式。所有状态转换和相关结果写入同一事务，保证进程崩溃后状态可恢复。

## 10. Apprise 投递

每个通过筛选的事件创建一条独立投递记录。FeedSentry 调用已有 Apprise API 实例：

```text
POST {apprise.base_url}/notify/{monitor.destination.apprise_key}
```

消息包含标题、简短摘要、关联理由和原文链接。HTTP/API 成功响应后标记为 `DELIVERED`；失败只重试投递阶段，不再次调用 Firecrawl 或模型。

FeedSentry 负责何时发送、幂等、重试和审计；Apprise 负责渠道协议和消息发送。

## 11. 故障处理

- Feed 失败：隔离到单个来源，并进行来源级退避。
- AI 请求失败或响应无效：保留事件并从 AI 阶段重试。
- Firecrawl 失败：保留事件并从抓取阶段重试。
- Apprise 失败：保留投递并仅重试发送。
- SQLite 暂时不可写：停止领取新任务并报告服务不健康，避免在无持久化保护下继续处理。
- 配置热重载失败：继续使用最后一次有效内存快照。
- 进程重启：启动时重置超时的处理中状态，并领取到期任务继续执行。

## 12. 健康与可观察性

第一版提供轻量 HTTP 接口：

- `/health/live`：进程存活。
- `/health/ready`：配置有效、SQLite 可用。
- `/status`：监控任务数量、最后调度时间、待处理/失败事件数量；不返回敏感配置。

日志采用结构化格式，至少包含 monitor ID、entry ID、event ID、stage 和 attempt，且对外部响应正文进行长度限制和敏感信息脱敏。

## 13. 测试策略

### 单元测试

- 环境变量插值和配置校验。
- Feed URL/条目规范化和内容指纹。
- 冷启动基线和新增条目检测。
- 状态机合法转换、退避计算和幂等键。
- AI 结构化响应验证。
- 敏感字段脱敏。

### 集成测试

使用临时 SQLite 和本地假 HTTP 服务覆盖：

- Feed 条件请求与错误隔离。
- Firecrawl 无鉴权和 Bearer Token 两种调用。
- AI 的 `discard`、`accept`、`fetch` 和无效响应。
- Apprise 成功、失败与投递重试。
- 配置热重载成功及失败后保留旧配置。
- 进程重启后的任务恢复。

### 端到端测试

使用固定 Feed 和模拟的 AI、Firecrawl、Apprise 服务跑通：

```text
发现新条目 -> AI 要求抓取 -> Firecrawl 返回正文
-> AI 接受并总结 -> Apprise 接收消息 -> 状态变为 DELIVERED
```

同时验证重复运行不会重复抓取、重复分析或重复投递。

## 14. 未来 MCP 扩展

MCP 是核心服务之上的控制层，不承担后台调度。计划暴露：

- `list_monitors`
- `create_monitor`
- `update_monitor`
- `remove_monitor`
- `pause_monitor`
- `run_monitor`
- `list_events`
- `get_event`
- `retry_event`
- `test_source`
- `test_destination`

MCP 工具调用领域服务完成验证、原子写入、备份和配置重载，而不是允许智能体任意编辑 YAML 文本。用户仍可手动编辑同一个配置文件。

## 15. 第一版完成标准

- 配置至少一个监控任务后，服务能够建立 Feed 基线并持续发现新增条目。
- 新条目可以完成 AI 初筛、按需原文抓取、最终总结和 Apprise 即时推送。
- 进程可在任意处理阶段重启并继续。
- 同一条目不会对同一监控任务重复分析或重复投递。
- AI、Firecrawl、Apprise 或单个 Feed 暂时不可用时不会丢失事件。
- 错误配置不会中断当前有效监控。
- 自动化测试覆盖核心状态机及完整成功链路。
