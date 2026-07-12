# Telegram Native Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add first-class Telegram Bot API delivery with a compact, safe Chinese message template while keeping Apprise destinations working.

**Architecture:** The event processor creates a `Notification` from stored event and feed data, then dispatches it according to a typed destination. Telegram delivery uses a pure HTML renderer and Bot API client; Apprise remains a generic adapter. Existing delivery rows keep their idempotency mechanism by storing `telegram:<chat-id>` as the destination key.

**Tech Stack:** Python 3.12+, Pydantic, httpx, SQLAlchemy, pytest, respx, Telegram Bot API.

---

### Task 1: Add typed destinations and notification payload

**Files:**
- Modify: `src/feedsentry/domain.py`
- Modify: `src/feedsentry/config.py`
- Modify: `tests/test_config.py`
- Modify: `tests/conftest.py`

- [x] **Step 1: Write failing configuration coverage**

```python
def test_load_config_supports_telegram_destination(tmp_path, monkeypatch):
    monkeypatch.setenv("FIRECRAWL_URL", "http://firecrawl:3002")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    path = tmp_path / "config.yaml"
    content = VALID_CONFIG.replace(
        "  apprise:\n    base_url: http://apprise:8000\n",
        "  apprise:\n    base_url: http://apprise:8000\n  telegram:\n    bot_token: ${TELEGRAM_BOT_TOKEN}\n    chat_id: ${TELEGRAM_CHAT_ID}\n",
    ).replace("destination: {apprise_key: telegram}", "destination: {kind: telegram}")
    write_config(path, content)
    config = load_config(path)
    assert config.monitors[0].destination.kind == "telegram"
    assert config.integrations.telegram.chat_id == "123"
```

- [x] **Step 2: Run the test and verify it fails**

Run: `uv run --extra dev pytest tests/test_config.py::test_load_config_supports_telegram_destination -v`
Expected: FAIL because the Telegram integration and destination kind do not yet exist.

- [x] **Step 3: Implement the smallest compatible configuration change**

Add optional `TelegramConfig(bot_token, chat_id)` to `IntegrationsConfig`. Change `DestinationConfig` to a `kind: Literal["apprise", "telegram"]` with default `apprise`; require `apprise_key` only for `kind: apprise`. In `AppConfig` validation, reject `kind: telegram` when `integrations.telegram` is missing. Add immutable `Notification(title, summary, source_url, link)` to `domain.py`.

- [x] **Step 4: Verify the configuration boundary**

Run: `uv run --extra dev pytest tests/test_config.py tests/test_domain.py -v`
Expected: PASS.

- [x] **Step 5: Commit**

Run: `git add src/feedsentry/domain.py src/feedsentry/config.py tests/test_config.py tests/conftest.py && git commit -m "feat: add typed notification destinations"`

### Task 2: Implement the Telegram HTML renderer and client

**Files:**
- Create: `src/feedsentry/telegram.py`
- Create: `tests/test_telegram.py`

- [ ] **Step 1: Write failing renderer and request tests**

```python
def test_render_telegram_message_escapes_content_and_adds_url_button():
    message = render_telegram_message(Notification(
        title="模型 <V2>", summary="支持 A & B。",
        source_url="https://example.com/feed", link="https://example.com/post",
    ))
    assert "<b>模型 &lt;V2&gt;</b>" in message.text
    assert message.reply_markup["inline_keyboard"][0][0]["url"] == "https://example.com/post"

@respx.mock
async def test_telegram_notifier_posts_html_message():
    route = respx.post("https://api.telegram.org/bottoken/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})
    )
    async with httpx.AsyncClient() as http:
        result = await TelegramNotifier(http, "token", "123").notify(notification)
    assert result == "telegram_message_id=42"
    assert json.loads(route.calls[0].request.content)["parse_mode"] == "HTML"
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `uv run --extra dev pytest tests/test_telegram.py -v`
Expected: FAIL with `ModuleNotFoundError: feedsentry.telegram`.

- [ ] **Step 3: Implement renderer and Bot API adapter**

Implement `render_telegram_message()` using `html.escape()`, a hostname-derived source label, HTTP(S)-only links, `<b>` for title, and `reply_markup.inline_keyboard` containing one `阅读原文` URL button. Count UTF-16 units and truncate the summary so text stays within 4096 units while retaining title and button.

Implement `TelegramNotifier.notify()` as `POST https://api.telegram.org/bot{token}/sendMessage` with `chat_id`, `text`, `parse_mode: HTML`, `disable_web_page_preview: true`, and `reply_markup`. Raise on HTTP errors, `ok: false`, missing `message_id`, or invalid links; return `telegram_message_id=<integer>` only.

