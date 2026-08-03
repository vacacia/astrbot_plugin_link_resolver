from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from acabot.runtime.plugin_protocol import RuntimePluginContext
from acabot.types import ActionType, EventSource, StandardEvent
from plugins.link_resolver import Plugin


DOUYIN_LINK = "https://v.douyin.com/Yy-s1QWwVzk/ 04/08 v@S.lp wFu:/"


class RecordingGateway:
    def __init__(self) -> None:
        self._self_id = "9000"
        self.sent_actions = []
        self.call_api_actions = []

    async def send(self, action):
        self.sent_actions.append(action)
        if action.action_type == ActionType.SEND_MESSAGE:
            for part in action.payload.get("parts", []):
                if str(part.get("kind", "") or "") not in {"image", "file", "audio", "video"}:
                    continue
                file_ref = str(part.get("source_uri", "") or "")
                if file_ref and not file_ref.startswith(("http://", "https://", "file://", "base64://", "data:")):
                    if not Path(file_ref).exists():
                        raise FileNotFoundError(file_ref)
        return {"status": "ok", "data": {"message_id": "1"}}

    async def call_api(self, action: str, params: dict):
        self.call_api_actions.append((action, params))
        if action == "get_group_member_info":
            return {"status": "ok", "data": {"shut_up_timestamp": 0}}
        return {"status": "ok", "data": {"message_id": "fwd-1"}}

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def on_event(self, handler):
        self._handler = handler


@pytest.mark.asyncio
async def test_douyin_real_link_fake_event_reaches_video_send(tmp_path) -> None:
    plugin = Plugin()
    gateway = RecordingGateway()
    runtime = RuntimePluginContext(
        plugin_id="link_resolver",
        plugin_config={
            "general_settings": {
                "reaction_emoji_enabled": True,
                "error_notify_mode": "报错",
                "max_video_size_mb": 200,
            },
            "douyin_settings": {
                "merge_send": False,
                "max_media": 1,
            },
        },
        data_dir=tmp_path,
        gateway=gateway,
        tool_broker=MagicMock(),
    )
    await plugin.setup(runtime)

    ctx = SimpleNamespace(
        run=SimpleNamespace(run_id="run-douyin-live"),
        event=StandardEvent(
            event_id="evt-douyin-live",
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
                "message": [{"type": "text", "data": {"text": DOUYIN_LINK}}],
            },
        ),
        actions=[],
        metadata={},
    )

    on_event_hook = next(hook for point, hook in plugin.hooks() if point.value == "on_event")
    result = await on_event_hook.handle(ctx)

    assert result.action == "skip_agent"
    assert gateway.call_api_actions
    assert any(plan.action.action_type == ActionType.REACTION for plan in ctx.actions)
    assert any(
        plan.action.action_type == ActionType.SEND_MESSAGE
        and any(part.get("kind") == "video" for part in plan.action.payload.get("parts", []))
        for plan in ctx.actions
    )
