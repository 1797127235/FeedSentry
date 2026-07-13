# FeedSentry MCP 控制面设计

日期：2026-07-13

## 1. 目标

FeedSentry 仍是个人 RSS 智能筛选推送服务：持续拉取信息源，按全局关注规则
筛选新条目，按需抓取正文，并把接受的内容推送到全局通知目标。

本次增加一个受控的 MCP 管理入口，使 Claude、Codex 等客户端可以完成来源
订阅、规则修改、状态查看和故障恢复。MCP 断开或无人使用时，后台管线继续
独立运行。

## 2. 用户结果

用户可以通过 Claude/Codex 完成以下操作：

- 从普通 RSS/Atom URL 添加订阅。
- 从平台页面 URL 发现 RSSHub 候选 Feed，并订阅符合意图的候选项。
- 列出、启用、停用和删除来源。
- 查看和修改全局 AI 关注规则。
- 查看系统状态、来源异常和失败事件。
- 立即检查指定来源、重试失败事件、测试通知目标。

平台页面订阅的固定流程为：

```text
用户表达订阅意图
-> Claude/Codex 获得页面 URL
-> discover_feeds(page_url)
-> FeedSentry 使用 RSSHub Radar 规则生成候选
-> Claude/Codex 根据用户意图选择候选
-> subscribe_feed(candidate_id)
-> FeedSentry 验证 Feed、写入配置、静默建立基线
```

用户直接提供 RSS/Atom URL 时，调用 `add_feed(url)`，不经过 Radar 发现。

## 3. 系统结构

```text
Claude / Codex
      |
      | MCP Streamable HTTP + Bearer Token
      v
MCP Adapter
      |
      v
Control Services
  |- SourceService
  |- FilterService
  |- StatusService
  `- RecoveryService
      |
      +-> ConfigStore -> config.yaml
      +-> RSSHubClient -> https://rsshub.antest.cc.cd
      +-> Repository -> SQLite
      `-> PollCoordinator -> IngestionService

现有后台管线：Scheduler -> RSS -> AI -> Firecrawl -> 通知
```

MCP handler 只负责参数和结果转换。配置校验、原子写入、来源检查和状态恢复
全部由控制服务完成。MCP 不直接编辑 YAML，不直接操作 ORM，也不执行 Shell。

## 4. RSSHub 集成

配置新增：

```yaml
integrations:
  rsshub:
    base_url: https://rsshub.antest.cc.cd
```

`RSSHubClient` 使用该实例的结构化接口：

```text
GET /api/radar/rules
```

规则按页面域名与路径匹配，`source` 模式中的参数替换到 `target`，生成一个或
多个候选 Feed。候选项包含标题、页面 URL、RSSHub 路由和最终 Feed URL。

`discover_feeds` 返回带有效期的签名 `candidate_id`。签名内容包含候选路由、
页面 URL、最终 Feed URL 和过期时间，使用 MCP Token 做 HMAC；服务端不需要
保存临时候选状态。`subscribe_feed` 验证签名和有效期后再请求 Feed。

Radar 规则在进程内缓存，并使用有限 TTL 刷新。刷新失败时可以继续使用最近
一次有效规则；没有有效缓存时返回明确的 RSSHub 不可用错误。

## 5. 来源模型

配置明确区分直接 Feed 与 RSSHub 来源：

```yaml
sources:
  - id: v2ex
    kind: feed
    url: https://www.v2ex.com/index.xml
    enabled: true

  - id: bilibili-946974-video
    kind: rsshub
    page_url: https://space.bilibili.com/946974
    route: /bilibili/user/video/946974
    enabled: true
```

`id` 是添加来源时生成的稳定、小写标识，用于所有 MCP 管理操作。它不能因
RSSHub 基址变化而改变。直接来源的抓取地址是 `url`；RSSHub 来源的抓取地址
由当前 `integrations.rsshub.base_url` 与保存的 `route` 组合生成。

RSSHub 来源保存 `page_url` 供用户识别和重新发现，保存具体 `route` 供稳定
运行。配置不保存候选签名，也不复制 Radar 规则。来源标题等远端元数据写入
SQLite 状态，不作为调度所需配置。

## 6. Feed 验证和静默基线

`add_feed` 与 `subscribe_feed` 最终进入同一个 Feed 添加流程：

1. 规范化并检查 URL 是否已存在。
2. 使用受限 HTTP 请求拉取 Feed。
3. 使用 `feedparser` 验证 RSS/Atom 版本并规范化当前条目。
4. 原子写入来源配置。
5. 把当前条目写入 SQLite，并标记该来源已经初始化。
6. 返回来源信息和 `baseline_initialized=true`。

当前条目只建立基线，不创建事件，不调用 AI，不发送通知。配置写入成功但
SQLite 基线失败时，返回 `baseline_pending`；调度器下一次成功轮询仍按新源
语义静默建立基线。