- [ ] **Step 4: Verify renderer and transport behavior**

Run: `uv run --extra dev pytest tests/test_telegram.py -v`
Expected: PASS for escaping, invalid links, UTF-16 truncation, successful requests, HTTP failures, and API failures.

- [ ] **Step 5: Commit**

Run: `git add src/feedsentry/telegram.py tests/test_telegram.py && git commit -m "feat: add Telegram notification client"`

### Task 3: Dispatch event delivery by destination kind

**Files:**
- Modify: `src/feedsentry/processor.py`
- Modify: `src/feedsentry/app.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_processor.py`
- Modify: `tests/test_end_to_end.py`

- [ ] **Step 1: Write a failing Telegram delivery test**

```python
async def test_telegram_delivery_hides_internal_reason(processor_fixture):
    destination = DestinationConfig(kind="telegram")
    telegram = FakeTelegramNotifier()
    dispatcher = NotificationDispatcher(apprise=fixture.apprise, telegram=telegram)
    fixture.processor = EventProcessor(
        fixture.repository, fixture.ai, fixture.firecrawl, dispatcher, lambda _: destination
    )
    fixture.ai.screen_result = ScreeningDecision(
        action=DecisionAction.ACCEPT, reason="internal", title="标题", summary="摘要"
    )
    await fixture.processor.process_event(fixture.event_id)
    assert telegram.notifications[0].notification.summary == "摘要"
    assert "internal" not in telegram.notifications[0].text
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `uv run --extra dev pytest tests/test_processor.py::test_telegram_delivery_hides_internal_reason -v`
Expected: FAIL because `EventProcessor` currently only accepts Apprise dependencies.

- [ ] **Step 3: Implement typed notifier dispatch**

Add an Apprise adapter that preserves the current generic output for `kind: apprise`. Change `EventProcessor` to resolve `DestinationConfig`, construct `Notification` from `EventBundle.entry` and persisted output fields, and dispatch to the selected adapter. Use `telegram:<chat-id>` as the existing delivery table key; preserve existing Apprise keys unchanged. The Telegram path must never include `decision_reason` in visible content. Wire both adapters in `create_app()` through `config_manager.current` so future reloads apply to unprocessed events.

- [ ] **Step 4: Verify delivery and retry regressions**

Run: `uv run --extra dev pytest tests/test_processor.py tests/test_end_to_end.py tests/test_repository.py -v`
Expected: PASS; failed Telegram delivery resumes from `DELIVERING` without rerunning AI, and Apprise tests remain unchanged.

- [ ] **Step 5: Commit**

Run: `git add src/feedsentry/processor.py src/feedsentry/app.py tests/conftest.py tests/test_processor.py tests/test_end_to_end.py && git commit -m "feat: route notifications to Telegram"`

### Task 4: Finalize output contract and deploy

**Files:**
- Modify: `src/feedsentry/ai.py`
- Modify: `tests/test_ai.py`
- Modify: `config.example.yaml`
- Modify: `compose.yaml`
- Modify: `README.md`
- Modify on server only: `/home/anya/feedsentry/config.yaml`

- [ ] **Step 1: Write a failing output-language assertion**

```python
def test_prompts_require_compact_simplified_chinese_output():
    assert "Simplified Chinese" in SCREEN_SYSTEM_PROMPT
    assert "Simplified Chinese" in FINAL_SYSTEM_PROMPT
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `uv run --extra dev pytest tests/test_ai.py::test_prompts_require_compact_simplified_chinese_output -v`
Expected: FAIL because accepted output is not currently constrained to Chinese.

- [ ] **Step 3: Implement output and deployment configuration**

Require accepted AI decisions to return a concise Simplified Chinese title and one- or two-sentence Simplified Chinese summary; keep `reason` as internal data. Document `integrations.telegram`, pass `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` through Compose, and switch the live monitor destination to `kind: telegram` only after a controlled test passes. Do not commit real secrets or the server configuration.

- [ ] **Step 4: Run complete verification**

Run: `uv run --extra dev pytest && uv run --extra dev ruff check src tests`
Expected: all tests and lint checks pass.

- [ ] **Step 5: Deploy and verify the real message**

Build the committed source on the server, set the two Telegram values only in its deployment environment, recreate the container, and send one controlled notification containing Chinese, `<`, `&`, and a long URL. Verify bold title, a URL button, no visible `Reason`, `/health/ready` success, `config_error: null`, and zero failed events. Restore the backed-up server config and use `kind: apprise` if this live check fails.

- [ ] **Step 6: Commit**

Run: `git add src/feedsentry/ai.py tests/test_ai.py config.example.yaml compose.yaml README.md && git commit -m "docs: configure Telegram notifications"`
