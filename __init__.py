from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib
import logging
import re
from types import SimpleNamespace
from typing import Any, Awaitable

from acabot.agent import ToolSpec
from acabot.runtime.tool_broker import ToolExecutionContext, ToolResult
from acabot.runtime.plugin_protocol import (
    RuntimeHook,
    RuntimeHookPoint,
    RuntimeHookResult,
    RuntimePlugin,
    RuntimePluginContext,
    RuntimeToolRegistration,
)
from acabot.types import EventSource, MsgSegment, StandardEvent

from .compat import (
    CompatEvent,
    DeliverySink,
    ensure_optional_dependency_stubs,
    install_astrbot_compat,
    set_runtime_state,
)
from .compat.astrbot.api.star import Context as CompatContext

logger = logging.getLogger("acabot.plugins.link_resolver")


class LinkResolverOnEventHook(RuntimeHook):
    name = "link_resolver_on_event"
    priority = 20

    def __init__(self, plugin: "Plugin") -> None:
        self._plugin = plugin

    async def handle(self, ctx: Any) -> RuntimeHookResult:
        if not self._plugin._resolver or not self._plugin._legacy_module:
            return RuntimeHookResult()
        if getattr(ctx.event, "event_type", "") != "message":
            return RuntimeHookResult()

        sink = DeliverySink(ctx=ctx, gateway=self._plugin._runtime.gateway)
        event = CompatEvent(ctx=ctx, sink=sink)
        if not self._plugin._event_might_match(event):
            return RuntimeHookResult()
        if getattr(ctx, "computer_policy_decision", None) is not None:
            ctx.computer_policy_decision.visible_skills = []
        if getattr(ctx, "agent", None) is not None and hasattr(ctx.agent, "skills"):
            ctx.agent.skills = []
        run_id = getattr(getattr(ctx, "run", None), "run_id", "")
        logger.info(
            "link_resolver on_event matched: run_id=%s conversation_id=%s",
            run_id,
            getattr(getattr(ctx, "event", None), "conversation_id", ""),
        )
        try:
            await self._plugin._dispatch_event(event)
            await event.flush()
            logger.info(
                "link_resolver on_event completed: run_id=%s action_count=%s",
                run_id,
                len(getattr(ctx, "actions", []) or []),
            )
        except Exception:
            logger.exception(
                "link_resolver on_event dispatch failed: run_id=%s",
                run_id,
            )
        return RuntimeHookResult(action="skip_agent")


