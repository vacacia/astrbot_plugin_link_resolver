from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import shutil
import time
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote, urlparse

from acabot.runtime.contracts.context import PlannedAction
from acabot.runtime.messages.outbound import (
    OutboundAudioPart,
    OutboundFilePart,
    OutboundImagePart,
    OutboundMentionPart,
    OutboundMessage,
    OutboundMessagePart,
    OutboundTextPart,
    OutboundVideoPart,
    build_outbound_message_from_parts,
    outbound_target_from_inbound_event_source,
)
from acabot.types import Action, ActionType

from .astrbot.api.event import MessageChain
from .astrbot.api.message_components import File, Node, Nodes, Plain
from .runtime_state import get_runtime_state

logger = logging.getLogger("acabot.plugins.link_resolver")

OUTBOUND_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
OUTBOUND_CACHE_CLEANUP_INTERVAL_SECONDS = 60 * 60
_LAST_OUTBOUND_CACHE_CLEANUP_AT = 0.0


@dataclass(slots=True)
class CompatResult:
    kind: str
    payload: Any


class CompatBot:
    def __init__(self, ctx: Any, gateway: Any) -> None:
        self._ctx = ctx
        self._gateway = gateway

    async def call_action(self, action: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if args and not kwargs:
            if len(args) == 1 and isinstance(args[0], dict):
                kwargs = dict(args[0])
            else:
                raise TypeError("call_action positional arguments are not supported here")
        response = await self._gateway.call_api(action, kwargs)
        if isinstance(response, dict) and isinstance(response.get("data"), dict):
            return response["data"]
        return response or {}

    async def set_msg_emoji_like(
        self,
        *,
        message_id: int,
        emoji_id: int,
        emoji_type: str = "1",
        set: bool = True,
    ) -> dict[str, Any] | None:
        self._ctx.actions.append(
            PlannedAction(
                action_id=f"action:{self._ctx.run.run_id}:link_resolver:reaction:{message_id}",
                action=Action(
                    action_type=ActionType.REACTION,
                    target=outbound_target_from_inbound_event_source(self._ctx.event.source),
                    payload={
                        "message_id": message_id,
                        "emoji_id": emoji_id,
                        "emoji_type": emoji_type,
                        "set": set,
                    },
                ),
                thread_content=None,
                metadata={"origin": "link_resolver", "inject_into_context": False},
            )
        )
        return {"status": "planned", "message_id": message_id, "emoji_id": emoji_id}


class DeliverySink:
    def __init__(self, *, ctx: Any, gateway: Any) -> None:
        self.ctx = ctx
        self.gateway = gateway
        self._counter = 0
        self._pending: list[CompatResult] = []
        self.direct_send_count = 0

    def has_output(self) -> bool:
        return bool(self.ctx.actions) or bool(self._pending) or self.direct_send_count > 0

    def queue_result(self, result: CompatResult) -> None:
        if result.kind == "text":
            self._append_text_action(str(result.payload))
            return
        self._pending.append(result)

    async def flush(self) -> None:
        while self._pending:
            result = self._pending.pop(0)
            await self.handle_result(result)

    async def handle_result(self, result: CompatResult) -> None:
        if result.kind == "text":
            self._append_text_action(str(result.payload))
            return
        await self.send_chain(MessageChain(result.payload))

    async def send_chain(self, message: MessageChain) -> None:
        has_special = any(isinstance(item, (Node, Nodes, File)) for item in message.chain)
        if not has_special:
            await self._dispatch_segments_immediately(message.chain)
            return
        for item in message.chain:
            if isinstance(item, Node):
                await self._send_forward(Nodes([item]))
            elif isinstance(item, Nodes):
                await self._send_forward(item)
            elif isinstance(item, File):
                await self._dispatch_segments_immediately([item])
            else:
                await self._dispatch_segments_immediately([item])

    def _append_text_action(self, text: str) -> None:
        self._counter += 1
        self.ctx.actions.append(
            PlannedAction(
                action_id=f"action:{self.ctx.run.run_id}:link_resolver:{self._counter}",
                action=Action(
                    action_type=ActionType.SEND_TEXT,
                    target=outbound_target_from_inbound_event_source(self.ctx.event.source),
                    payload={"text": text},
                ),
                thread_content=text,
                metadata={"origin": "link_resolver"},
            )
        )

    def _append_segments_action(self, components: list[Any]) -> None:
        self._counter += 1
        segments = [
            self._materialize_segment_file_refs(
                self._component_to_segment(component),
                action_index=self._counter,
            )
            for component in components
        ]
        outbound = self._outbound_message_from_segments(segments)
        self.ctx.actions.append(
            PlannedAction(
                action_id=f"action:{self.ctx.run.run_id}:link_resolver:{self._counter}",
                action=Action(
                    action_type=ActionType.SEND_MESSAGE,
                    target=outbound.target,
                    payload=outbound.to_payload_json(),
                    reply_to=outbound.reply_to,
                ),
                thread_content=self._thread_content_from_segments(segments),
                metadata={"origin": "link_resolver"},
            )
        )

    async def _dispatch_segments_immediately(self, components: list[Any]) -> None:
        self._append_segments_action(components)

    async def _send_forward(self, nodes: Nodes) -> None:
        payload = await nodes.to_dict()
        self._counter += 1
        messages = self._materialize_forward_messages(
            payload["messages"],
            action_index=self._counter,
        )
        self.ctx.actions.append(
            PlannedAction(
                action_id=f"action:{self.ctx.run.run_id}:link_resolver:{self._counter}",
                action=Action(
                    action_type=ActionType.SEND_FORWARD,
                    target=outbound_target_from_inbound_event_source(self.ctx.event.source),
                    payload={"messages": messages},
                ),
                thread_content=self._thread_content_from_forward_messages(messages),
                metadata={"origin": "link_resolver"},
            )
        )

    @staticmethod
    def _component_to_segment(component: Any) -> dict[str, Any]:
        if hasattr(component, "toDict"):
            return component.toDict()
        if isinstance(component, dict):
            return component
        raise TypeError(f"unsupported component: {component!r}")

    def _outbound_message_from_segments(self, segments: list[dict[str, Any]]) -> OutboundMessage:
        reply_to, parts = self._segments_to_outbound_parts(segments)
        target = outbound_target_from_inbound_event_source(self.ctx.event.source)
        return build_outbound_message_from_parts(
            target=target,
            parts=parts,
            reply_to=reply_to,
            idempotency_key=f"{self.ctx.run.run_id}:link_resolver:{self._counter}",
            metadata={"origin": "link_resolver"},
        )

    def _segments_to_outbound_parts(
        self,
        segments: list[dict[str, Any]],
    ) -> tuple[str | None, list[OutboundMessagePart]]:
        reply_to: str | None = None
        parts: list[OutboundMessagePart] = []
        for segment in segments:
            seg_type = str(segment.get("type", "") or "").strip()
            data = dict(segment.get("data", {}) or {})
            if seg_type == "reply":
                reply_to = reply_to or str(data.get("id", "") or "").strip() or None
                continue
            part = self._segment_to_outbound_part(seg_type=seg_type, data=data)
            if part is not None:
                parts.append(part)
        return reply_to, parts

    @staticmethod
    def _segment_to_outbound_part(
        *,
        seg_type: str,
        data: dict[str, Any],
    ) -> OutboundMessagePart | None:
        if seg_type == "text":
            text = str(data.get("text", "") or "")
            return OutboundTextPart(text=text) if text else None
        if seg_type == "at":
            user_id = str(data.get("qq", "") or "").strip()
            if not user_id:
                return None
            if user_id == "all":
                return OutboundTextPart(text="@全体成员")
            return OutboundMentionPart(actor_id=f"qq:user:{user_id}", user_id=user_id)
        if seg_type == "image":
            file_ref = str(data.get("file", "") or "").strip()
            return OutboundImagePart(source_uri=file_ref) if file_ref else None
        if seg_type == "file":
            file_ref = str(data.get("file", "") or "").strip()
            if not file_ref:
                return None
            return OutboundFilePart(
                source_uri=file_ref,
                file_name=str(data.get("name", "") or "").strip(),
            )
        if seg_type == "record":
            file_ref = str(data.get("file", "") or "").strip()
            return OutboundAudioPart(source_uri=file_ref) if file_ref else None
        if seg_type == "video":
            file_ref = str(data.get("file", "") or "").strip()
            if not file_ref:
                return None
            return OutboundVideoPart(
                source_uri=file_ref,
                thumbnail_uri=str(data.get("cover", "") or "").strip(),
                metadata={
                    key: value
                    for key, value in data.items()
                    if key not in {"file", "cover"}
                },
            )
        if seg_type:
            return OutboundTextPart(text=f"[link_resolver:{seg_type}]")
        return None

    def _materialize_forward_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        action_index: int,
    ) -> list[dict[str, Any]]:
        materialized: list[dict[str, Any]] = []
        for node_index, node in enumerate(messages):
            if not isinstance(node, dict):
                materialized.append(node)
                continue
            data = dict(node.get("data", {}) or {})
            content = data.get("content", [])
            if isinstance(content, list):
                data["content"] = [
                    self._materialize_segment_file_refs(
                        segment,
                        action_index=action_index,
                        node_index=node_index,
                        segment_index=segment_index,
                    )
                    if isinstance(segment, dict)
                    else segment
                    for segment_index, segment in enumerate(content)
                ]
            updated = dict(node)
            updated["data"] = data
            materialized.append(updated)
        return materialized

    def _materialize_segment_file_refs(
        self,
        segment: dict[str, Any],
        *,
        action_index: int,
        node_index: int | None = None,
        segment_index: int | None = None,
    ) -> dict[str, Any]:
        seg_type = str(segment.get("type", "") or "")
        if seg_type not in {"image", "file", "record", "video"}:
            return segment
        data = dict(segment.get("data", {}) or {})
        file_ref = data.get("file")
        if isinstance(file_ref, str):
            data["file"] = self._materialize_local_file_ref(
                file_ref,
                action_index=action_index,
                node_index=node_index,
                segment_index=segment_index,
            )
        updated = dict(segment)
        updated["data"] = data
        return updated

    def _materialize_local_file_ref(
        self,
        file_ref: str,
        *,
        action_index: int,
        node_index: int | None,
        segment_index: int | None,
    ) -> str:
        source_path = self._local_path_from_file_ref(file_ref)
        if source_path is None or not source_path.is_file():
            return file_ref
        state = get_runtime_state()
        outbound_root = state.data_dir / "outbound"
        maybe_cleanup_outbound_cache(outbound_root)
        run_id = self._safe_path_segment(str(self.ctx.run.run_id))
        action_part = self._safe_path_segment(str(action_index))
        parts = [run_id, action_part]
        if node_index is not None:
            parts.append(f"node-{node_index}")
        if segment_index is not None:
            parts.append(f"segment-{segment_index}")
        destination_dir = outbound_root / Path(*parts)
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / source_path.name
        shutil.copy2(source_path, destination)
        return str(destination)

    @staticmethod
    def _local_path_from_file_ref(file_ref: str) -> Path | None:
        raw = str(file_ref or "").strip()
        if not raw:
            return None
        if raw.startswith(("http://", "https://", "base64://", "data:")):
            return None
        if raw.startswith("file://"):
            parsed = urlparse(raw)
            return Path(unquote(parsed.path)).expanduser().resolve(strict=False)
        return Path(raw).expanduser().resolve(strict=False)

    @staticmethod
    def _safe_path_segment(value: str) -> str:
        cleaned = "".join(ch if ch.isalnum() or ch in {":", "-", "_"} else "_" for ch in value)
        return cleaned or "value"

    @staticmethod
    def _thread_content_from_segments(segments: list[dict[str, Any]]) -> str:
        texts = [
            str(segment.get("data", {}).get("text", ""))
            for segment in segments
            if segment.get("type") == "text"
        ]
        if texts:
            return " ".join(texts).strip()
        attachment_types = [str(segment.get("type", "")) for segment in segments]
        return f"[link_resolver:{','.join(filter(None, attachment_types))}]"

    @staticmethod
    def _thread_content_from_forward_messages(messages: list[dict[str, Any]]) -> str:
        texts: list[str] = []
        attachment_types: list[str] = []
        for node in messages:
            data = dict(node.get("data", {}) or {})
            for segment in data.get("content", []) or []:
                seg_type = str(segment.get("type", "") or "")
                seg_data = dict(segment.get("data", {}) or {})
                if seg_type == "text":
                    text = str(seg_data.get("text", "") or "").strip()
                    if text:
                        texts.append(text)
                elif seg_type:
                    attachment_types.append(seg_type)
        if texts:
            return "\n".join(texts)
        return f"[link_resolver:forward:{','.join(filter(None, attachment_types))}]"


