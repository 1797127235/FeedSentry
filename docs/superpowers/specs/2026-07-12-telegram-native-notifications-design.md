# Telegram 原生推送设计

## 目标

将 FeedSentry 的 Telegram 推送从通用 Apprise 纯文本投递迁移为原生 Telegram Bot API 投递，使每条即时通知具备稳定的中文排版、原文链接按钮、长度控制和可恢复的投递记录。

本期不包括 RSS 源管理面板、每日摘要、已读/静音交互或筛选策略调整。

## 范围与边界

Telegram 是一等渠道。Apprise 保留为邮件、Bark、Discord 等通用渠道的投递方式；现有 `default` Apprise 目标不会被删除。Telegram Bot Token 和 Chat ID 仅通过部署环境变量提供，不写入 Git、配置示例或日志。

## 架构

事件处理器不再直接拼接可见通知正文，而是构造结构化通知：标题、摘要、来源 URL 和内部判定理由。内部理由只保存到数据库，永不进入 Telegram 正文。

新增 `TelegramMessageRenderer` 负责把结构化通知转换为 Telegram 支持的 HTML；新增 `TelegramNotifier` 负责调用 Bot API。成功投递后保存 Telegram `message_id`，失败则沿用现有事件重试状态机和 delivery 幂等记录。

```text
MonitorEvent + Entry
  -> Notification payload
  -> TelegramMessageRenderer
  -> TelegramNotifier
  -> Telegram Bot API
  -> Delivery record with response summary and message id
```

## 消息格式

每条通知按以下布局发送：

```text
来源域名

中文标题
一到两句中文摘要。

[阅读原文]
```

标题使用加粗 HTML，链接使用 Telegram inline keyboard 的 URL 按钮。标题、摘要与来源名称均进行 HTML 转义；只接受 `http` 和 `https` 链接。正文以 UTF-16 code units 计算，超出 Telegram 4096 限制时保留标题和链接，截断摘要并以省略号结尾。

## 配置

目标渠道新增 `kind` 字段。`kind: telegram` 使用 Telegram 原生适配器，并读取 `TELEGRAM_BOT_TOKEN` 与 `TELEGRAM_CHAT_ID`；`kind: apprise` 继续通过 `apprise_key` 调用 Apprise。一个监控器只能配置一种目标类型。

## 可靠性

Telegram API 请求使用既有共享 HTTP 客户端。对超时、网络错误、429 和 5xx 响应抛出异常，由现有事件状态机安排重试；4xx 配置错误保留错误信息并遵循既有最大重试次数。每次发送均使用当前 `DeliveryRow` 的幂等键，已成功的 delivery 不重复发送。

## 验证

单元测试覆盖 HTML 转义、来源显示、URL 校验、UTF-16 长度截断和 URL 按钮。集成测试模拟 Telegram API 的成功、429 和超时响应，验证请求载荷与恢复行为。部署后发送一条受控测试消息，检查中文、特殊字符、长链接和按钮渲染。
