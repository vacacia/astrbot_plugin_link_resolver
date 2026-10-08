"""接入 AstrBot 消息流程, 发送单个 YouTube 视频."""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Node, Nodes, Plain, Video

from ..common import SizeLimitExceeded, get_youtube_video_path
from . import (
    YoutubeDownloadError,
    YoutubeParseError,
    YoutubeResult,
    extract_youtube_links,
    is_youtube_403_error,
)


class YoutubeMixin:
    def _build_youtube_summary(self, result: YoutubeResult) -> str:
        header = "YouTube" + (f" | {result.uploader}" if result.uploader else "")
        lines = [header]
        if result.title:
            lines.append(result.title)
        if result.duration:
            minutes, seconds = divmod(result.duration, 60)
            lines.append(f"时长: {minutes}:{seconds:02d}")
        lines.append(f"链接: {result.source_url}")
        return "\n".join(lines)

    async def _download_youtube_video(
        self, url: str, request_id: str, player_client: str
    ) -> tuple[YoutubeResult, Path]:
        max_bytes = (
            self.max_video_size_mb * 1024 * 1024 if self.max_video_size_mb > 0 else None
        )
        return await self.youtube_extractor.download(
            url,
            get_youtube_video_path(),
            request_id,
            max_height=self.youtube_max_height,
            video_codec=self.youtube_video_codec,
            max_bytes=max_bytes,
            cookie_file=self.youtube_cookies_file or None,
            player_client=player_client,
        )

    async def _inspect_youtube_video(
        self, target_link: str, player_client: str
    ) -> YoutubeResult:
        return await self.youtube_extractor.inspect(
            target_link,
            cookie_file=self.youtube_cookies_file or None,
            player_client=player_client,
        )

    async def _process_youtube(
        self, event: AstrMessageEvent, target_link: str, is_from_card: bool = False
    ) -> None:
        started = time.perf_counter()
        self._refresh_config()
        if not self.youtube_enabled:
            return
        source_tag = "(来自卡片)" if is_from_card else ""
        if not (target_link := (target_link or "").strip()):
            return
        await self._send_reaction_emoji(event, source_tag)
        result: YoutubeResult | None = None
        video_path: Path | None = None
        last_error: str | None = None
        request_id = uuid.uuid4().hex[:8]
        active_client = self.youtube_player_client
        fallback_attempted = active_client == "web_embedded"
        attempt = 0
        while attempt <= self.retry_count:
            try:
                preview = await self._inspect_youtube_video(target_link, active_client)
                if (
                    self.youtube_max_duration_seconds > 0
                    and preview.duration is not None
                    and preview.duration > self.youtube_max_duration_seconds
                ):
                    logger.warning(
                        "⚠️ YouTube 视频时长超过限制%s: %ss > %ss",
                        source_tag,
                        preview.duration,
                        self.youtube_max_duration_seconds,
                    )
                    event.set_result(event.plain_result("视频太长了你自己看去"))
                    return
                result, video_path = await self._download_youtube_video(
                    target_link, request_id, active_client
                )
                break
            except asyncio.CancelledError:
                logger.info("♻️ YouTube 解析任务已中断%s", source_tag)
                return
            except (YoutubeParseError, YoutubeDownloadError, SizeLimitExceeded) as exc:
                last_error = str(exc)
            except Exception as exc:
                last_error = str(exc)

            if is_youtube_403_error(last_error):
                if not fallback_attempted:
                    fallback_attempted = True
                    active_client = "web_embedded"
                    logger.warning(
                        "⚠️ YouTube 媒体流被拒绝%s，改用兼容客户端 "
                        "web_embedded 重试一次",
                        source_tag,
                    )
                    continue
                logger.error(
                    "❌ YouTube 媒体流被拒绝%s: %s。请更新 yt-dlp、"
                    "检查 Cookies 是否有效或检查出口 IP。",
                    source_tag,
                    last_error,
                )
                break

            if attempt < self.retry_count:
                logger.warning(
                    "⚠️ YouTube 处理失败%s: %s，重试 %d/%d",
                    source_tag,
                    last_error,
                    attempt + 1,
                    self.retry_count,
                )
                await asyncio.sleep(1.0)
            attempt += 1
        if result is None or video_path is None:
            logger.error(
                "❌ YouTube 处理失败%s: %s", source_tag, last_error or "未知错误"
            )
            return
        try:
            component = Video.fromFileSystem(str(video_path.resolve()))
            if self.youtube_merge_send:
                nodes = Nodes([])
                sender_uin = self._get_merge_sender_uin(event)
                nodes.nodes.append(
                    Node(
                        uin=sender_uin,
                        content=[Plain(self._build_youtube_summary(result))],
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
                "▶️ YouTube 处理完成%s: id=%s, 文件=%s, 耗时=%.2fs",
                source_tag,
                result.video_id,
                video_path.name,
                time.perf_counter() - started,
            )
        finally:
            await self.cleanup_files([video_path], [])

    async def handle_youtube(self, event: AstrMessageEvent) -> None:
        if not self.youtube_enabled or self._is_self_message(event):
            return
        if await self._is_bot_muted(event):
            return
        event.should_call_llm(True)
        links = extract_youtube_links(event.message_str)
        if not links:
            return
        try:
            await self._process_youtube(event, links[0])
        except asyncio.CancelledError:
            logger.info("♻️ YouTube 解析任务已中断")