class CompatEvent:
    def __init__(self, *, ctx: Any, sink: DeliverySink) -> None:
        self._ctx = ctx
        self._sink = sink
        raw_event = dict(getattr(ctx.event, "raw_event", {}) or {})
        message = raw_event.get("message")
        if not isinstance(message, list):
            message = [_segment_like_to_dict(segment) for segment in getattr(ctx.event, "segments", [])]
        self.message_obj = SimpleNamespace(
            message=message,
            raw_message=raw_event,
            message_id=str(getattr(ctx.event, "platform_message_id", "") or raw_event.get("message_id", "")),
        )
        self.bot = CompatBot(ctx, sink.gateway)
        self._should_call_llm = False

    def plain_result(self, text: str) -> CompatResult:
        return CompatResult(kind="text", payload=text)

    def chain_result(self, chain: list[Any]) -> CompatResult:
        return CompatResult(kind="chain", payload=list(chain))

    def set_result(self, result: CompatResult) -> None:
        self._sink.queue_result(result)

    async def accept_result(self, result: CompatResult) -> None:
        await self._sink.handle_result(result)

    async def flush(self) -> None:
        await self._sink.flush()

    async def send(self, message: MessageChain) -> None:
        await self._sink.send_chain(message)

    def should_call_llm(self, value: bool) -> None:
        self._should_call_llm = bool(value)

    def is_tool_invocation(self) -> bool:
        """判断当前解析是否由 AcaBot 工具主动触发."""

        return self._ctx.metadata.get("tool_name") == "link_resolver"

    def get_group_id(self) -> str | None:
        return self._ctx.event.source.group_id

    def get_self_id(self) -> str:
        return str(getattr(self._sink.gateway, "_self_id", "") or self._ctx.event.raw_event.get("self_id", "") or "")

    def get_sender_id(self) -> str:
        return str(self._ctx.event.source.user_id)

    def has_json_component(self) -> bool:
        for component in self.message_obj.message:
            if isinstance(component, dict):
                comp_type = str(component.get("type", "") or "")
                if comp_type == "reply":
                    continue
                if "json" in comp_type.lower():
                    return True
            else:
                comp_type = str(getattr(component, "type", "") or "")
                if "json" in comp_type.lower():
                    return True
        return False

    def plain_text(self) -> str:
        parts: list[str] = []
        for component in self.message_obj.message:
            if isinstance(component, dict):
                if str(component.get("type", "")) == "text":
                    parts.append(str((component.get("data") or {}).get("text", "")))
                continue
            if isinstance(component, Plain):
                parts.append(component.text)
        return "".join(parts)

    @property
    def message_str(self) -> str:
        return self.plain_text()

    def is_self_message(self) -> bool:
        self_id = self.get_self_id()
        return bool(self_id) and self.get_sender_id() == self_id

    def has_output(self) -> bool:
        return self._sink.has_output()



