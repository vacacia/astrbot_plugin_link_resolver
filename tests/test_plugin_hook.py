from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from acabot.runtime.contracts.session_config import ComputerPolicyDecision
from acabot.runtime.plugin_protocol import RuntimePluginContext
from acabot.runtime.tool_broker import ToolResult
from acabot.types import EventSource, StandardEvent
from plugins.link_resolver import Plugin
from plugins.link_resolver.compat.runtime_adapter import cleanup_outbound_cache


class FakeGateway:
    def __init__(self) -> None:
        self._self_id = "9000"

    async def send(self, action):
        return {"status": "ok", "data": {"message_id": "1"}}

    async def call_api(self, action: str, params: dict):
        return {"status": "ok", "data": params}

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def on_event(self, handler):
        self._handler = handler


@pytest.mark.asyncio
async def test_setup_builds_legacy_resolver(tmp_path) -> None:
    plugin = Plugin()
    runtime = RuntimePluginContext(
        plugin_id="link_resolver",
        plugin_config={},
        data_dir=tmp_path,
        gateway=FakeGateway(),
        tool_broker=MagicMock(),
    )

    await plugin.setup(runtime)

    assert plugin._resolver is not None
    assert plugin.hooks()


@pytest.mark.asyncio
async def test_on_event_hook_trims_visible_skills_before_workspace_build(tmp_path) -> None:
    plugin = Plugin()
    runtime = RuntimePluginContext(
        plugin_id="link_resolver",
        plugin_config={},
        data_dir=tmp_path,
        gateway=FakeGateway(),
        tool_broker=MagicMock(),
    )
    await plugin.setup(runtime)

    async def fake_handler(event):
        event.set_result(event.plain_result("handled"))

    plugin._resolver.handle_douyin = fake_handler

    ctx = SimpleNamespace(
        run=SimpleNamespace(run_id="run-on-event"),
        event=StandardEvent(
            event_id="evt-on-event",
            event_type="message",
            platform="qq",
            timestamp=1,
            source=EventSource(platform="qq", message_type="group", user_id="42", group_id="1097619430"),
            segments=[],
            platform_message_id="123",
            sender_nickname="tester",
            sender_role=None,
            raw_event={"message_id": 123, "message": [{"type": "text", "data": {"text": "https://v.douyin.com/Yy-s1QWwVzk/"}}]},
        ),
        actions=[],
        metadata={},
        agent=SimpleNamespace(skills=["a", "b", "c"]),
        computer_policy_decision=ComputerPolicyDecision(visible_skills=["a", "b", "c"]),
    )

    on_event_hook = next(hook for point, hook in plugin.hooks() if point.value == "on_event")
    result = await on_event_hook.handle(ctx)

    assert result.action == "skip_agent"
    assert ctx.computer_policy_decision.visible_skills == []
    assert ctx.agent.skills == []


@pytest.mark.asyncio
async def test_hook_routes_bilibili_text_and_skips_agent(tmp_path) -> None:
    plugin = Plugin()
    runtime = RuntimePluginContext(
        plugin_id="link_resolver",
        plugin_config={},
        data_dir=tmp_path,
        gateway=FakeGateway(),
        tool_broker=MagicMock(),
    )
    await plugin.setup(runtime)

    async def fake_handler(event):
        event.set_result(event.plain_result("handled"))

    plugin._resolver.handle_bili_video = fake_handler

    ctx = SimpleNamespace(
        run=SimpleNamespace(run_id="run-1"),
        event=StandardEvent(
            event_id="evt-1",
            event_type="message",
            platform="qq",
            timestamp=1,
            source=EventSource(platform="qq", message_type="group", user_id="42", group_id="777"),
            segments=[],
            platform_message_id="123",
            sender_nickname="tester",
            sender_role=None,
            raw_event={"message_id": 123, "message": [{"type": "text", "data": {"text": "BV1xx411c7mD"}}]},
        ),
        actions=[],
        metadata={},
    )

    on_event_hook = next(hook for point, hook in plugin.hooks() if point.value == "on_event")
    result = await on_event_hook.handle(ctx)

    assert result.action == "skip_agent"
    assert len(ctx.actions) == 1
    assert ctx.actions[0].action.payload["text"] == "handled"


@pytest.mark.asyncio
async def test_hook_routes_weibo_text_and_skips_agent(tmp_path) -> None:
    plugin = Plugin()
    runtime = RuntimePluginContext(
        plugin_id="link_resolver",
        plugin_config={"enable_platforms": ["微博"]},
        data_dir=tmp_path,
        gateway=FakeGateway(),
        tool_broker=MagicMock(),
    )
    await plugin.setup(runtime)

    async def fake_handler(event):
        event.set_result(event.plain_result("handled weibo"))

    plugin._resolver.handle_weibo = fake_handler

    ctx = SimpleNamespace(
        run=SimpleNamespace(run_id="run-weibo"),
        event=StandardEvent(
            event_id="evt-weibo",
            event_type="message",
            platform="qq",
            timestamp=1,
            source=EventSource(platform="qq", message_type="group", user_id="42", group_id="777"),
            segments=[],
            platform_message_id="123",
            sender_nickname="tester",
            sender_role=None,
            raw_event={
                "message_id": 123,
                "message": [{"type": "text", "data": {"text": "https://weibo.com/1234567890/AbCdEfGhI"}}],
            },
        ),
        actions=[],
        metadata={},
        agent=SimpleNamespace(skills=["a", "b"]),
        computer_policy_decision=ComputerPolicyDecision(visible_skills=["a", "b"]),
    )

    on_event_hook = next(hook for point, hook in plugin.hooks() if point.value == "on_event")
    result = await on_event_hook.handle(ctx)

    assert ctx.computer_policy_decision.visible_skills == []
    assert ctx.agent.skills == []
    assert result.action == "skip_agent"
    assert len(ctx.actions) == 1
    assert ctx.actions[0].action.payload["text"] == "handled weibo"


