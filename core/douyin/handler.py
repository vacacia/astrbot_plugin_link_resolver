# region 导入
import asyncio
import time
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Node, Nodes, Plain, Video

from ..common import (
    SizeLimitExceeded,
    get_douyin_card_path,
    get_douyin_image_path,
    get_douyin_video_path,
)
from . import (
    ANDROID_HEADERS,
    IOS_HEADERS,
    DouyinParseError,
    DouyinResult,
    extract_douyin_links,
)
from .render import DouyinCardRenderer

# endregion


# region 抖音混入
class DouyinMixin:
    # region 下载与路径
    def _build_douyin_path(self, url: str, is_video: bool, request_id: str) -> Path:
        suffix = ".mp4" if is_video else ".jpg"
        base_dir = get_douyin_video_path() if is_video else get_douyin_image_path()
        return base_dir / f"{self._hash_url(url)}_{request_id}{suffix}"

    async def _download_douyin_video(
        self, urls: str | list[str], request_id: str
    ) -> Path:
        candidates = [urls] if isinstance(urls, str) else list(dict.fromkeys(urls))
        if not candidates:
            raise RuntimeError("没有可用的抖音视频地址")
        max_bytes = (
            self.max_video_size_mb * 1024 * 1024 if self.max_video_size_mb > 0 else None
        )
        output_path = self._build_douyin_path(
            candidates[0], is_video=True, request_id=request_id
        )
        last_error: Exception | None = None
        for index, url in enumerate(candidates):
            try:
                size_mb = await self._estimate_total_size_mb(
                    url, None, headers=IOS_HEADERS
                )
                logger.debug(
                    "🎵 估算抖音视频大小: %s MB",
                    f"{size_mb:.2f}" if size_mb is not None else "未知",
                )
                if (
                    size_mb is not None
                    and max_bytes
                    and size_mb * 1024 * 1024 > max_bytes
                ):
                    raise SizeLimitExceeded("超过大小限制")
                await self._download_stream(
                    url,
                    output_path,
                    cookies=None,
                    max_bytes=max_bytes,
                    headers=IOS_HEADERS,
                    retries=max(1, int(getattr(self, "retry_count", 3))),
                )
                logger.debug(
                    "🎵 抖音视频实际使用地址: 候选=%d/%d%s",
                    index + 1,
                    len(candidates),
                    " (已回退)" if index else "",
                )
                return output_path
            except asyncio.CancelledError:
                raise
            except SizeLimitExceeded:
                if index < len(candidates) - 1:
                    logger.warning(
                        "⚠️ 抖音视频候选超过大小限制, 尝试后续低画质/备用地址 (%d/%d)",
                        index + 1,
                        len(candidates),
                    )
                    continue
                raise
            except Exception as exc:
                last_error = exc
                if index < len(candidates) - 1:
                    logger.warning(
                        "⚠️ 抖音视频地址不可用, 尝试候选地址 (%d/%d): %s",
                        index + 1,
                        len(candidates),
                        str(exc),
                    )
        if last_error:
            raise last_error
        raise RuntimeError("抖音视频下载失败")

    async def _download_douyin_image(
        self, urls: str | list[str], request_id: str
    ) -> Path:
        candidates = [urls] if isinstance(urls, str) else list(dict.fromkeys(urls))
        if not candidates:
            raise RuntimeError("没有可用的抖音图片地址")
        output_path = self._build_douyin_path(
            candidates[0], is_video=False, request_id=request_id
        )
        last_error: Exception | None = None
        for index, url in enumerate(candidates):
            try:
                await self._download_stream(
                    url,
                    output_path,
                    cookies=None,
                    max_bytes=None,
                    headers=ANDROID_HEADERS,
                    retries=max(1, int(getattr(self, "retry_count", 3))),
                )
                return output_path
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                if index < len(candidates) - 1:
                    logger.warning(
                        "⚠️ 抖音图片地址不可用, 尝试候选地址 (%d/%d): %s",
                        index + 1,
                        len(candidates),
                        str(exc),
                    )
        if last_error:
            raise last_error
        raise RuntimeError("抖音图片下载失败")

    async def _download_douyin_cover(
        self, cover_url: str, request_id: str
    ) -> Path | None:
        if not cover_url:
            return None
        try:
            # 使用哈希生成文件名
            name = self._hash_url(cover_url)
            cover_path = get_douyin_card_path() / f"{name}_{request_id}_cover.jpg"
            await self._download_stream(
                cover_url,
                cover_path,
                cookies=None,
                max_bytes=None,
                headers=ANDROID_HEADERS,
            )
            return cover_path
        except Exception:
            return None

    def _format_count(self, count: int) -> str:
        if count >= 100000000:
            return f"{count / 100000000:.1f}亿"
        if count >= 10000:
            return f"{count / 10000:.1f}万"
        return str(count)

    @staticmethod
    def _format_duration(seconds: int | None) -> str | None:
        if not seconds or seconds <= 0:
            return None
        total_seconds = int(seconds)
        minutes, secs = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    def _build_douyin_summary(
        self,
        result: DouyinResult,
        *,
        image_count: int,
        dynamic_count: int,
        display_link: str | None = None,
    ) -> str:
        author = result.author or "未知作者"
        title = result.title or "未知标题"
        lines = ["🎵 抖音", f"作者：{author}", f"标题：{title}"]
        if result.likes is not None:
            lines.append(f"点赞：{self._format_count(result.likes)}")
        if result.comments is not None:
            lines.append(f"评论：{self._format_count(result.comments)}")
        duration = DouyinMixin._format_duration(result.duration)
        if duration:
            lines.append(f"时长：{duration}")
        if image_count or dynamic_count:
            media_label = f"图片 {image_count} 张"
            if dynamic_count:
                media_label += f"，动图 {dynamic_count} 个"
        else:
            media_label = "视频"
        lines.append(f"媒体：{media_label}")
        summary_link = display_link or result.source_url
        if summary_link:
            lines.append(f"链接：{summary_link}")
        return "\n".join(lines)

    async def _render_douyin_card(
        self,
        *,
        title: str,
        author: str,
        cover_url: str | None,
        likes: int | None,
        comments: int | None,
        request_id: str,
    ) -> Path | None:
        try:
            cover_path = (
                await self._download_douyin_cover(cover_url, request_id)
                if cover_url
                else None
            )

            renderer = DouyinCardRenderer()

            likes_str = self._format_count(likes) if likes is not None else None
            comments_str = (
                self._format_count(comments) if comments is not None else None
            )

            card_img = await asyncio.to_thread(
                renderer.render,
                title=title,
                author=author,
                cover_path=cover_path,
                likes=likes_str,
                comments=comments_str,
            )

            # 使用标题哈希作为卡片文件名
            name = self._hash_url(title + author)
            card_path = get_douyin_card_path() / f"{name}_{request_id}_card.png"
            # save 操作也放在线程池中
            await asyncio.to_thread(card_img.save, card_path)
            return card_path
        except Exception as exc:
            logger.warning("⚠️ 抖音卡片渲染失败: %s", str(exc))
            return None

    # region 抖音处理
    async def _process_douyin(
        self, event: AstrMessageEvent, target_link: str, is_from_card: bool = False
    ):
        process_start = time.perf_counter()
        timing = {}  # 记录各步骤耗时

        self._refresh_config()
        if not self.douyin_enabled:
            return

        target_link = (target_link or "").strip()

        source_tag = "(来自卡片)" if is_from_card else ""
        request_id = uuid.uuid4().hex[:8]
        await self._send_reaction_emoji(event, source_tag)

        if not target_link:
            logger.warning("⚠️ 抖音链接为空%s", source_tag)
            return
        logger.info("🎵 抖音解析%s: %s", source_tag, target_link)

        # region 解析阶段
        parse_start = time.perf_counter()
        retry_count = getattr(self, "retry_count", 3)
        result = None
        last_error = None

        for attempt in range(retry_count + 1):
            try:
                result = await asyncio.wait_for(
                    self.douyin_extractor.parse(target_link),
                    timeout=25.0,
                )
                break  # 成功则跳出循环
            except asyncio.CancelledError:
                logger.info("♻️ 抖音解析任务已中断%s", source_tag)
                return
            except asyncio.TimeoutError:
                last_error = "超时"
                if attempt < retry_count:
                    logger.warning(
                        "⚠️ 抖音解析超时%s，重试 %d/%d",
                        source_tag,
                        attempt + 1,
                        retry_count,
                    )
                    await asyncio.sleep(1.0)  # 重试前等待
                else:
                    logger.error(
                        "❌ 抖音解析超时%s (已重试%d次)", source_tag, retry_count
                    )
            except DouyinParseError as exc:
                last_error = str(exc)
                if attempt < retry_count:
                    logger.warning(
                        "⚠️ 抖音解析失败%s: %s，重试 %d/%d",
                        source_tag,
                        str(exc),
                        attempt + 1,
                        retry_count,
                    )
                    await asyncio.sleep(1.0)
                else:
                    logger.error(
                        "❌ 抖音解析失败%s: %s (已重试%d次)",
                        source_tag,
                        str(exc),
                        retry_count,
                    )
            except Exception as exc:
                last_error = str(exc)
                if attempt < retry_count:
                    logger.warning(
                        "⚠️ 抖音解析异常%s: %s，重试 %d/%d",
                        source_tag,
                        str(exc),
                        attempt + 1,
                        retry_count,
                    )
                    await asyncio.sleep(1.0)
                else:
                    logger.error(
                        "❌ 抖音解析异常%s: %s (已重试%d次)",
                        source_tag,
                        str(exc),
                        retry_count,
                    )

        timing["parse"] = time.perf_counter() - parse_start

        if result is None:
            logger.error(
                "❌ 抖音解析最终失败%s: %s, 解析耗时=%.2fs",
                source_tag,
                last_error,
                timing["parse"],
            )
            return

        logger.debug(
            "🎵 抖音解析完成%s: 视频=%s, 图片=%d, 动图=%d, 解析耗时=%.2fs",
            source_tag,
            "有" if result.video_url else "无",
            len(result.image_urls),
            len(result.dynamic_urls),
            timing["parse"],
        )
        # endregion

        title = result.title or "未知标题"
        author = result.author or "未知作者"

        if not result.video_url and not result.image_urls and not result.dynamic_urls:
            logger.warning("⚠️ 抖音未找到可下载的媒体%s", source_tag)
            return

        media_components: list[object] = []
        media_paths: list[Path] = []
        failed_images = 0
        failed_dynamics = 0

        image_urls = result.image_urls[: self.douyin_max_media]
        remaining = max(self.douyin_max_media - len(image_urls), 0)
        dynamic_urls = result.dynamic_urls[:remaining]

        # region 下载阶段
        download_start = time.perf_counter()

        if image_urls or dynamic_urls:
            logger.debug(
                "📥 抖音下载开始%s: 图片=%d, 动图=%d",
                source_tag,
                len(image_urls),
                len(dynamic_urls),
            )
            for i, url in enumerate(image_urls):
                try:
                    img_start = time.perf_counter()
                    candidates = (
                        result.image_url_candidates[i]
                        if i < len(result.image_url_candidates)
                        else [url]
                    )
                    image_path = await self._download_douyin_image(
                        candidates, request_id
                    )
                    media_paths.append(image_path)
                    media_components.append(
                        Image.fromFileSystem(str(image_path.resolve()))
                    )
                    logger.debug(
                        "📥 抖音图片下载成功%s [%d/%d]: size=%.1fKB, 耗时=%.2fs",
                        source_tag,
                        i + 1,
                        len(image_urls),
                        image_path.stat().st_size / 1024,
                        time.perf_counter() - img_start,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failed_images += 1
                    logger.warning(
                        "⚠️ 抖音图片下载失败%s [%d/%d]: %s",
                        source_tag,
                        i + 1,
                        len(image_urls),
                        str(exc),
                    )

            for i, url in enumerate(dynamic_urls):
                try:
                    dyn_start = time.perf_counter()
                    candidates = (
                        result.dynamic_url_candidates[i]
                        if i < len(result.dynamic_url_candidates)
                        else [url]
                    )
                    video_path = await self._download_douyin_video(
                        candidates, request_id
                    )
                    media_paths.append(video_path)
                    media_components.append(
                        Video.fromFileSystem(str(video_path.resolve()))
                    )
                    logger.debug(
                        "📥 抖音动图下载成功%s [%d/%d]: size=%.2fMB, 耗时=%.2fs",
                        source_tag,
                        i + 1,
                        len(dynamic_urls),
                        video_path.stat().st_size / 1024 / 1024,
                        time.perf_counter() - dyn_start,
                    )
                except asyncio.CancelledError:
                    raise
                except SizeLimitExceeded:
                    failed_dynamics += 1
                    logger.warning(
                        "⚠️ 抖音动图视频超过大小限制%s [%d/%d]",
                        source_tag,
                        i + 1,
                        len(dynamic_urls),
                    )
                except Exception as exc:
                    failed_dynamics += 1
                    logger.warning(
                        "⚠️ 抖音动图视频下载失败%s [%d/%d]: %s",
                        source_tag,
                        i + 1,
                        len(dynamic_urls),
                        str(exc),
                    )
        elif result.video_url:
            logger.debug("📥 抖音视频下载开始%s...", source_tag)
            try:
                video_start = time.perf_counter()
                video_path = await self._download_douyin_video(
                    result.video_urls or [result.video_url], request_id
                )
                media_paths.append(video_path)
                media_components.append(Video.fromFileSystem(str(video_path.resolve())))
                logger.debug(
                    "📥 抖音视频下载成功%s: size=%.2fMB, 耗时=%.2fs",
                    source_tag,
                    video_path.stat().st_size / 1024 / 1024,
                    time.perf_counter() - video_start,
                )
            except asyncio.CancelledError:
                raise
            except SizeLimitExceeded:
                logger.warning(
                    "⚠️ 抖音视频超过大小限制%s (%dMB)",
                    source_tag,
                    self.max_video_size_mb,
                )
                return
            except Exception as exc:
                logger.error("❌ 抖音视频下载失败%s: %s", source_tag, str(exc))
                return

        timing["download"] = time.perf_counter() - download_start
        # endregion

        if not media_components:
            logger.warning(
                "⚠️ 抖音媒体下载全部失败%s, 下载耗时=%.2fs",
                source_tag,
                timing["download"],
            )
            return

        # Build failure summary (只记录日志，不发送给用户)
        total_failed = failed_images + failed_dynamics
        if total_failed > 0:
            logger.warning(
                "⚠️ 抖音部分媒体下载失败%s: 图片=%d, 动图=%d",
                source_tag,
                failed_images,
                failed_dynamics,
            )

        # 判断是否为图文笔记（有图片或动图）
        is_image_post = bool(image_urls or dynamic_urls)
        # 图文笔记始终合并转发；视频笔记根据配置决定
        enable_merge_send = is_image_post or getattr(self, "douyin_merge_send", True)
        render_card = getattr(self, "douyin_render_card", False)
        summary_text = None
        if enable_merge_send and not render_card:
            summary_text = self._build_douyin_summary(
                result,
                image_count=len(image_urls),
                dynamic_count=len(dynamic_urls),
                display_link=target_link,
            )

        # region 渲染阶段
        render_start = time.perf_counter()
        card_path = None

        if enable_merge_send and render_card:
            card_path = await self._render_douyin_card(
                title=title,
                author=author,
                cover_url=result.cover_url,
                likes=result.likes,
                comments=result.comments,
                request_id=request_id,
            )
        timing["render"] = time.perf_counter() - render_start
        # endregion

        # region 发送阶段
        send_start = time.perf_counter()

        if enable_merge_send:
            # 合并转发：摘要 + 媒体
            nodes = Nodes([])
            sender_uin = self._get_merge_sender_uin(event)

            if summary_text:
                nodes.nodes.append(Node(uin=sender_uin, content=[Plain(summary_text)]))
            elif card_path and card_path.exists():
                nodes.nodes.append(
                    Node(
                        uin=sender_uin,
                        content=[Image.fromFileSystem(str(card_path.resolve()))],
                    )
                )

            for component in media_components:
                merge_component = await self._prepare_component_for_merge_send(
                    component
                )
                nodes.nodes.append(Node(uin=sender_uin, content=[merge_component]))

            logger.debug(
                "🚀 抖音合并消息准备发送%s: 节点数=%d", source_tag, len(nodes.nodes)
            )
            await event.send(MessageChain([nodes]))
        else:
            # 非合并转发（仅视频笔记可能走到这里）：只发送单独视频
            logger.debug(
                "🚀 抖音普通消息准备发送%s: 媒体数=%d",
                source_tag,
                len(media_components),
            )
            await event.send(MessageChain([media_components[0]]))

        timing["send"] = time.perf_counter() - send_start
        # endregion

        # 输出完整耗时日志
        total_elapsed = time.perf_counter() - process_start
        logger.info(
            "🎵 抖音处理完成%s: 标题=%s, 媒体=%d, 失败=%d | 耗时: 解析=%.2fs, 下载=%.2fs, 渲染=%.2fs, 发送=%.2fs, 总计=%.2fs",
            source_tag,
            title[:20],
            len(media_components),
            total_failed,
            timing.get("parse", 0),
            timing.get("download", 0),
            timing.get("render", 0),
            timing.get("send", 0),
            total_elapsed,
        )
        # 发送完成后立即清理文件（Direct Send Pattern：此时文件已被读取）
        if media_paths:
            await self.cleanup_files(media_paths, [])

    # endregion

    # region 事件处理器
    # 事件过滤器由 main.py 注册，确保绑定插件实例。
    async def handle_douyin(self, event: AstrMessageEvent):
        if not self.douyin_enabled:
            return
        if self._is_self_message(event):
            return
        if await self._is_bot_muted(event):
            return
        event.should_call_llm(True)
        links = extract_douyin_links(event.message_str)
        logger.info("🎵 抖音匹配链接: %s", links)
        if not links:
            return
        try:
            await self._process_douyin(event, links[0], is_from_card=False)
        except asyncio.CancelledError:
            logger.info("♻️ 抖音解析任务已中断")
            return

        # endregion

        # endregion
        return

    # endregion


# endregion