def _segment_like_to_dict(segment: Any) -> dict[str, Any]:
    if isinstance(segment, dict):
        return dict(segment)
    seg_type = getattr(segment, "type", "")
    data = dict(getattr(segment, "data", {}) or {})
    return {"type": seg_type, "data": data}


def maybe_cleanup_outbound_cache(
    outbound_root: Path,
    *,
    now: float | None = None,
) -> int:
    """最多每小时清理一次 outbound 旧媒体缓存."""

    global _LAST_OUTBOUND_CACHE_CLEANUP_AT
    resolved_now = time.time() if now is None else float(now)
    if (
        _LAST_OUTBOUND_CACHE_CLEANUP_AT
        and resolved_now - _LAST_OUTBOUND_CACHE_CLEANUP_AT < OUTBOUND_CACHE_CLEANUP_INTERVAL_SECONDS
    ):
        return 0
    _LAST_OUTBOUND_CACHE_CLEANUP_AT = resolved_now
    return cleanup_outbound_cache(outbound_root, now=resolved_now)


def cleanup_outbound_cache(
    outbound_root: Path,
    *,
    ttl_seconds: int = OUTBOUND_CACHE_TTL_SECONDS,
    now: float | None = None,
) -> int:
    """删除 TTL 外的 outbound 文件或目录，返回删除条目数."""

    if ttl_seconds <= 0 or not outbound_root.exists():
        return 0
    resolved_now = time.time() if now is None else float(now)
    cutoff = resolved_now - float(ttl_seconds)
    removed = 0
    for child in list(outbound_root.iterdir()):
        try:
            stat = child.stat()
        except OSError:
            continue
        if stat.st_mtime > cutoff:
            continue
        try:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1
        except OSError:
            logger.warning("Failed to clean link resolver outbound cache path: %s", child)
    if removed:
        logger.info("Cleaned link resolver outbound cache: root=%s removed=%s", outbound_root, removed)
    return removed