URL 与重定向目标都执行安全检查。响应使用超时和大小限制，避免 MCP 来源
添加工具变成无界 HTTP 请求入口。允许访问的私有 RSSHub 主机由明确配置决定。

## 7. 配置存储

`config.yaml` 继续是来源、关注规则和通知目标的唯一事实来源。

`ConfigStore` 对写操作使用进程内异步锁，并执行：

```text
读取原始 YAML
-> 只修改目标结构字段
-> 展开环境变量后执行 AppConfig 完整校验
-> 写入同目录临时文件
-> flush + fsync
-> os.replace 原子替换
-> 刷新 ConfigManager 当前快照
```

必须基于原始 YAML 修改，不能序列化已经展开环境变量的 `AppConfig`，否则会
把 API Key 或 Token 写回配置文件。Compose 中 `config.yaml` 挂载改为可写。
不在仓库根目录生成配置备份。

删除来源只修改配置，SQLite 中的基线、条目、事件和投递记录保留。重新添加
同一 Feed URL 时沿用原有基线，不把历史条目识别为新内容。修改关注规则只
影响之后创建的事件，现有事件继续使用自己的规则快照。

## 8. MCP 工具

### 来源

- `discover_feeds(page_url)`：通过 RSSHub Radar 返回候选 Feed。
- `subscribe_feed(candidate_id)`：验证候选并静默添加来源。
- `add_feed(url)`：验证并添加直接 RSS/Atom 来源。
- `list_sources()`：返回来源配置和最近运行状态。
- `set_source_enabled(source_id, enabled)`：启用或停用来源，不清除基线。
- `remove_source(source_id)`：删除来源配置，保留历史状态。
- `check_source_now(source_id)`：通过共享轮询协调器立即检查来源。

### 关注规则

- `get_filter_goal()`：读取全局关注规则。
- `set_filter_goal(goal)`：验证并更新规则，只影响新事件。

### 状态与恢复

- `get_status()`：返回系统和来源健康摘要。
- `list_failed_events()`：返回失败阶段、错误和尝试次数。
- `retry_failed_event(event_id)`：从保存的失败阶段恢复事件。
- `test_destination()`：发送标题和正文都明确标记为测试的通知。

工具返回结构化对象，不把完整配置、密钥、原始抓取正文或模型原始响应返回
给客户端。重复添加、重复启停和重复恢复采用幂等语义。

## 9. 立即检查与失败恢复

Scheduler 和 MCP 共用 `PollCoordinator` 的每来源异步锁，避免 `check_source_now`
与定时轮询同时处理同一来源。条目和事件唯一约束继续提供持久化幂等保护。

事件达到最终失败状态时保留失败阶段。`retry_failed_event` 只能把 `failed`
事件恢复到该阶段并设为立即到期，不能重跑已经完成的前置 AI、Firecrawl 或
通知步骤。

当前实现会在事件进入 `failed` 时清空已有的 `resume_stage`，新实现改为保留
最后失败阶段。该变化复用现有列，不修改 SQLite schema，也不需要重建数据库。

## 10. MCP 传输与认证

FeedSentry 在同一个 ASGI 服务中提供 Streamable HTTP MCP 端点：

```text
POST https://<FeedSentry 域名>/mcp
Authorization: Bearer <token>
```

服务端 Token 来自环境变量：

```dotenv
FEEDSENTRY_MCP_TOKEN=<64 字符随机值>
```

Token 不写入 `config.yaml`、SQLite 或日志。服务端使用恒定时间比较。缺失、
格式错误或不匹配返回 `401`。没有配置 Token 时不启用 `/mcp`，但 RSS 后台
服务仍可运行。HTTPS 由现有反向代理终止。

MCP 请求限制正文大小和并发数；日志只记录工具名、结果类型、来源 ID、事件
ID 和耗时，不记录 Token、关注规则全文或 Feed 内容。

## 11. 错误语义

- 页面没有 Radar 候选：返回 `no_feed_candidates`。
- RSSHub 不可达且无缓存规则：返回 `rsshub_unavailable`。
- candidate 过期或签名不匹配：返回 `invalid_candidate`。
- Feed 不是有效 RSS/Atom：返回 `invalid_feed`。
- 来源已存在：返回现有来源，`created=false`。
- 配置并发更新：锁内重读并应用变更，不覆盖其他成功写入。
- 配置校验或原子替换失败：原文件保持不变。
- 来源检查失败：更新该来源退避状态，不阻塞其他来源。

## 12. 验证

测试覆盖：Radar 域名和路径匹配、candidate 签名与过期、Feed 验证、静默
基线、重复添加、配置原子写入和密钥占位符保留、并发写入、MCP Token、来源
立即检查互斥、失败阶段恢复，以及完整的 MCP 订阅到后续新条目通知流程。

部署验证包括容器构建、Compose 配置、HTTPS `/mcp` 鉴权、三个健康接口，
以及使用可控 Feed 验证首次静默和后续新条目通知。