@pytest.mark.asyncio
async def test_runtime_tool_registers_link_resolver(tmp_path) -> None:
    plugin = Plugin()
    runtime = RuntimePluginContext(
        plugin_id="link_resolver",
        plugin_config={},
        data_dir=tmp_path,
        gateway=FakeGateway(),
        tool_broker=MagicMock(),
    )
    await plugin.setup(runtime)

    tools = plugin.runtime_tools()

    assert [item.spec.name for item in tools] == ["link_resolver"]
    assert tools[0].spec.parameters["required"] == ["text"]
    assert tools[0].metadata["suppress_tool_notice"] is True


def test_cleanup_outbound_cache_removes_only_expired_entries(tmp_path) -> None:
    outbound = tmp_path / "outbound"
    old_dir = outbound / "old-run"
    recent_dir = outbound / "recent-run"
    old_file = outbound / "old-file.jpg"
    recent_file = outbound / "recent-file.jpg"
    old_dir.mkdir(parents=True)
    recent_dir.mkdir(parents=True)
    old_file.write_text("old", encoding="utf-8")
    recent_file.write_text("recent", encoding="utf-8")

    now = 10_000.0
    old_mtime = now - 500.0
    recent_mtime = now - 10.0
    os.utime(old_dir, (old_mtime, old_mtime))
    os.utime(old_file, (old_mtime, old_mtime))
    os.utime(recent_dir, (recent_mtime, recent_mtime))
    os.utime(recent_file, (recent_mtime, recent_mtime))

    removed = cleanup_outbound_cache(outbound, ttl_seconds=100, now=now)

    assert removed == 2
    assert not old_dir.exists()
    assert not old_file.exists()
    assert recent_dir.exists()
    assert recent_file.exists()


@pytest.mark.asyncio
async def test_runtime_tool_dispatches_with_temporary_config(tmp_path) -> None:
    plugin = Plugin()
    runtime = RuntimePluginContext(
        plugin_id="link_resolver",
        plugin_config={
            "enable_platforms": ["B站"],
            "douyin_settings": {"max_media": 1, "merge_send": False, "summary_mode": "文字摘要"},
        },
        data_dir=tmp_path,
        gateway=FakeGateway(),
        tool_broker=MagicMock(),
    )
    await plugin.setup(runtime)

    async def fake_handler(event):
        assert plugin._resolver.douyin_enabled is True
        assert plugin._resolver.douyin_max_media == 7
        event.set_result(event.plain_result("handled by tool"))

    plugin._resolver.handle_douyin = fake_handler
    registration = plugin.runtime_tools()[0]
    ctx = SimpleNamespace(
        run_id="run-tool",
        target=EventSource(platform="qq", message_type="group", user_id="42", group_id="777"),
        metadata={"event_id": "evt-tool", "event_timestamp": 1},
    )

    result = await registration.handler(
        {
            "text": "请解析 https://v.douyin.com/Yy-s1QWwVzk/",
            "platform": "douyin",
            "max_media": 7,
        },
        ctx,
    )

    assert isinstance(result, ToolResult)
    assert result.raw["handled"] is True
    assert result.raw["action_count"] == 1
    assert result.user_actions[0].action.payload["text"] == "handled by tool"
    assert result.user_actions[0].metadata["suppresses_default_reply"] is True
    assert plugin._resolver.douyin_enabled is False
    assert plugin._resolver.douyin_max_media == 1


@pytest.mark.asyncio
async def test_runtime_tool_reports_failure_when_handler_generates_no_output(tmp_path) -> None:
    plugin = Plugin()
    runtime = RuntimePluginContext(
        plugin_id="link_resolver",
        plugin_config={"enable_platforms": ["抖音"]},
        data_dir=tmp_path,
        gateway=FakeGateway(),
        tool_broker=MagicMock(),
    )
    await plugin.setup(runtime)

    async def fake_handler(event):
        _ = event
        return None

    plugin._resolver.handle_douyin = fake_handler
    registration = plugin.runtime_tools()[0]
    ctx = SimpleNamespace(
        run_id="run-tool-no-output",
        target=EventSource(platform="qq", message_type="group", user_id="42", group_id="777"),
        metadata={"event_id": "evt-tool-no-output", "event_timestamp": 1},
    )

    result = await registration.handler(
        {
            "text": "请解析 https://v.douyin.com/Yy-s1QWwVzk/",
            "platform": "douyin",
        },
        ctx,
    )

    assert result.raw["handled"] is False
    assert result.raw["action_count"] == 0
    assert result.raw["error"] == "no output generated"
    assert "没有生成可发送内容" in result.llm_content
    assert result.user_actions == []
