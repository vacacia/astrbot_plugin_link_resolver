from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from acabot.runtime.contracts.context import PlannedAction
from acabot.types import ActionType, EventSource, StandardEvent
from plugins.link_resolver.compat.runtime_adapter import CompatEvent, DeliverySink
from plugins.link_resolver.compat.astrbot.api.event import MessageChain
from plugins.link_resolver.compat.astrbot.api.message_components import Node, Nodes, Plain, Video


class FakeGateway:
    def __init__(self) -> None:
        self._self_id = "9000"
        self.call_api = AsyncMock(return_value={"status": "ok", "data": {"message_id": "fwd-1"}})


@pytest.fixture
def base_ctx() -> SimpleNamespace:
    event = StandardEvent(
        event_id="evt-1",
        event_type="message",
        platform="qq",
        timestamp=1,
        source=EventSource(platform="qq", message_type="group", user_id="42", group_id="777"),
        segments=[{"type": "text", "data": {"text": "hello"}}],
        platform_message_id="123",
        sender_nickname="tester",
        sender_role=None,
        raw_event={"message_id": 123, "message": [{"type": "text", "data": {"text": "hello"}}]},
    )
    return SimpleNamespace(
        run=SimpleNamespace(run_id="run-1"),
        event=event,
        actions=[],
        metadata={},
    )


def test_plain_result_collects_text_reply(base_ctx: SimpleNamespace) -> None:
    sink = DeliverySink(ctx=base_ctx, gateway=FakeGateway())
    event = CompatEvent(ctx=base_ctx, sink=sink)

    event.set_result(event.plain_result("done"))

    assert len(base_ctx.actions) == 1
    action = base_ctx.actions[0]
    assert isinstance(action, PlannedAction)
    assert action.action.action_type == ActionType.SEND_TEXT
    assert action.action.payload == {"text": "done"}


@pytest.mark.asyncio
async def test_forward_nodes_create_planned_action(base_ctx: SimpleNamespace) -> None:
    gateway = FakeGateway()
    sink = DeliverySink(ctx=base_ctx, gateway=gateway)
    event = CompatEvent(ctx=base_ctx, sink=sink)

    nodes = Nodes([Node(uin="42", name="tester", content=[Plain("hello")])])
    await event.send(MessageChain([nodes]))

    gateway.call_api.assert_not_awaited()
    assert len(base_ctx.actions) == 1
    action = base_ctx.actions[0]
    assert action.action.action_type == ActionType.SEND_FORWARD
    assert action.action.payload["messages"][0]["type"] == "node"
    assert action.thread_content == "hello"


@pytest.mark.asyncio
async def test_media_chain_creates_outbound_message_action(base_ctx: SimpleNamespace) -> None:
    gateway = FakeGateway()
    sink = DeliverySink(ctx=base_ctx, gateway=gateway)
    event = CompatEvent(ctx=base_ctx, sink=sink)

    await event.send(MessageChain([Video.fromFileSystem("/tmp/video.mp4")]))

    gateway.call_api.assert_not_awaited()
    assert len(base_ctx.actions) == 1
    action = base_ctx.actions[0]
    assert action.action.action_type == ActionType.SEND_MESSAGE
    assert action.action.payload["parts"] == [
        {
            "kind": "video",
            "source_uri": "/tmp/video.mp4",
            "metadata": {"c": 2},
        },
    ]
