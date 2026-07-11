# FeedSentry 模块说明

本文说明 `src/feedsentry/` 下各模块的职责、边界和调用关系。

## 整体流程

```text
config.yaml
    -> ConfigManager
    -> Scheduler
    -> IngestionService -> FeedClient -> RSS/RSSHub
    -> Repository -> SQLite
    -> EventProcessor -> AIClient -> OpenAI 兼容模型
                      -> FirecrawlClient -> Firecrawl
                      -> AppriseClient -> Apprise
    -> FastAPI API (/health/*, /status)
```

每个监控器和 RSS 源第一次成功拉取时只建立基线。后续出现的新条目才会创建
事件并进入处理状态机，因此不会向用户推送历史文章。

## 文件清单

| 文件 | 职责 |
| --- | --- |
| `__init__.py` | 包版本号。 |
| `app.py` | 应用入口、依赖组装、FastAPI 生命周期和 CLI 启动。 |
| `api.py` | 存活、就绪和状态 HTTP 接口。 |
| `config.py` | YAML 配置读取、环境变量插值、Pydantic 校验和热重载。 |
| `domain.py` | 领域枚举、事件状态机、AI 决策模型、重试时间和目标哈希。 |
| `database.py` | SQLAlchemy 表定义、SQLite 引擎、WAL、外键和 UTC 时间类型。 |
| `repository.py` | 所有 SQLite 事务、幂等写入、状态转换、恢复与统计。 |
| `feeds.py` | RSS/Atom 条目规范化、稳定指纹和条件 HTTP 拉取。 |
| `ingestion.py` | 冷启动基线、新条目识别和源拉取失败退避。 |
| `ai.py` | OpenAI 兼容聊天补全请求、结构化筛选与总结。 |
| `firecrawl.py` | Firecrawl Markdown 抓取客户端。 |
| `apprise.py` | Apprise 通知客户端。 |
| `processor.py` | 单个事件的可恢复处理状态机。 |
| `scheduler.py` | 定时轮询源、处理到期事件和配置热重载。 |
| `logging.py` | JSON 日志和敏感字段脱敏。 |

## 启动与依赖组装

### `app.py`

`create_app()` 是应用工厂。FastAPI 启动时依次完成以下工作：

1. 通过 `ConfigManager` 读取 `config.yaml`。
2. 创建并初始化 SQLite 数据库。
3. 调用 `Repository.recover_in_progress()`，把进程异常中断的事件放回可重试状态。
4. 创建共享的 `httpx.AsyncClient`，超时为 20 秒并允许重定向。
5. 组装 Feed、AI、Firecrawl、Apprise、采集器、处理器和调度器。
6. 将这些对象放进不可变的 `AppServices`，供 API 路由访问。
7. 在后台启动 `Scheduler.run()`。

关闭时按相反顺序停止调度器、关闭 HTTP 客户端并释放数据库连接。
`run()` 是 `feedsentry` 命令行入口，读取 `FEEDSENTRY_CONFIG`，默认使用
`config.yaml`。

### `api.py`

- `GET /health/live`：进程存活检查，不依赖数据库。
- `GET /health/ready`：配置已加载且 SQLite 可连接时返回成功。
- `GET /status`：返回监控器数量、最近轮询时间、待处理事件数、失败事件数和
  最近配置错误。

这些接口不返回配置内容、密钥、RSS 正文或模型输出。

## 配置与领域模型

### `config.py`

定义所有配置 Pydantic 模型，例如 `AIConfig`、`MonitorConfig` 和
`DestinationConfig`。支持以下环境变量写法：

```yaml
api_key: ${AI_API_KEY}
optional_key: ${FIRECRAWL_API_KEY:-}
```

`ConfigManager.reload_if_changed()` 通过文件修改时间进行热重载。新配置无效或
文件暂时不可读取时，继续使用上一次有效配置，并且只保存不含敏感值的通用错误
信息。

### `domain.py`

这里没有数据库或网络代码，只保存稳定的领域规则：

- `DecisionAction`：`discard`、`accept`、`fetch`。
- `EventStatus`：发现、筛选、抓取、总结、投递、重试、完成等状态。
- `ScreeningDecision`：模型返回的结构化决策；接受时必须给出摘要。
- `assert_transition()`：阻止非法状态跳转。
- `next_retry_at()`：1 分钟、5 分钟、30 分钟、2 小时的重试退避。
- `goal_hash()`：对目标文本归一化后计算稳定哈希。

## 数据库与仓储

### `database.py`

定义五张 SQLite 表：

- `feed_state`：每个监控器和 RSS 源的 ETag、基线、下次轮询时间和失败计数。
- `entries`：规范化后的 RSS 条目，按源 URL 与外部 ID 去重。
- `monitor_events`：一个监控器针对一个条目的处理状态。
- `scrape_cache`：按文章 URL 缓存 Firecrawl Markdown。
- `deliveries`：投递记录和幂等键。