class Plugin(RuntimePlugin):
    name = "link_resolver"

    def __init__(self) -> None:
        self._runtime: RuntimePluginContext | None = None
        self._legacy_module: Any | None = None
        self._resolver: Any | None = None
        self._on_event_hook = LinkResolverOnEventHook(self)
        self._dispatch_lock = asyncio.Lock()

    async def setup(self, runtime: RuntimePluginContext) -> None:
        self._runtime = runtime
        set_runtime_state(
            data_dir=runtime.data_dir,
            gateway=runtime.gateway,
            plugin_config=runtime.plugin_config,
        )
        ensure_optional_dependency_stubs()
        install_astrbot_compat()
        self._legacy_module = importlib.import_module("plugins.link_resolver.main")
        resolver_cls = getattr(self._legacy_module, "LinkResolver")
        compat_context = CompatContext(runtime.plugin_config)
        self._resolver = resolver_cls(compat_context, runtime.plugin_config)

    def hooks(self) -> list[tuple[RuntimeHookPoint, RuntimeHook]]:
        if self._resolver is None:
            return []
        return [(RuntimeHookPoint.ON_EVENT, self._on_event_hook)]

    def runtime_tools(self) -> list[RuntimeToolRegistration]:
        if self._resolver is None:
            return []
        return [
            RuntimeToolRegistration(
                spec=ToolSpec(
                    name="link_resolver",
                    description=(
                        "主动解析并发送 B站、抖音、小红书、微博、X 链接。"
                        "遇到这些平台的链接时优先使用本工具，不要用 web_fetch、浏览器或 shell 自己抓取页面。"
                        "当普通消息自动解析受到平台启用项、媒体数量、合并发送、摘要样式、B站时长等设置限制时，"
                        "使用这个工具并在参数里临时指定需要的解析配置。"
                        "工具会把解析出的文字、图片、视频或合并转发发送到当前会话。"
                        "如果返回 handled=true，表示解析和发送动作已经生成；同一个链接不要再重复调用其他抓取工具。"
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "包含待解析链接的原文。可以只填 URL，也可以填用户整条消息。",
                            },
                            "platform": {
                                "type": "string",
                                "enum": [
                                    "auto",
                                    "bili",
                                    "bilibili",
                                    "douyin",
                                    "xhs",
                                    "xiaohongshu",
                                    "weibo",
                                    "x",
                                    "twitter",
                                ],
                                "default": "auto",
                                "description": "指定平台；auto 会临时启用全部支持的平台并按链接匹配。",
                            },
                            "max_media": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 99,
                                "description": "临时覆盖图片/视频数量上限，适用于抖音、小红书、微博、X。",
                            },
                            "merge_send": {
                                "type": "boolean",
                                "description": "临时覆盖合并发送开关，适用于支持合并发送的平台。",
                            },
                            "summary_mode": {
                                "type": "string",
                                "enum": ["文字摘要", "渲染卡片", "text", "card"],
                                "description": "临时覆盖摘要样式。",
                            },
                            "video_quality": {
                                "type": "string",
                                "description": "临时覆盖 B站画质，例如 4K、1080P高帧率、720P。",
                            },
                            "max_duration_seconds": {
                                "type": "integer",
                                "minimum": 0,
                                "description": "临时覆盖 B站最大视频时长；0 表示不限制。",
                            },
                            "allow_quality_fallback": {
                                "type": "boolean",
                                "description": "临时覆盖 B站画质回退开关。",
                            },
                            "enable_multi_page": {
                                "type": "boolean",
                                "description": "临时覆盖 B站多P开关。",
                            },
                            "multi_page_max": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 20,
                                "description": "临时覆盖 B站多P数量上限。",
                            },
                            "timeout_seconds": {
                                "type": "integer",
                                "minimum": 5,
                                "maximum": 300,
                                "default": 120,
                                "description": "本次解析总超时秒数。默认 120 秒。",
                            },
                            "config": {
                                "type": "object",
                                "description": (
                                    "高级配置覆盖，形状与插件配置一致，例如 "
                                    "{\"douyin_settings\":{\"max_media\":99},"
                                    "\"xhs_settings\":{\"auto_unmerge_threshold_mb\":150}}。"
                                ),
                            },
                        },
                        "required": ["text"],
                    },
                ),
                handler=self._handle_link_resolver_tool,
                metadata={"plugin": "link_resolver", "suppress_tool_notice": True},
            )
        ]

    def _candidate_patterns(self) -> list[tuple[bool, str, str]]:
        assert self._legacy_module is not None
        assert self._resolver is not None
        return [
            (
                bool(getattr(self._resolver, "bili_enabled", True)),
                str(getattr(self._legacy_module, "BILI_MESSAGE_PATTERN", "")),
                "handle_bili_video",
            ),
            (
                bool(getattr(self._resolver, "douyin_enabled", True)),
                str(getattr(self._legacy_module, "DOUYIN_MESSAGE_PATTERN", "")),
                "handle_douyin",
            ),
            (
                bool(getattr(self._resolver, "xhs_enabled", True)),
                str(getattr(self._legacy_module, "XHS_MESSAGE_PATTERN", "")),
                "handle_xhs",
            ),
            (
                bool(getattr(self._resolver, "weibo_enabled", True)),
                str(getattr(self._legacy_module, "WEIBO_MESSAGE_PATTERN", "")),
                "handle_weibo",
            ),
            (
                bool(getattr(self._resolver, "twitter_enabled", True)),
                str(getattr(self._legacy_module, "TWITTER_MESSAGE_PATTERN", "")),
                "handle_twitter",
            ),
        ]

    def _event_might_match(self, event: CompatEvent) -> bool:
        if event.is_self_message():
            return False
        if event.has_json_component():
            return True
        text = event.plain_text()
        if not text:
            return False
        for enabled, pattern, _method_name in self._candidate_patterns():
            if enabled and pattern and re.search(pattern, text):
                return True
        return False

    async def _dispatch_event(self, event: CompatEvent) -> bool:
        async with self._dispatch_lock:
            return await self._dispatch_event_unlocked(event)

    async def _dispatch_event_unlocked(self, event: CompatEvent) -> bool:
        assert self._runtime is not None
        assert self._legacy_module is not None
        assert self._resolver is not None

        if not self._event_might_match(event):
            return False

        if event.has_json_component():
            handled = await _consume_handler(self._resolver.handle_json_card(event), event)
            if handled or event.has_output():
                return True

        text = event.plain_text()
        if not text:
            return event.has_output()

        for enabled, pattern, method_name in self._candidate_patterns():
            if not enabled or not pattern or not re.search(pattern, text):
                continue
            handled = await _consume_handler(getattr(self._resolver, method_name)(event), event)
            if handled or event.has_output():
                return True
        return event.has_output()

    async def _handle_link_resolver_tool(
        self,
        arguments: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> ToolResult:
        assert self._runtime is not None
        assert self._resolver is not None

        text = str(arguments.get("text", "") or "").strip()
        if not text:
            raise ValueError("text is required")

        adapter_ctx = self._build_tool_event_context(text=text, arguments=arguments, ctx=ctx)
        sink = DeliverySink(ctx=adapter_ctx, gateway=self._runtime.gateway)
        event = CompatEvent(ctx=adapter_ctx, sink=sink)
        timeout_seconds = self._optional_int(
            arguments.get("timeout_seconds"),
            minimum=5,
            maximum=300,
        ) or 120
        started_at = asyncio.get_running_loop().time()

        async with self._dispatch_lock:
            previous_config = deepcopy(getattr(self._resolver, "config", {}) or {})
            try:
                self._resolver.config = self._build_tool_config(arguments)
                self._resolver._refresh_config()
                try:
                    handled = await asyncio.wait_for(
                        self._dispatch_event_unlocked(event),
                        timeout=timeout_seconds,
                    )
                    await asyncio.wait_for(event.flush(), timeout=30)
                except asyncio.TimeoutError:
                    payload = {
                        "handled": False,
                        "action_count": 0,
                        "action_types": [],
                        "media_counts": {},
                        "text_preview": "",
                        "platform": str(arguments.get("platform", "auto") or "auto"),
                        "temporary_overrides": self._tool_override_summary(arguments),
                        "error": f"timeout after {timeout_seconds}s",
                    }
                    return ToolResult(
                        llm_content=(
                            f"link_resolver 解析超时，{timeout_seconds} 秒内没有完成。"
                            "同一链接不要改用 web_fetch、浏览器或 shell 重试；请告诉用户稍后重试或换链接。"
                        ),
                        metadata={"plugin": "link_resolver", **payload},
                        raw=payload,
                    )
            finally:
                self._resolver.config = previous_config
                self._resolver._refresh_config()

        actions = list(adapter_ctx.actions)
        for plan in actions:
            plan.metadata.setdefault("origin", "link_resolver")
            plan.metadata["link_resolver_tool"] = True
            plan.metadata["suppresses_default_reply"] = True

        action_types = [str(plan.action.action_type.value) for plan in actions]
        action_summary = self._summarize_actions(actions)
        has_actions = bool(actions)
        payload = {
            "handled": has_actions,
            "action_count": len(actions),
            "action_types": action_types,
            "media_counts": action_summary["media_counts"],
            "text_preview": action_summary["text_preview"],
            "platform": str(arguments.get("platform", "auto") or "auto"),
            "temporary_overrides": self._tool_override_summary(arguments),
        }
        if not has_actions:
            elapsed = asyncio.get_running_loop().time() - started_at
            if elapsed >= max(float(timeout_seconds) - 0.5, 0.0):
                payload["error"] = f"timeout after {timeout_seconds}s"
            else:
                payload["error"] = "no output generated"
        return ToolResult(
            llm_content=self._format_tool_result(payload),
            user_actions=actions,
            metadata={"plugin": "link_resolver", **payload},
            raw=payload,
        )

    def _build_tool_event_context(
        self,
        *,
        text: str,
        arguments: dict[str, Any],
        ctx: ToolExecutionContext,
    ) -> Any:
        platform_message_id = str(arguments.get("message_id", "") or "").strip()
        event_id = str(ctx.metadata.get("event_id", "") or f"{ctx.run_id}:link_resolver_tool")
        event = StandardEvent(
            event_id=f"{event_id}:link_resolver_tool",
            event_type="message",
            platform=ctx.target.platform,
            timestamp=int(ctx.metadata.get("event_timestamp", 0) or 0),
            source=self._event_source_from_tool_context(ctx),
            segments=[MsgSegment(type="text", data={"text": text})],
            platform_message_id=platform_message_id,
            sender_nickname=str(ctx.metadata.get("sender_nickname", "") or "tool"),
            sender_role=str(ctx.metadata.get("sender_role", "") or "") or None,
            raw_event={
                "message_id": platform_message_id,
                "self_id": "",
                "message": [{"type": "text", "data": {"text": text}}],
            },
        )
        return SimpleNamespace(
            run=SimpleNamespace(run_id=ctx.run_id),
            event=event,
            actions=[],
            metadata={"tool_name": "link_resolver"},
        )

    @staticmethod
    def _event_source_from_tool_context(ctx: ToolExecutionContext) -> EventSource:
        target = ctx.target
        actor_id = str(getattr(ctx, "actor_id", "") or "").strip()
        if actor_id.startswith("qq:user:"):
            user_id = actor_id.rsplit(":", 1)[-1]
        else:
            user_id = actor_id or str(getattr(target, "user_id", "") or "")
        return EventSource(
            platform=str(getattr(target, "platform", "") or "qq"),
            message_type=str(getattr(target, "message_type", "") or "private"),
            user_id=user_id,
            group_id=getattr(target, "group_id", None),
        )

    def _build_tool_config(self, arguments: dict[str, Any]) -> dict[str, Any]:
        assert self._runtime is not None
        base = deepcopy(getattr(self._runtime, "plugin_config", {}) or {})
        advanced = arguments.get("config")
        if isinstance(advanced, dict):
            self._deep_merge(base, advanced)

        platform_names = self._platform_names(arguments.get("platform"))
        base["enable_platforms"] = platform_names

        max_media = self._optional_int(arguments.get("max_media"), minimum=1, maximum=99)
        if max_media is not None:
            for key in (
                "douyin_settings",
                "xhs_settings",
                "weibo_settings",
                "twitter_settings",
            ):
                base.setdefault(key, {})["max_media"] = max_media

        if "merge_send" in arguments:
            merge_send = bool(arguments.get("merge_send"))
            for key in (
                "bili_settings",
                "douyin_settings",
                "xhs_settings",
                "weibo_settings",
                "twitter_settings",
            ):
                base.setdefault(key, {})["merge_send"] = merge_send

        summary_mode = self._normalize_summary_mode(arguments.get("summary_mode"))
        if summary_mode is not None:
            for key in ("bili_settings", "douyin_settings", "xhs_settings"):
                base.setdefault(key, {})["summary_mode"] = summary_mode

        if arguments.get("video_quality"):
            base.setdefault("bili_settings", {})["video_quality"] = str(arguments["video_quality"])
        if "allow_quality_fallback" in arguments:
            base.setdefault("bili_settings", {})["allow_quality_fallback"] = bool(arguments["allow_quality_fallback"])
        max_duration = self._optional_int(arguments.get("max_duration_seconds"), minimum=0)
        if max_duration is not None:
            base.setdefault("bili_settings", {})["max_duration_seconds"] = max_duration
        if "enable_multi_page" in arguments:
            base.setdefault("bili_settings", {})["enable_multi_page"] = bool(arguments["enable_multi_page"])
        multi_page_max = self._optional_int(arguments.get("multi_page_max"), minimum=1, maximum=20)
        if multi_page_max is not None:
            base.setdefault("bili_settings", {})["multi_page_max"] = multi_page_max
        return base

    @staticmethod
    def _platform_names(value: Any) -> list[str]:
        raw = str(value or "auto").strip().lower()
        mapping = {
            "bili": ["B站"],
            "bilibili": ["B站"],
            "douyin": ["抖音"],
            "xhs": ["小红书"],
            "xiaohongshu": ["小红书"],
            "weibo": ["微博"],
            "x": ["X"],
            "twitter": ["X"],
        }
        return mapping.get(raw, ["B站", "抖音", "小红书", "微博", "X"])

    @staticmethod
    def _normalize_summary_mode(value: Any) -> str | None:
        raw = str(value or "").strip().lower()
        if not raw:
            return None
        if raw in {"text", "文字摘要"}:
            return "文字摘要"
        if raw in {"card", "render_card", "渲染卡片"}:
            return "渲染卡片"
        return str(value).strip()

    @staticmethod
    def _optional_int(value: Any, *, minimum: int, maximum: int | None = None) -> int | None:
        if value is None or value == "":
            return None
        parsed = int(value)
        parsed = max(minimum, parsed)
        if maximum is not None:
            parsed = min(maximum, parsed)
        return parsed

    @classmethod
    def _deep_merge(cls, target: dict[str, Any], patch: dict[str, Any]) -> None:
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                cls._deep_merge(target[key], value)
            else:
                target[key] = deepcopy(value)

    @staticmethod
    def _tool_override_summary(arguments: dict[str, Any]) -> dict[str, Any]:
        keys = [
            "platform",
            "max_media",
            "merge_send",
            "summary_mode",
            "video_quality",
            "max_duration_seconds",
            "allow_quality_fallback",
            "enable_multi_page",
            "multi_page_max",
            "timeout_seconds",
        ]
        summary = {key: arguments[key] for key in keys if key in arguments}
        if isinstance(arguments.get("config"), dict):
            summary["config_keys"] = sorted(arguments["config"].keys())
        return summary

    @classmethod
    def _summarize_actions(cls, actions: list[Any]) -> dict[str, Any]:
        media_counts: dict[str, int] = {}
        text_parts: list[str] = []
        for plan in actions:
            payload = dict(getattr(plan.action, "payload", {}) or {})
            if "text" in payload:
                text = str(payload.get("text", "") or "").strip()
                if text:
                    text_parts.append(text)
            for part in payload.get("parts", []) or []:
                cls._summarize_outbound_part(part, media_counts, text_parts)
            for segment in payload.get("segments", []) or []:
                cls._summarize_segment(segment, media_counts, text_parts)
            for node in payload.get("messages", []) or []:
                data = dict(node.get("data", {}) or {}) if isinstance(node, dict) else {}
                for segment in data.get("content", []) or []:
                    cls._summarize_segment(segment, media_counts, text_parts)
            thread_content = str(getattr(plan, "thread_content", "") or "").strip()
            if thread_content:
                text_parts.append(thread_content)
        preview = cls._compact_preview("\n".join(text_parts))
        return {"media_counts": media_counts, "text_preview": preview}

    @staticmethod
    def _summarize_outbound_part(
        part: Any,
        media_counts: dict[str, int],
        text_parts: list[str],
    ) -> None:
        if not isinstance(part, dict):
            return
        kind = str(part.get("kind", "") or "").strip()
        if not kind:
            return
        if kind == "text":
            text = str(part.get("text", "") or "").strip()
            if text:
                text_parts.append(text)
            return
        if kind == "mention":
            user_id = str(part.get("user_id", "") or "").strip()
            if user_id:
                text_parts.append(f"@{user_id}")
            return
        media_counts[kind] = media_counts.get(kind, 0) + 1

    @staticmethod
    def _summarize_segment(
        segment: Any,
        media_counts: dict[str, int],
        text_parts: list[str],
    ) -> None:
        if not isinstance(segment, dict):
            return
        seg_type = str(segment.get("type", "") or "").strip()
        if not seg_type:
            return
        if seg_type == "text":
            text = str((segment.get("data") or {}).get("text", "") or "").strip()
            if text:
                text_parts.append(text)
            return
        media_counts[seg_type] = media_counts.get(seg_type, 0) + 1

    @staticmethod
    def _compact_preview(text: str, *, max_chars: int = 500) -> str:
        compact = re.sub(r"\s+", " ", text).strip()
        if len(compact) <= max_chars:
            return compact
        return compact[: max_chars - 1].rstrip() + "…"

    @staticmethod
    def _format_tool_result(payload: dict[str, Any]) -> str:
        media_counts = dict(payload.get("media_counts", {}) or {})
        media_text = ", ".join(
            f"{name}={count}" for name, count in sorted(media_counts.items())
        ) or "none"
        preview = str(payload.get("text_preview", "") or "")
        if not payload.get("handled") or int(payload.get("action_count") or 0) <= 0:
            error = str(payload.get("error", "") or "no output generated")
            return (
                "link_resolver 没有生成可发送内容。"
                f" handled={payload.get('handled')}, action_count={payload.get('action_count')},"
                f" media_counts={media_text}, error={error}."
                " 不要声称图片、视频或文本已经发送；请告诉用户解析失败、超时或稍后重试。"
                " 同一链接不要改用 web_fetch、浏览器或 shell。"
            )
        return (
            "link_resolver 已完成解析并生成发送动作。"
            f" handled={payload.get('handled')}, action_count={payload.get('action_count')},"
            f" action_types={','.join(payload.get('action_types', []) or []) or 'none'},"
            f" media_counts={media_text}."
            + (f" 内容预览: {preview}" if preview else "")
            + " 同一链接不需要再调用 web_fetch、浏览器或 shell。"
        )


async def _consume_handler(result: Any, event: CompatEvent) -> bool:
    handled = False
    if hasattr(result, "__aiter__"):
        async for item in result:
            handled = True
            await event.accept_result(item)
        return handled
    await result
    return True
