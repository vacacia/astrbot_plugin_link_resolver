"""通用站点(yt-dlp 兜底)的消息处理: 白名单域名 -> 信息预检 -> 下载 -> 发送视频.

流程: 提取白名单链接 -> 表情回应 -> inspect(时长限制) -> download -> 发送 -> 清理.
失败一律记日志; 是否在群里提示由 error_notify_mode 决定(静默/脱敏/报错, 默认静默),
与 B 站保持一致. 注意白名单站点里很多链接是图文/直播等 yt-dlp 不支持的类型,
开启"报错"模式时这类链接也会提示.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Node, Nodes, Plain, Video

from ..common import SizeLimitExceeded, get_generic_video_path
from ..youtube import YoutubeDownloadError, YoutubeParseError, YoutubeResult

# 这类错误重试也不会成功(链接类型不受支持/无视频), 直接放弃以免白等
_NON_RETRYABLE_HINTS = (
    "unsupported url",
    "no video",
    "there is no video",
    "is not a valid url",
    "private",
    "login required",
    "logged-in",
    "sign in",
    "--cookies",
    "no video formats",
    "不是单个视频",
    "直播/预告内容不支持",
    "不支持的链接",
    "仅音频内容不支持",
    "解析结果指向被排除的站点",
    "超过大小限制",
    # yt-dlp 在 Content-Length 超限时提前中止, 不产生文件, 错误文案是这两种
    "file is larger than",
    "未生成可发送的视频文件",
)


def _is_non_retryable(error: str | None) -> bool:
    text = (error or "").lower()
    return any(hint in text for hint in _NON_RETRYABLE_HINTS)


class GenericMixin:
    def _build_generic_summary(self, result: YoutubeResult) -> str:
        lines = ["通用解析" + (f" | {result.uploader}" if result.uploader else "")]
        if result.title:
            lines.append(result.title)
        if result.duration:
            minutes, seconds = divmod(result.duration, 60)
            lines.append(f"时长: {minutes}:{seconds:02d}")
        lines.append(f"链接: {result.source_url}")
        return "\n".join(lines)

    async def _process_generic(
        self, event: AstrMessageEvent, target_link: str, is_from_card: bool = False
    ) -> None:
        started = time.perf_counter()
        self._refresh_config()
        if not self.generic_enabled:
            return
        source_tag = "(来自卡片)" if is_from_card else ""
        if not (target_link := (target_link or "").strip()):
            return
        await self._send_reaction_emoji(event, source_tag)
        logger.info("🌐 通用解析%s: %s", source_tag, target_link)

        max_bytes = (
            self.max_video_size_mb * 1024 * 1024 if self.max_video_size_mb > 0 else None
        )
        result: YoutubeResult | None = None
        video_path: Path | None = None
        last_error: str | None = None
        request_id = uuid.uuid4().hex[:8]
        timing = {"inspect": 0.0, "download": 0.0}

        for attempt in range(self.retry_count + 1):
            try:
                step = time.perf_counter()
                preview = await self.generic_extractor.inspect(
                    target_link, cookie_file=self.generic_cookies_file
                )
                timing["inspect"] = time.perf_counter() - step
                if (
                    self.generic_max_duration_seconds > 0
                    and preview.duration is not None
                    and preview.duration > self.generic_max_duration_seconds
                ):
                    logger.warning(
                        "⚠️ 通用解析时长超过限制%s: %ss > %ss",
                        source_tag,
                        preview.duration,
                        self.generic_max_duration_seconds,
                    )
                    event.set_result(event.plain_result("视频太长了你自己看去"))
                    return
                step = time.perf_counter()
                result, video_path = await self.generic_extractor.download(
                    target_link,
                    get_generic_video_path(),
                    request_id,
                    max_height=self.generic_max_height,
                    video_codec="h264",
                    max_bytes=max_bytes,
                    cookie_file=self.generic_cookies_file,
                )
                timing["download"] = time.perf_counter() - step
                break
            except asyncio.CancelledError:
                logger.info("♻️ 通用解析任务已中断%s", source_tag)
                return
            except (YoutubeParseError, YoutubeDownloadError, SizeLimitExceeded) as exc:
                last_error = str(exc)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if _is_non_retryable(last_error) or attempt >= self.retry_count:
                break
            logger.warning(
                "⚠️ 通用解析失败%s: %s，重试 %d/%d",
                source_tag,
                last_error,
                attempt + 1,
                self.retry_count,
            )
            await asyncio.sleep(1.0)

        if result is None or video_path is None:
            logger.error("❌ 通用解析失败%s: %s", source_tag, last_error or "未知错误")
            # 与 B 站一致: 是否在群里提示由 general_settings.error_notify_mode 决定(默认静默)
            if self.error_notify_mode == "报错":
                event.set_result(
                    event.plain_result(f"❌ 通用解析失败: {last_error or '未知错误'}")
                )
            elif self.error_notify_mode == "脱敏":
                event.set_result(event.plain_result("❌ 通用解析失败"))
            return

        try:
            send_started = time.perf_counter()
            component = Video.fromFileSystem(str(video_path.resolve()))
            if self.generic_merge_send:
                sender_uin = self._get_merge_sender_uin(event)
                nodes = Nodes([])
                nodes.nodes.append(
                    Node(
                        uin=sender_uin,
                        content=[Plain(self._build_generic_summary(result))],
                    )
                )
                nodes.nodes.append(
                    Node(
                        uin=sender_uin,
                        content=[
                            await self._prepare_component_for_merge_send(component)
                        ],
                    )
                )
                await event.send(MessageChain([nodes]))
            else:
                await event.send(MessageChain([component]))
            logger.info(
                "🌐 通用解析完成%s: id=%s, 文件=%s | 耗时: 预检=%.2fs, 下载=%.2fs, 发送=%.2fs, 总计=%.2fs",
                source_tag,
                result.video_id,
                video_path.name,
                timing["inspect"],
                timing["download"],
                time.perf_counter() - send_started,
                time.perf_counter() - started,
            )
        finally:
            await self.cleanup_files([video_path], [])

    async def handle_generic(self, event: AstrMessageEvent) -> None:
        if not self.generic_enabled or self._is_self_message(event):
            return
        # filter.regex 对任意链接都会触发, 先用白名单快速过滤, 命中后才做其它检查
        links = self.generic_extractor.extract_links(event.message_str)
        if not links:
            return
        if await self._is_bot_muted(event):
            return
        event.should_call_llm(True)
        try:
            await self._process_generic(event, links[0])
        except asyncio.CancelledError:
            logger.info("♻️ 通用解析任务已中断")
