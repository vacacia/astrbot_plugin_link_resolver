# region 导入
import asyncio
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import File, Image, Node, Nodes, Plain, Video

from ..common import (
    SizeLimitExceeded,
    get_xhs_card_path,
    get_xhs_image_path,
    get_xhs_video_path,
)
from . import (
    XHS_HEADERS,
    XiaohongshuParseError,
    XiaohongshuResult,
    XiaohongshuRetryableError,
    extract_xhs_links,
)
from .extractor import _XHS_DOWNLOAD_UA

# endregion

# region 解析策略常量
XHS_PARSE_TIMEOUT_SEC = 30.0
XHS_PARSE_RETRY_BASE_DELAY_SEC = 1.0
XHS_PARSE_RETRY_MAX_DELAY_SEC = 8.0
# endregion


# region 小红书混入
class XiaohongshuMixin:
    # region 路径与候选构建
    def _build_xhs_path(self, url: str, is_video: bool, request_id: str) -> Path:
        suffix = ".mp4" if is_video else self._guess_media_suffix(url, ".jpg")
        base_dir = get_xhs_video_path() if is_video else get_xhs_image_path()
        return base_dir / f"{self._hash_url(url)}_{request_id}{suffix}"

    def _build_xhs_card_path(self, source_url: str, request_id: str) -> Path:
        return (
            get_xhs_card_path() / f"{self._hash_url(source_url)}_{request_id}_card.png"
        )

    @staticmethod
    def _force_https(url: str) -> str:
        try:
            parsed = urlparse(url)
        except Exception:
            return url
        if parsed.scheme in ("", "http"):
            return parsed._replace(scheme="https").geturl()
        return url

    # endregion

    # region 下载与渲染
    @staticmethod
    def _xhs_download_headers(referer: str | None) -> dict[str, str]:
        headers = dict(XHS_HEADERS)
        if referer:
            headers["Referer"] = referer
        headers["Origin"] = "https://www.xiaohongshu.com"
        headers["Accept"] = "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
        headers["Accept-Language"] = "zh-CN,zh;q=0.9"
        return headers

    @staticmethod
    def _is_retryable_xhs_exception(exc: Exception) -> bool:
        if isinstance(exc, (asyncio.TimeoutError, XiaohongshuRetryableError)):
            return True
        text = str(exc).lower()
        retryable_patterns = (
            "timeout",
            "timed out",
            "connection",
            "reset",
            "refused",
            "temporary",
            "unavailable",
            "503",
            "502",
            "504",
            "429",
            "network",
        )
        return any(p in text for p in retryable_patterns)

    async def _download_xhs_video(
        self, url: str, request_id: str, referer: str | None = None
    ) -> Path:
        max_bytes = (
            self.max_video_size_mb * 1024 * 1024 if self.max_video_size_mb > 0 else None
        )
        size_mb = await self._estimate_total_size_mb(
            url, None, headers=self._xhs_download_headers(referer)
        )
        logger.debug(
            "📹 估算小红书视频大小: %s MB",
            f"{size_mb:.2f}" if size_mb is not None else "未知",
        )
        if size_mb is not None and max_bytes and size_mb * 1024 * 1024 > max_bytes:
            raise SizeLimitExceeded("超过大小限制")
        output_path = self._build_xhs_path(url, is_video=True, request_id=request_id)
        await self._download_stream(
            url,
            output_path,
            cookies=None,
            max_bytes=max_bytes,
            headers=self._xhs_download_headers(referer),
            retries=3,
        )
        return output_path

    async def _download_xhs_image(
        self,
        url: str,
        request_id: str,
        file_id: str | None = None,
        referer: str | None = None,
    ) -> Path:
        """下载图片 - 两级回退策略

        1. 如果开启原图下载：按配置顺序尝试 CDN 原图 / CI-PNG 转码
           （xhs_prefer_ci_png=True 时 CI-PNG 优先，否则 CDN 优先）
        2. 如果都失败：回退到多 CDN 兜底策略
        """
        start_time = time.perf_counter()

        output_path = self._build_xhs_path(url, is_video=False, request_id=request_id)

        # 提取 image token (参考 XHS-Downloader)
        token = self._extract_image_token(url)
        logger.debug(
            "XHS 图片下载开始: url=%s, file_id=%s, token=%s", url[:80], file_id, token
        )

        # region 原图下载尝试
        if getattr(self, "xhs_download_original", True) and token:
            original_start = time.perf_counter()

            # 构建原图 URL 候选列表
            original_candidates = []

            # CDN 直接链接 - 返回小红书存储的原始文件（通常为 JPEG）
            cdn_domains = [
                "sns-img-bd.xhscdn.com",
                "sns-img-qc.xhscdn.com",
                "sns-img-hw.xhscdn.com",
            ]
            cdn_candidates = [
                {
                    "url": f"https://{domain}/{token}",
                    "desc": f"CDN-{domain.split('-')[2].split('.')[0]}-auto",
                    "format": None,
                }
                for domain in cdn_domains
            ]

            # CI 转码链接 - 通过腾讯云 CI 将图片转为 PNG（体积约为原图 2 倍，画质一致）
            ci_candidates = [
                {
                    "url": f"https://ci.xiaohongshu.com/{token}?imageView2/format/png",
                    "desc": "CI-PNG-原图",
                    "format": "png",
                },
            ]

            if getattr(self, "xhs_prefer_ci_png", False):
                original_candidates = ci_candidates + cdn_candidates
            else:
                original_candidates = cdn_candidates + ci_candidates

            retry_count = max(0, int(getattr(self, "retry_count", 3)))
            for cand in original_candidates:
                cand_url = cand["url"]
                desc = cand["desc"]
                format_name = cand["format"]

                for attempt in range(retry_count + 1):
                    attempt_start = time.perf_counter()
                    try:
                        timeout = aiohttp.ClientTimeout(total=600, connect=60)
                        headers = {
                            "User-Agent": _XHS_DOWNLOAD_UA,
                            "Referer": "https://www.xiaohongshu.com/",
                        }

                        async with aiohttp.ClientSession(
                            headers=headers, timeout=timeout
                        ) as session:
                            async with session.get(cand_url) as resp:
                                attempt_elapsed = time.perf_counter() - attempt_start

                                if resp.status == 200:
                                    # 先写入临时文件，避免一次性读取导致 payload 不完整
                                    temp_output = output_path.with_suffix(".tmp")
                                    temp_path = temp_output.with_suffix(
                                        temp_output.suffix + ".part"
                                    )
                                    content_len = 0
                                    f = None
                                    try:

                                        def _open_temp():
                                            temp_path.parent.mkdir(
                                                parents=True, exist_ok=True
                                            )
                                            return open(temp_path, "wb")

                                        f = await asyncio.to_thread(_open_temp)
                                        try:
                                            async for (
                                                chunk
                                            ) in resp.content.iter_chunked(256 * 1024):
                                                if not chunk:
                                                    continue
                                                content_len += len(chunk)
                                                await asyncio.to_thread(f.write, chunk)
                                        finally:
                                            if f is not None:
                                                await asyncio.to_thread(f.close)

                                        # 验证文件大小（至少 10KB 才认为是有效图片）
                                        if (
                                            content_len >= 10 * 1024
                                            and temp_path.exists()
                                        ):
                                            # 确定输出文件后缀
                                            if format_name:
                                                actual_suffix = f".{format_name}"
                                            else:

                                                def _read_head():
                                                    with open(temp_path, "rb") as rf:
                                                        return rf.read(32)

                                                head = await asyncio.to_thread(
                                                    _read_head
                                                )
                                                actual_suffix = (
                                                    self._detect_image_suffix(
                                                        head,
                                                        resp.headers.get(
                                                            "Content-Type"
                                                        ),
                                                    )
                                                )

                                            final_output = output_path.with_suffix(
                                                actual_suffix
                                            )
                                            final_part = final_output.with_suffix(
                                                final_output.suffix + ".part"
                                            )

                                            def _move():
                                                if final_part.exists():
                                                    final_part.unlink()
                                                temp_path.replace(final_part)
                                                final_part.replace(final_output)

                                            await asyncio.to_thread(_move)

                                            total_elapsed = (
                                                time.perf_counter() - start_time
                                            )
                                            logger.debug(
                                                "XHS 原图下载成功 (%s): size=%.1fMB, 请求耗时=%.2fs, 总耗时=%.2fs",
                                                desc,
                                                content_len / 1024 / 1024,
                                                attempt_elapsed,
                                                total_elapsed,
                                            )
                                            return final_output
                                        else:
                                            logger.debug(
                                                "XHS 原图响应过小 (%s): size=%d bytes, 耗时=%.2fs",
                                                desc,
                                                content_len,
                                                attempt_elapsed,
                                            )
                                    finally:
                                        if (
                                            temp_path.exists()
                                            and content_len < 10 * 1024
                                        ):
                                            try:
                                                temp_path.unlink()
                                            except Exception:
                                                pass
                                else:
                                    logger.debug(
                                        "XHS 原图请求失败 (%s): HTTP %d, 耗时=%.2fs",
                                        desc,
                                        resp.status,
                                        attempt_elapsed,
                                    )
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        attempt_elapsed = time.perf_counter() - attempt_start
                        logger.debug(
                            "XHS 原图下载异常 (%s): %s, 耗时=%.2fs",
                            desc,
                            str(e)[:50],
                            attempt_elapsed,
                        )

                    if attempt < retry_count:
                        wait_time = 0.5 * (2**attempt)
                        await asyncio.sleep(wait_time)

            original_elapsed = time.perf_counter() - original_start
            logger.debug(
                "XHS 原图下载全部失败，回退到普通策略，原图尝试耗时=%.2fs",
                original_elapsed,
            )
        # endregion

        # region CDN 兜底策略

        # 基础 Headers
        base_headers = {
            "User-Agent": _XHS_DOWNLOAD_UA,
        }

        # 构建阶梯候选列表
        candidates = []

        # 1. 原始 URL (带签名)
        raw_url = (
            url.replace("http://", "https://", 1) if url.startswith("http://") else url
        )
        candidates.append({"url": raw_url, "desc": "Raw"})

        effective_id = file_id or token
        if effective_id:
            # 2. 无签名通用 CDN (兜底方案)
            domains = [
                "sns-img-bd.xhscdn.com",
                "sns-img-qc.xhscdn.com",
                "sns-img-hw.xhscdn.com",
                "sns-webpic-qc.xhscdn.com",
            ]
            for domain in domains:
                for path_prefix in ["", "spectrum/"]:
                    path = f"{path_prefix}{effective_id}"
                    candidates.append(
                        {
                            "url": f"https://{domain}/{path}",
                            "desc": f"CDN-{domain.split('.')[0]}",
                        }
                    )

        errors = []
        retry_count = max(0, int(getattr(self, "retry_count", 3)))
        for cand in candidates:
            cand_url = cand["url"]
            desc = cand["desc"]

            # 两种 header 变体
            header_variants = [
                {**base_headers, "Referer": "https://www.xiaohongshu.com/"},
                base_headers.copy(),
            ]

            for hv in header_variants:
                for attempt in range(retry_count + 1):
                    attempt_start = time.perf_counter()
                    try:
                        # 超长超时
                        timeout = aiohttp.ClientTimeout(total=300, connect=30)
                        async with aiohttp.ClientSession(
                            headers=hv, timeout=timeout
                        ) as session:
                            async with session.get(cand_url) as resp:
                                if resp.status == 200:
                                    temp_path = output_path.with_suffix(
                                        output_path.suffix + ".part"
                                    )
                                    content_len = 0
                                    f = None
                                    try:

                                        def _open_part():
                                            temp_path.parent.mkdir(
                                                parents=True, exist_ok=True
                                            )
                                            return open(temp_path, "wb")

                                        f = await asyncio.to_thread(_open_part)
                                        try:
                                            async for (
                                                chunk
                                            ) in resp.content.iter_chunked(256 * 1024):
                                                if not chunk:
                                                    continue
                                                content_len += len(chunk)
                                                await asyncio.to_thread(f.write, chunk)
                                        finally:
                                            if f is not None:
                                                await asyncio.to_thread(f.close)

                                        if content_len >= 1024 and temp_path.exists():

                                            def _move():
                                                temp_path.replace(output_path)

                                            await asyncio.to_thread(_move)

                                            attempt_elapsed = (
                                                time.perf_counter() - attempt_start
                                            )
                                            total_elapsed = (
                                                time.perf_counter() - start_time
                                            )
                                            logger.debug(
                                                "📥 XHS CDN 图片下载成功 (%s): size=%.1fKB, 请求耗时=%.2fs, 总耗时=%.2fs",
                                                desc,
                                                content_len / 1024,
                                                attempt_elapsed,
                                                total_elapsed,
                                            )
                                            return output_path
                                        else:
                                            logger.debug(
                                                "XHS CDN 图片过小，忽略 (%s): size=%d bytes",
                                                desc,
                                                content_len,
                                            )
                                            errors.append(
                                                f"{desc}: 文件过小 ({content_len} bytes)"
                                            )
                                            if temp_path.exists():
                                                await asyncio.to_thread(
                                                    temp_path.unlink
                                                )
                                    except asyncio.CancelledError:
                                        raise
                                    except Exception:
                                        if temp_path.exists():
                                            try:
                                                await asyncio.to_thread(
                                                    temp_path.unlink
                                                )
                                            except Exception:
                                                pass
                                        raise

                                errors.append(f"{desc}: HTTP {resp.status}")
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        errors.append(f"{desc}: {str(e)[:20]}")

                    if attempt < retry_count:
                        wait_time = 0.5 * (2**attempt)
                        await asyncio.sleep(wait_time)
        # endregion

        # 全部失败
        total_elapsed = time.perf_counter() - start_time
        error_summary = " | ".join(errors[:5])  # 只取前5个错误
        logger.error(
            "❌ XHS 图片下载全线失败: 总耗时=%.2fs, 错误=%s",
            total_elapsed,
            error_summary,
        )
        raise RuntimeError(f"图片下载失败: {error_summary}")

    @staticmethod
    def _extract_image_token(url: str) -> str | None:
        """从 URL 中提取 image token（参考 XHS-Downloader）"""
        if not url:
            return None
        try:
            # 格式: https://xxx.xhscdn.com/spectrum/1040g0k... 或类似
            # 提取路径第5个/之后的部分，去掉!后缀
            parts = url.split("/")
            if len(parts) >= 6:
                token = "/".join(parts[5:]).split("!")[0].split("?")[0]
                if token and len(token) > 10:
                    return token
            # 备用方案：直接取最后一段
            last_part = url.split("/")[-1].split("!")[0].split("?")[0]
            if last_part and len(last_part) > 10:
                return last_part
        except Exception:
            pass
        return None

    @staticmethod
    def _detect_image_suffix(content: bytes, content_type: str | None) -> str:
        """从文件签名或 Content-Type 检测图片格式"""
        # 文件签名检测（魔数）
        if content[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if content[:3] == b"\xff\xd8\xff":
            return ".jpeg"
        if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            return ".webp"
        if content[:4] == b"GIF8":
            return ".gif"
        if content[4:12] in (b"ftypavif", b"ftypavis"):
            return ".avif"
        if content[4:12] in (b"ftypheic", b"ftypmif1"):
            return ".heic"

        # Content-Type 检测
        if content_type:
            ct = content_type.lower()
            if "png" in ct:
                return ".png"
            if "jpeg" in ct or "jpg" in ct:
                return ".jpeg"
            if "webp" in ct:
                return ".webp"
            if "gif" in ct:
                return ".gif"

        # 默认 jpeg
        return ".jpeg"

    @staticmethod
    def _clean_xhs_summary_text(text: str) -> str:
        # 小红书原始 desc / shareDesc 的话题标签通常形如 #xxx[话题]#。
        # 文字摘要直接展示原文时会把这个内部标记带出来，因此仅在摘要里清洗成 #xxx#。
        return re.sub(r"#([^#\n]+?)\[话题\]#", r"#\1#", text)

    @staticmethod
    def _clean_xhs_display_link(link: str) -> str:
        # 仅清洗摘要展示用链接，不影响真实解析请求。
        # JSON 卡片里的 jumpUrl 常带 app_platform/xsec_token/share_channel 等参数，展示时去掉后缀更简洁。
        try:
            parsed = urlparse(link)
        except Exception:
            return link
        host = parsed.netloc.lower()
        if "xiaohongshu.com" not in host:
            return link
        return parsed._replace(query="", fragment="").geturl()

    def _build_xhs_summary(
        self,
        result: XiaohongshuResult,
        *,
        image_count: int,
        is_video: bool,
        display_link: str | None = None,
    ) -> str:
        author = result.author or "未知作者"
        title = result.title or "未知标题"
        lines = ["🍠 小红书", f"作者：{author}", f"标题：{title}"]
        if result.text:
            lines.append(
                f"正文：{XiaohongshuMixin._clean_xhs_summary_text(result.text)}"
            )
        lines.append(f"媒体：{'视频' if is_video else f'图片 {image_count} 张'}")
        summary_link = display_link or result.source_url
        if summary_link:
            lines.append(
                f"链接：{XiaohongshuMixin._clean_xhs_display_link(summary_link)}"
            )
        return "\n".join(lines)

    async def _render_xhs_card(
        self,
        result: XiaohongshuResult,
        image_paths: list[Path],
        cover_path: Path | None,
        is_video: bool,
        request_id: str,
    ) -> Path | None:
        try:
            card_path = self._build_xhs_card_path(result.source_url, request_id)
            image = await asyncio.to_thread(
                self.xhs_renderer.render,
                title=result.title,
                author=result.author,
                text=result.text,
                image_paths=image_paths,
                cover_path=cover_path,
                is_video=is_video,
            )
            await asyncio.to_thread(image.save, card_path, format="PNG")
            return card_path
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("⚠️ 小红书卡片渲染失败: %s", str(exc))
            return None

    # endregion

    # region 小红书处理
    async def _process_xhs(
        self, event: AstrMessageEvent, target_link: str, is_from_card: bool = False
    ):
        process_start = time.perf_counter()
        timing = {}  # 记录各步骤耗时

        self._refresh_config()
        if not self.xhs_enabled:
            return
        source_tag = "(来自卡片)" if is_from_card else ""
        request_id = uuid.uuid4().hex[:8]

        await self._send_reaction_emoji(event, source_tag)

        target_link = (target_link or "").strip()

        if not target_link:
            logger.warning("⚠️ 小红书链接为空%s", source_tag)
            return
        logger.info("🍠 小红书解析%s: %s", source_tag, target_link)

        # region 解析阶段
        parse_start = time.perf_counter()
        retry_count = max(0, int(getattr(self, "retry_count", 3)))
        result: XiaohongshuResult | None = None
        last_error: Exception | None = None

        for attempt in range(retry_count + 1):
            try:
                result = await asyncio.wait_for(
                    self.xhs_extractor.parse(target_link),
                    timeout=XHS_PARSE_TIMEOUT_SEC,
                )
                break
            except asyncio.CancelledError:
                logger.info("♻️ 小红书解析任务已中断%s", source_tag)
                return
            except XiaohongshuParseError as exc:
                last_error = exc
                if attempt < retry_count and self._is_retryable_xhs_exception(exc):
                    wait_time = min(
                        XHS_PARSE_RETRY_MAX_DELAY_SEC,
                        XHS_PARSE_RETRY_BASE_DELAY_SEC * (2**attempt),
                    )
                    logger.warning(
                        "⚠️ 小红书解析失败%s: %s，%.1fs后重试 (%d/%d)",
                        source_tag,
                        str(exc),
                        wait_time,
                        attempt + 1,
                        retry_count,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                logger.error("❌ 小红书解析失败%s: %s", source_tag, str(exc))
                return
            except Exception as exc:
                last_error = exc
                if attempt < retry_count and self._is_retryable_xhs_exception(exc):
                    wait_time = min(
                        XHS_PARSE_RETRY_MAX_DELAY_SEC,
                        XHS_PARSE_RETRY_BASE_DELAY_SEC * (2**attempt),
                    )
                    logger.warning(
                        "⚠️ 小红书解析异常%s: %s，%.1fs后重试 (%d/%d)",
                        source_tag,
                        str(exc),
                        wait_time,
                        attempt + 1,
                        retry_count,
                    )
                    await asyncio.sleep(wait_time)
                    continue
                logger.error("❌ 小红书解析异常%s: %s", source_tag, str(exc))
                return

        if result is None:
            logger.error(
                "❌ 小红书解析最终失败%s: %s, link=%s, timeout=%.0fs, retries=%d",
                source_tag,
                str(last_error) if last_error else "unknown",
                target_link,
                XHS_PARSE_TIMEOUT_SEC,
                retry_count,
            )
            return

        timing["parse"] = time.perf_counter() - parse_start
        # endregion

        logger.debug(
            "🍠 小红书解析完成%s: 视频=%s, 图片=%s, 解析耗时=%.2fs",
            source_tag,
            "有" if result.video_url else "无",
            len(result.image_urls),
            timing["parse"],
        )

        title = result.title or "未知标题"

        if not result.video_url and not result.image_urls:
            logger.warning("⚠️ 小红书未找到可下载的媒体%s: %s", source_tag, target_link)
            return

        media_components: list[object] = []
        media_paths: list[Path] = []
        image_paths: list[Path] = []
        cover_path: Path | None = None
        failed_images = 0

        # region 下载阶段
        download_start = time.perf_counter()

        # 视频笔记：优先下载视频
        if result.video_url:
            try:
                video_path = await self._download_xhs_video(
                    result.video_url, request_id, referer=result.source_url
                )
                media_paths.append(video_path)
                media_components.append(Video.fromFileSystem(str(video_path.resolve())))
                # 下载封面图
                cover_url = result.cover_url or (
                    result.image_urls[0] if result.image_urls else None
                )
                if cover_url:
                    try:
                        cover_path = await self._download_xhs_image(
                            cover_url, request_id, referer=result.source_url
                        )
                        media_paths.append(cover_path)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "⚠️ 小红书封面下载失败%s: %s", source_tag, str(exc)
                        )
            except asyncio.CancelledError:
                raise
            except SizeLimitExceeded:
                logger.warning(
                    "⚠️ 小红书视频大小超过限制%s (%dMB)",
                    source_tag,
                    self.max_video_size_mb,
                )
                return
            except Exception as exc:
                logger.error("❌ 小红书视频下载失败%s: %s", source_tag, str(exc))
                return
        # 图片笔记：下载图片
        elif result.image_urls:
            image_urls = result.image_urls[: self.xhs_max_media]
            file_ids = result.file_ids[: self.xhs_max_media] if result.file_ids else []
            if getattr(self, "xhs_concurrent_download", False):
                # 并发下载
                async def _download_one(
                    i: int, url: str
                ) -> tuple[int, Path | None, Exception | None]:
                    file_id = file_ids[i] if i < len(file_ids) else None
                    try:
                        path = await self._download_xhs_image(
                            url, request_id, file_id=file_id, referer=result.source_url
                        )
                        return (i, path, None)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        return (i, None, exc)

                dl_results = await asyncio.gather(
                    *[_download_one(i, url) for i, url in enumerate(image_urls)]
                )
                # 按原始顺序组装结果
                for i, path, exc in dl_results:
                    if path is not None:
                        image_paths.append(path)
                        media_paths.append(path)
                        media_components.append(
                            Image.fromFileSystem(str(path.resolve()))
                        )
                    else:
                        failed_images += 1
                        logger.warning(
                            "⚠️ 小红书图片下载失败%s [%d/%d]: %s",
                            source_tag,
                            i + 1,
                            len(image_urls),
                            str(exc),
                        )
            else:
                # 串行下载
                for i, url in enumerate(image_urls):
                    try:
                        file_id = file_ids[i] if i < len(file_ids) else None
                        image_path = await self._download_xhs_image(
                            url, request_id, file_id=file_id, referer=result.source_url
                        )
                        image_paths.append(image_path)
                        media_paths.append(image_path)
                        media_components.append(
                            Image.fromFileSystem(str(image_path.resolve()))
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        failed_images += 1
                        logger.warning(
                            "⚠️ 小红书图片下载失败%s [%d/%d]: %s",
                            source_tag,
                            i + 1,
                            len(image_urls),
                            str(exc),
                        )

        timing["download"] = time.perf_counter() - download_start
        # endregion

        if not media_components:
            logger.debug(
                "XHS 无媒体下载成功%s: url=%s, 失败图片=%d, 下载耗时=%.2fs",
                source_tag,
                result.source_url,
                failed_images,
                timing["download"],
            )
            return

        summary_enabled = bool(image_paths) or bool(
            result.video_url and self.xhs_merge_send
        )
        render_card = getattr(self, "xhs_render_card", False)
        is_video_post = bool(result.video_url and not image_paths)
        summary_text = None
        if summary_enabled and not render_card:
            summary_text = self._build_xhs_summary(
                result,
                image_count=len(image_paths),
                is_video=is_video_post,
                display_link=target_link,
            )

        # region 渲染阶段
        render_start = time.perf_counter()
        card_path = None
        if summary_enabled and render_card:
            card_path = await self._render_xhs_card(
                result,
                image_paths=image_paths,
                cover_path=cover_path,
                is_video=is_video_post,
                request_id=request_id,
            )
        if card_path:
            media_paths.append(card_path)
            media_components.insert(0, Image.fromFileSystem(str(card_path.resolve())))
        timing["render"] = time.perf_counter() - render_start
        # endregion

        # region 发送阶段
        send_start = time.perf_counter()

        # 转换超过QQ限制的图片为文件组件
        def _convert_to_file_if_needed(component):
            """如果图片超过QQ限制，转为 File 组件上传"""
            if (
                isinstance(component, Image)
                and hasattr(component, "path")
                and component.path
            ):
                try:
                    qq_image_size_limit_mb = getattr(
                        self, "xhs_qq_image_size_limit_mb", 30
                    )
                    if qq_image_size_limit_mb <= 0:
                        return component
                    file_size = Path(component.path).stat().st_size
                    if file_size > qq_image_size_limit_mb * 1024 * 1024:
                        file_name = Path(component.path).name
                        logger.info(
                            "XHS 图片 %.1fMB 超过 %dMB QQ限制，转为文件上传: %s",
                            file_size / 1024 / 1024,
                            qq_image_size_limit_mb,
                            file_name,
                        )
                        return File(file=component.path, name=file_name)
                except Exception:
                    pass
            return component

        media_components = [_convert_to_file_if_needed(c) for c in media_components]

        # 计算总大小
        total_size_bytes = await asyncio.to_thread(
            lambda: sum(p.stat().st_size for p in media_paths if p.exists())
        )
        total_size_mb = total_size_bytes / (1024 * 1024)

        # 判断是否触发解合阈值
        threshold = getattr(self, "xhs_auto_unmerge_threshold_mb", 20)
        force_unmerge = False
        is_image_post = bool(image_paths)
        if is_image_post and threshold > 0 and total_size_mb > threshold:
            is_tool_invocation = getattr(event, "is_tool_invocation", None)
            should_force_resolve = callable(is_tool_invocation) and is_tool_invocation()
            if not should_force_resolve:
                notice = (
                    f"这篇有 {len(image_paths)} 张图片, 共 {total_size_mb:.2f} MB, "
                    "全部展开会很大喵, 真的要解析吗?"
                )
                logger.info(
                    "XHS 媒体总大小 (%.2fMB) 超过阈值 (%dMB), 自动解析改为询问",
                    total_size_mb,
                    threshold,
                )
                await self.cleanup_files(media_paths, [])
                yield event.plain_result(notice)
                return
            logger.info(
                "XHS 媒体总大小 (%.2fMB) 超过阈值 (%dMB), 工具解析强制逐条发送",
                total_size_mb,
                threshold,
            )
            force_unmerge = True

        # 图文笔记始终合并转发；视频笔记根据配置决定
        # force_unmerge 仅对图文笔记生效（逐条发送图片）
        if is_image_post:
            # 图文笔记：始终合并转发（除非触发解合阈值）
            should_merge = not force_unmerge
        else:
            # 视频笔记：根据配置决定
            should_merge = self.xhs_merge_send

        if should_merge:
            nodes = Nodes([])
            sender_uin = self._get_merge_sender_uin(event)
            if summary_text:
                nodes.nodes.append(Node(uin=sender_uin, content=[Plain(summary_text)]))
            for component in media_components:
                merge_component = await self._prepare_component_for_merge_send(
                    component
                )
                nodes.nodes.append(Node(uin=sender_uin, content=[merge_component]))
            yield event.chain_result([nodes])
        else:
            if is_image_post:
                # 图文笔记逐条发送（触发解合阈值时）
                if summary_text:
                    yield event.chain_result([Plain(summary_text)])
                for i, component in enumerate(media_components):
                    yield event.chain_result([component])
                    if i < len(media_components) - 1:
                        await asyncio.sleep(2.0)
            else:
                # 视频笔记不合并发送：只发送视频（不含卡片）
                # 找到视频组件（第一个非卡片的组件）
                for component in media_components:
                    if isinstance(component, Video):
                        yield event.chain_result([component])
                        break

        timing["send"] = time.perf_counter() - send_start
        # endregion

        # 输出完整耗时日志
        total_elapsed = time.perf_counter() - process_start
        logger.info(
            "🍠 XHS 处理完成%s: 标题=%s, 媒体=%d, 失败=%d | 耗时: 解析=%.2fs, 下载=%.2fs, 渲染=%.2fs, 发送=%.2fs, 总计=%.2fs",
            source_tag,
            title[:20],
            len(media_components),
            failed_images,
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
    async def handle_xhs(self, event: AstrMessageEvent):
        if not self.xhs_enabled:
            return
        if self._is_self_message(event):
            return
        if await self._is_bot_muted(event):
            return
        event.should_call_llm(True)
        links = extract_xhs_links(event.message_str)
        logger.info("🍠 小红书匹配链接: %s", links)
        if not links:
            return
        try:
            async for result in self._process_xhs(event, links[0], is_from_card=False):
                yield result
        except asyncio.CancelledError:
            logger.info("♻️ 小红书解析任务已中断")
            return

    # endregion


# endregion
