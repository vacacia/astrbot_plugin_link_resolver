"""X/Twitter 消息处理器。

流程：
1. 从消息中提取 X / Twitter 链接并调用 extractor 解析。
2. 下载推文图片或视频，多媒体混合推文会同时保留图片和视频。
3. 纯图片、多视频、图文混合推文始终合并转发；单视频推文按配置决定。
4. 非合并模式下只发单独视频，不发送摘要节点。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Node, Nodes, Plain, Video

from ..common import SizeLimitExceeded, get_twitter_image_path, get_twitter_video_path
from . import (
    TWITTER_DOWNLOAD_HEADERS,
    TwitterParseError,
    TwitterResult,
    extract_twitter_links,
)


class TwitterMixin:
    def _build_twitter_path(self, url: str, is_video: bool, request_id: str) -> Path:
        base_dir = get_twitter_video_path() if is_video else get_twitter_image_path()
        default_suffix = ".mp4" if is_video else ".jpg"
        suffix = default_suffix if is_video else self._guess_media_suffix(url, ".jpg")
        return base_dir / f"{self._hash_url(url)}_{request_id}{suffix}"

    async def _download_twitter_video(self, url: str, request_id: str) -> Path:
        max_bytes = (
            self.max_video_size_mb * 1024 * 1024 if self.max_video_size_mb > 0 else None
        )
        size_mb = await self._estimate_total_size_mb(
            url,
            None,
            headers=TWITTER_DOWNLOAD_HEADERS,
        )
        logger.debug(
            "𝕏 估算视频大小: %s MB",
            f"{size_mb:.2f}" if size_mb is not None else "未知",
        )
        if size_mb is not None and max_bytes and size_mb * 1024 * 1024 > max_bytes:
            raise SizeLimitExceeded("超过大小限制")

        output_path = self._build_twitter_path(
            url, is_video=True, request_id=request_id
        )
        await self._download_stream(
            url,
            output_path,
            cookies=None,
            max_bytes=max_bytes,
            headers=TWITTER_DOWNLOAD_HEADERS,
        )
        return output_path

    async def _download_twitter_image(self, url: str, request_id: str) -> Path:
        output_path = self._build_twitter_path(
            url, is_video=False, request_id=request_id
        )
        await self._download_stream(
            url,
            output_path,
            cookies=None,
            max_bytes=None,
            headers=TWITTER_DOWNLOAD_HEADERS,
        )
        return output_path

    def _build_twitter_summary(self, result: TwitterResult) -> str:
        lines: list[str] = []
        header = []
        if result.author:
            header.append(f"X @{result.author}")
        else:
            header.append("X")
        if result.created_at:
            header.append(result.created_at)
        lines.append(" | ".join(header))

        if result.text:
            text = result.text.strip()
            if len(text) > 1200:
                text = text[:1197] + "..."
            lines.append(text)

        if result.source_url:
            lines.append(f"链接: {result.source_url}")
        return "\n".join(line for line in lines if line)

    async def _process_twitter(
        self, event: AstrMessageEvent, target_link: str, is_from_card: bool = False
    ) -> None:
        process_start = time.perf_counter()
        timing: dict[str, float] = {}

        self._refresh_config()
        if not self.twitter_enabled:
            return

        source_tag = "(来自卡片)" if is_from_card else ""
        request_id = uuid.uuid4().hex[:8]
        await self._send_reaction_emoji(event, source_tag)

        target_link = (target_link or "").strip()
        if not target_link:
            logger.warning("⚠️ X 链接为空%s", source_tag)
            return
        logger.info("𝕏 解析%s: %s", source_tag, target_link)

        parse_start = time.perf_counter()
        retry_count = getattr(self, "retry_count", 3)
        result: TwitterResult | None = None
        last_error = None

        for attempt in range(retry_count + 1):
            try:
                result = await asyncio.wait_for(
                    self.twitter_extractor.parse(target_link),
                    timeout=30.0,
                )
                break
            except asyncio.CancelledError:
                logger.info("♻️ X 解析任务已中断%s", source_tag)
                return
            except TwitterParseError as exc:
                last_error = str(exc)
                if attempt < retry_count:
                    logger.warning(
                        "⚠️ X 解析失败%s: %s，重试 %d/%d",
                        source_tag,
                        str(exc),
                        attempt + 1,
                        retry_count,
                    )
                    await asyncio.sleep(1.0)
                else:
                    logger.error(
                        "❌ X 解析失败%s: %s (已重试%d次)",
                        source_tag,
                        str(exc),
                        retry_count,
                    )
            except Exception as exc:
                last_error = str(exc)
                if attempt < retry_count:
                    logger.warning(
                        "⚠️ X 解析异常%s: %s，重试 %d/%d",
                        source_tag,
                        str(exc),
                        attempt + 1,
                        retry_count,
                    )
                    await asyncio.sleep(1.0)
                else:
                    logger.error(
                        "❌ X 解析异常%s: %s (已重试%d次)",
                        source_tag,
                        str(exc),
                        retry_count,
                    )

        timing["parse"] = time.perf_counter() - parse_start
        if result is None:
            logger.error(
                "❌ X 解析最终失败%s: %s, 解析耗时=%.2fs",
                source_tag,
                last_error,
                timing["parse"],
            )
            return

        if not result.video_urls and not result.image_urls:
            logger.warning("⚠️ X 未找到可下载媒体%s", source_tag)
            return

        summary_text = self._build_twitter_summary(result)
        media_components: list[object] = []
        media_paths: list[Path] = []
        failed_media = 0

        video_urls = result.video_urls[: self.twitter_max_media]
        remaining = max(self.twitter_max_media - len(video_urls), 0)
        image_urls = result.image_urls[:remaining]

        download_start = time.perf_counter()
        for url in video_urls:
            try:
                video_path = await self._download_twitter_video(url, request_id)
                media_paths.append(video_path)
                media_components.append(Video.fromFileSystem(str(video_path.resolve())))
            except asyncio.CancelledError:
                raise
            except SizeLimitExceeded:
                logger.warning(
                    "⚠️ X 视频超过大小限制%s (%dMB)",
                    source_tag,
                    self.max_video_size_mb,
                )
                failed_media += 1
            except Exception as exc:
                failed_media += 1
                logger.warning("⚠️ X 视频下载失败%s: %s", source_tag, str(exc))

        for url in image_urls:
            try:
                image_path = await self._download_twitter_image(url, request_id)
                media_paths.append(image_path)
                media_components.append(Image.fromFileSystem(str(image_path.resolve())))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed_media += 1
                logger.warning("⚠️ X 图片下载失败%s: %s", source_tag, str(exc))

        timing["download"] = time.perf_counter() - download_start
        if not media_components:
            logger.warning(
                "⚠️ X 媒体下载全部失败%s, 下载耗时=%.2fs",
                source_tag,
                timing["download"],
            )
            return

        is_image_post = bool(image_urls and not video_urls)
        is_single_video = len(video_urls) == 1 and not image_urls
        has_multi_video = len(video_urls) > 1
        has_mixed_media = bool(video_urls and image_urls)
        enable_merge_send = (
            is_image_post
            or has_multi_video
            or has_mixed_media
            or (is_single_video and self.twitter_merge_send)
        )

        send_start = time.perf_counter()
        if enable_merge_send:
            nodes = Nodes([])
            sender_uin = self._get_merge_sender_uin(event)
            if summary_text:
                nodes.nodes.append(Node(uin=sender_uin, content=[Plain(summary_text)]))
            for component in media_components:
                merge_component = await self._prepare_component_for_merge_send(
                    component
                )
                nodes.nodes.append(Node(uin=sender_uin, content=[merge_component]))
            await event.send(MessageChain([nodes]))
        else:
            await event.send(MessageChain([media_components[0]]))

        timing["send"] = time.perf_counter() - send_start
        total_elapsed = time.perf_counter() - process_start
        logger.info(
            "𝕏 处理完成%s: tweet=%s, 媒体=%d, 失败=%d | 耗时: 解析=%.2fs, 下载=%.2fs, 发送=%.2fs, 总计=%.2fs",
            source_tag,
            result.tweet_id or "未知",
            len(media_components),
            failed_media,
            timing.get("parse", 0),
            timing.get("download", 0),
            timing.get("send", 0),
            total_elapsed,
        )

        if media_paths:
            await self.cleanup_files(media_paths, [])

    async def handle_twitter(self, event: AstrMessageEvent) -> None:
        if not self.twitter_enabled:
            return
        if self._is_self_message(event):
            return
        if await self._is_bot_muted(event):
            return
        event.should_call_llm(True)
        links = extract_twitter_links(event.message_str)
        logger.info("𝕏 匹配链接: %s", links)
        if not links:
            return
        try:
            await self._process_twitter(event, links[0], is_from_card=False)
        except asyncio.CancelledError:
            logger.info("♻️ X 解析任务已中断")
            return