`Database` 启用 WAL 与每个连接的 SQLite 外键检查。`UTCDateTime` 负责把
SQLite 中不带时区的存储值恢复为 UTC-aware `datetime`，避免调度比较时出现
时区错误。

### `repository.py`

`Repository` 是所有持久化读写的唯一入口。重要规则：

- 条目、事件和投递都使用 SQLite 冲突处理实现幂等。
- `transition_event()` 同时匹配当前持久化状态，防止两个工作者重复推进同一事件。
- `recover_in_progress()` 将中断在筛选、抓取、总结或投递中的事件恢复为
  `retry_wait`，并记录应恢复的阶段。
- `schedule_event_retry()` 在第五次失败后标记事件为 `failed`。
- `create_delivery()` 以 `event_id:apprise_key` 的 SHA-256 生成幂等键。

记录类型如 `EntryRecord`、`EventRecord`、`EventBundle` 和 `StatusCounts` 都是
不可变 dataclass，供上层使用而不直接暴露 ORM 行对象。

## RSS 采集

### `feeds.py`

`FeedClient` 使用条件请求头 `If-None-Match` 与 `If-Modified-Since` 拉取源，
在收到 `304 Not Modified` 时不重新解析内容。

`normalize_feed()` 使用 `feedparser` 解析 RSS/Atom，并生成不可变的
`NormalizedEntry`：

- 外部 ID 优先使用原始 `id`，其次是规范化链接，最后使用标题、摘要和发布时间
  的哈希。
- 内容哈希由标题、摘要、链接、作者和发布时间计算。
- 原始条目以稳定 JSON 保存，便于诊断和将来扩展。

### `ingestion.py`

`IngestionService.poll_monitor_source()` 负责一条监控器源：

1. 读取该源的 ETag、修改时间和基线状态。
2. 调用 `FeedClient`。
3. 首次成功时写入条目并建立基线，不创建事件。
4. 后续成功时，只为基线之后首次见到的条目创建事件。
5. HTTP 失败时记录错误与源级退避，延迟为 1、5、30、120 分钟。

## AI、抓取与通知客户端

### `ai.py`

`AIClient` 调用 OpenAI 兼容的 `/chat/completions` 接口，温度固定为 0。

- `screen()` 根据监控目标和 RSS 摘要返回丢弃、接受或抓取。
- `summarize()` 根据 Firecrawl Markdown 返回丢弃或接受，禁止再次请求抓取。
- 模型内容必须是 JSON，并通过 `ScreeningDecision` 校验。
- 系统提示明确要求将 RSS 和网页内容视为不可信数据，忽略其中嵌入的命令。

### `firecrawl.py`

`FirecrawlClient.scrape()` 调用 `/v1/scrape`，请求 Markdown 与主内容。API key
为空时不发送授权头。响应缺少非空 Markdown 时抛出错误，让事件进入重试。

### `apprise.py`

`AppriseClient.notify()` 调用 `/notify/{key}`，发送标题、正文和 `info` 类型。
目的地的真实 Telegram、邮件或其他 URL 只存储在 Apprise 中；FeedSentry 只保存
配置 key。

## 事件处理与调度

### `processor.py`

`EventProcessor.process_event()` 是可恢复状态机。它每次状态改变后都会重新读取
数据库，避免基于过期内存继续处理。

主要路径如下：

```text
discovered -> screening
screening -> filtered | fetching | delivery_pending
fetching -> summarizing
summarizing -> filtered | delivery_pending
delivery_pending -> delivering
delivering -> delivered
任一外部阶段失败 -> retry_wait -> 原失败阶段
```

处理器会优先读取抓取缓存，投递前创建幂等 `DeliveryRecord`。因此 Apprise 临时
失败时，只会重试投递，不会再次调用模型或 Firecrawl。

### `scheduler.py`

`Scheduler` 每个 tick 按以下顺序执行：

1. 尝试配置热重载。
2. 查找所有已启用且到期的监控器源。
3. 单独轮询每个源；一个源失败不会停止其他源。
4. 查找最多 20 个到期事件。
5. 顺序处理事件，保持 SQLite 行为可预测。

`run()` 使用可取消的 `asyncio.Event`，`stop()` 让 FastAPI 生命周期可以干净
关闭后台循环。

## 日志

### `logging.py`

`JsonFormatter` 输出 JSON 格式日志，可包含监控器、条目、事件、阶段和尝试次数
等上下文字段。日志会脱敏 `api_key=`、`token=`、`password=` 和 `secret=` 后的
值，并限制消息长度。不要记录监控目标、RSS 正文、抓取 Markdown、模型完整响应
或完整配置对象。
