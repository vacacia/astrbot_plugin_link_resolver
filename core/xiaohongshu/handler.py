# region 导入
import asyncio
import re
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image, Node, Nodes, Video

from ..common import XHS_VIDEO_PATH, XHS_IMAGE_PATH, XHS_CARD_PATH, SizeLimitExceeded
from . import (
    XHS_HEADERS,
    XHS_MESSAGE_PATTERN,
    XiaohongshuParseError,
    XiaohongshuResult,
    extract_xhs_links,
    load_xhs_cookies,
)
# endregion


# region 小红书混入
class XiaohongshuMixin:
    # region 路径与候选构建
    def _build_xhs_path(self, url: str, is_video: bool) -> Path:
        suffix = ".mp4" if is_video else self._guess_media_suffix(url, ".jpg")
        base_dir = XHS_VIDEO_PATH if is_video else XHS_IMAGE_PATH
        return base_dir / f"{self._hash_url(url)}{suffix}"

    def _build_xhs_card_path(self, source_url: str) -> Path:
        return XHS_CARD_PATH / f"{self._hash_url(source_url)}_card.png"

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

    def _get_xhs_cookies(self) -> dict[str, str]:
        """获取小红书 cookies（根据配置决定是否使用）"""
        # 检查配置是否启用 cookies
        if not getattr(self, 'xhs_use_cookies', False):
            return {}
        if not hasattr(self, "_xhs_cookies_cache"):
            self._xhs_cookies_cache = load_xhs_cookies()
        return self._xhs_cookies_cache

    def _try_extend_aiocqhttp_timeout(self, event: AstrMessageEvent, timeout_sec: float) -> None:
        """尝试延长 aiocqhttp API 超时时间（用于发送大图）
        
        路径: event.bot._api._wsr_api._timeout_sec
        """
        try:
            bot = getattr(event, 'bot', None)
            if not bot:
                return
            api = getattr(bot, '_api', None)
            if not api:
                return
            wsr_api = getattr(api, '_wsr_api', None)
            if not wsr_api:
                return
            current_timeout = getattr(wsr_api, '_timeout_sec', 180)
            if current_timeout < timeout_sec:
                wsr_api._timeout_sec = timeout_sec
                logger.debug("aiocqhttp API 超时已从 %.0fs 延长到 %.0fs", current_timeout, timeout_sec)
        except Exception as e:
            logger.debug("无法修改 aiocqhttp 超时: %s", str(e))

    async def _download_xhs_video(self, url: str, referer: str | None = None) -> Path:
        max_bytes = self.max_video_size_mb * 1024 * 1024 if self.max_video_size_mb > 0 else None
        cookies = self._get_xhs_cookies()
        size_mb = await self._estimate_total_size_mb(
            url, None, headers=self._xhs_download_headers(referer), cookies=cookies
        )
        logger.info(
            "📹 估算小红书视频大小: %s MB",
            f"{size_mb:.2f}" if size_mb is not None else "未知",
        )
        if size_mb is not None and max_bytes and size_mb * 1024 * 1024 > max_bytes:
            raise SizeLimitExceeded("超过大小限制")
        output_path = self._build_xhs_path(url, is_video=True)
        await self._download_stream(
            url,
            output_path,
            cookies=cookies,
            max_bytes=max_bytes,
            headers=self._xhs_download_headers(referer),
            retries=3,
        )
        return output_path

    async def _download_xhs_image(
        self, 
        url: str, 
        file_id: str | None = None,
        referer: str | None = None
    ) -> Path:
        """下载图片 - 三级回退策略
        
        1. 如果开启原图下载：尝试 PNG 原图 (ci.xiaohongshu.com)
        2. 如果 PNG 失败：尝试 JPEG 原图
        3. 如果都失败：回退到多 CDN 兜底策略
        """
        import time as time_module
        start_time = time_module.perf_counter()
        
        output_path = self._build_xhs_path(url, is_video=False)
        cookies = self._get_xhs_cookies()
        
        # 提取 image token (参考 XHS-Downloader)
        token = self._extract_image_token(url)
        logger.debug("XHS 图片下载开始: url=%s, file_id=%s, token=%s", url[:80], file_id, token)
        
        # region 原图下载尝试
        if getattr(self, 'xhs_download_original', True) and token:
            original_start = time_module.perf_counter()
            
            # 构建原图 URL 候选列表
            # 策略：优先直接 CDN 链接（无压缩原图），再尝试 imageView2 格式转换
            original_candidates = []
            
            # 1. 直接 CDN 链接 - 获取未压缩的原始文件（XHS-Downloader 的 auto 模式）
            cdn_domains = [
                "sns-img-bd.xhscdn.com",  # XHS-Downloader 默认使用
                "sns-img-qc.xhscdn.com",
                "sns-img-hw.xhscdn.com",
            ]
            for domain in cdn_domains:
                original_candidates.append({
                    "url": f"https://{domain}/{token}",
                    "desc": f"CDN-{domain.split('-')[2].split('.')[0]}-原始",
                    "format": None,  # 保持原始格式
                })
            
            # 2. imageView2 格式转换 - 作为备选（可能会压缩，但保证格式）
            for format_name in ["png", "jpeg"]:
                original_candidates.append({
                    "url": f"https://ci.xiaohongshu.com/{token}?imageView2/format/{format_name}",
                    "desc": f"CI-{format_name.upper()}",
                    "format": format_name,
                })
            
            for cand in original_candidates:
                cand_url = cand["url"]
                desc = cand["desc"]
                format_name = cand["format"]
                attempt_start = time_module.perf_counter()
                
                try:
                    # 使用非常长的超时时间（用户说带宽小）
                    timeout = aiohttp.ClientTimeout(total=600, connect=60)
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Referer": "https://www.xiaohongshu.com/",
                    }
                    
                    async with aiohttp.ClientSession(
                        headers=headers,
                        cookies=cookies if cookies else None,
                        timeout=timeout
                    ) as session:
                        async with session.get(cand_url) as resp:
                            attempt_elapsed = time_module.perf_counter() - attempt_start
                            
                            if resp.status == 200:
                                content = await resp.read()
                                content_len = len(content)
                                
                                # 验证文件大小（至少 10KB 才认为是有效图片）
                                if content_len >= 10 * 1024:
                                    # 确定输出文件后缀
                                    if format_name:
                                        actual_suffix = f".{format_name}"
                                    else:
                                        # 从响应头或文件签名推断格式
                                        actual_suffix = self._detect_image_suffix(content, resp.headers.get("Content-Type"))
                                    
                                    final_output = output_path.with_suffix(actual_suffix)
                                    temp_path = final_output.with_suffix(final_output.suffix + ".part")
                                    with open(temp_path, "wb") as f:
                                        f.write(content)
                                    if temp_path.exists():
                                        temp_path.replace(final_output)
                                    
                                    total_elapsed = time_module.perf_counter() - start_time
                                    logger.info(
                                        "XHS 原图下载成功 (%s): size=%.1fMB, 请求耗时=%.2fs, 总耗时=%.2fs",
                                        desc, content_len / 1024 / 1024, attempt_elapsed, total_elapsed
                                    )
                                    return final_output
                                else:
                                    logger.debug(
                                        "XHS 原图响应过小 (%s): size=%d bytes, 耗时=%.2fs",
                                        desc, content_len, attempt_elapsed
                                    )
                            else:
                                logger.debug(
                                    "XHS 原图请求失败 (%s): HTTP %d, 耗时=%.2fs",
                                    desc, resp.status, attempt_elapsed
                                )
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    attempt_elapsed = time_module.perf_counter() - attempt_start
                    logger.debug(
                        "XHS 原图下载异常 (%s): %s, 耗时=%.2fs",
                        desc, str(e)[:50], attempt_elapsed
                    )
            
            original_elapsed = time_module.perf_counter() - original_start
            logger.debug("XHS 原图下载全部失败，回退到普通策略，原图尝试耗时=%.2fs", original_elapsed)
        # endregion
        
        # region CDN 兜底策略
        fallback_start = time_module.perf_counter()
        
        # 基础 Headers
        base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        
        # 构建阶梯候选列表
        candidates = []
        
        # 1. 原始 URL (带签名)
        raw_url = url.replace("http://", "https://", 1) if url.startswith("http://") else url
        candidates.append({"url": raw_url, "use_cookies": False, "desc": "Raw-NoCookie"})
        candidates.append({"url": raw_url, "use_cookies": True, "desc": "Raw-WithCookie"})

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
                    candidates.append({"url": f"https://{domain}/{path}", "use_cookies": False, "desc": f"CDN-{domain.split('.')[0]}"})
        
        errors = []
        for cand in candidates:
            cand_url = cand["url"]
            use_cookies_flag = cand["use_cookies"]
            desc = cand["desc"]
            attempt_start = time_module.perf_counter()
            
            # 两种 header 变体
            header_variants = [
                {**base_headers, "Referer": "https://www.xiaohongshu.com/"},
                base_headers.copy(),
            ]
            
            for hv in header_variants:
                try:
                    # 超长超时
                    timeout = aiohttp.ClientTimeout(total=300, connect=30)
                    async with aiohttp.ClientSession(
                        headers=hv, 
                        cookies=cookies if use_cookies_flag else None,
                        timeout=timeout
                    ) as session:
                        async with session.get(cand_url) as resp:
                            if resp.status == 200:
                                content = await resp.read()
                                if len(content) >= 1024:
                                    temp_path = output_path.with_suffix(output_path.suffix + ".part")
                                    with open(temp_path, "wb") as f:
                                        f.write(content)
                                    if temp_path.exists():
                                        temp_path.replace(output_path)
                                    
                                    attempt_elapsed = time_module.perf_counter() - attempt_start
                                    total_elapsed = time_module.perf_counter() - start_time
                                    logger.info(
                                        "XHS CDN 图片下载成功 (%s): size=%.1fKB, 请求耗时=%.2fs, 总耗时=%.2fs",
                                        desc, len(content) / 1024, attempt_elapsed, total_elapsed
                                    )
                                    return output_path
                            
                            errors.append(f"{desc}: HTTP {resp.status}")
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    errors.append(f"{desc}: {str(e)[:20]}")
                    continue
        # endregion
        
        # 全部失败
        total_elapsed = time_module.perf_counter() - start_time
        error_summary = " | ".join(errors[:5])  # 只取前5个错误
        logger.error("XHS 图片下载全线失败: 总耗时=%.2fs, 错误=%s", total_elapsed, error_summary)
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
        if content[:8] == b'\x89PNG\r\n\x1a\n':
            return ".png"
        if content[:3] == b'\xff\xd8\xff':
            return ".jpeg"
        if content[:4] == b'RIFF' and content[8:12] == b'WEBP':
            return ".webp"
        if content[:4] == b'GIF8':
            return ".gif"
        if content[4:12] in (b'ftypavif', b'ftypavis'):
            return ".avif"
        if content[4:12] in (b'ftypheic', b'ftypmif1'):
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

    async def _render_xhs_card(
        self,
        result: XiaohongshuResult,
        image_paths: list[Path],
        cover_path: Path | None,
        is_video: bool,
    ) -> Path | None:
        try:
            card_path = self._build_xhs_card_path(result.source_url)
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
            logger.warning("小红书卡片渲染失败: %s", str(exc))
            return None
    # endregion

    # region 小红书处理
    async def _process_xhs(
        self, event: AstrMessageEvent, target_link: str, is_from_card: bool = False
    ):
        import time as time_module
        process_start = time_module.perf_counter()
        timing = {}  # 记录各步骤耗时
        
        self._refresh_config()
        if not self.xhs_enabled:
            return
        source_tag = "(来自卡片)" if is_from_card else ""
        
        # 尝试增加 aiocqhttp 超时时间（原图文件较大，需要更长时间上传）
        self._try_extend_aiocqhttp_timeout(event, getattr(self, 'api_timeout_sec', 600))

        await self._send_reaction_emoji(event, source_tag)

        target_link = (target_link or "").strip()
        
        # 初步去重
        if target_link and not self._check_and_record_url(target_link):
            return
            
        if not target_link:
            logger.info("⚠️ 小红书链接为空%s", source_tag)
            return
        logger.info("🍠 小红书解析%s: %s", source_tag, target_link)

        # region 解析阶段
        parse_start = time_module.perf_counter()
        try:
            result: XiaohongshuResult = await asyncio.wait_for(
                self.xhs_extractor.parse(target_link),
                timeout=30.0,  # 增加超时时间
            )
        except asyncio.CancelledError:
            logger.info("♻️ 小红书解析任务已中断%s", source_tag)
            return
        except asyncio.TimeoutError:
            logger.error("❌ 小红书解析超时%s", source_tag)
            # 不向用户发送错误信息
            return
        except XiaohongshuParseError as exc:
            logger.error("❌ 小红书解析失败%s: %s", source_tag, str(exc))
            return
        except Exception as exc:
            logger.error("❌ 小红书解析异常%s: %s", source_tag, str(exc))
            return
        timing["parse"] = time_module.perf_counter() - parse_start
        # endregion

        # 根据 note_id 去重
        if result.note_id and not self._check_and_record_url(f"xhs:{result.note_id}"):
            return

        logger.debug(
            "🍠 小红书解析完成%s: 视频=%s, 图片=%s, 解析耗时=%.2fs",
            source_tag,
            "有" if result.video_url else "无",
            len(result.image_urls),
            timing["parse"],
        )

        title = result.title or "未知标题"
        author = result.author or "未知作者"

        if not result.video_url and not result.image_urls:
            logger.warning("❌ 小红书未找到可下载的媒体%s: %s", source_tag, target_link)
            return

        media_components: list[object] = []
        media_paths: list[Path] = []
        image_paths: list[Path] = []
        cover_path: Path | None = None
        failed_images = 0

        # region 下载阶段
        download_start = time_module.perf_counter()
        
        # 视频笔记：优先下载视频
        if result.video_url:
            try:
                video_path = await self._download_xhs_video(
                    result.video_url, referer=result.source_url
                )
                media_paths.append(video_path)
                media_components.append(Video.fromFileSystem(str(video_path.resolve())))
                # 下载封面图
                cover_url = result.cover_url or (result.image_urls[0] if result.image_urls else None)
                if cover_url:
                    try:
                        cover_path = await self._download_xhs_image(
                            cover_url, referer=result.source_url
                        )
                        media_paths.append(cover_path)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning("小红书封面下载失败%s: %s", source_tag, str(exc))
            except asyncio.CancelledError:
                raise
            except SizeLimitExceeded:
                logger.warning("❌ 小红书视频大小超过限制%s (%dMB)", source_tag, self.max_video_size_mb)
                return
            except Exception as exc:
                logger.error("❌ 小红书视频下载失败%s: %s", source_tag, str(exc))
                return
        # 图片笔记：下载图片
        elif result.image_urls:
            image_urls = result.image_urls[: self.xhs_max_media]
            file_ids = result.file_ids[: self.xhs_max_media] if result.file_ids else []
            for i, url in enumerate(image_urls):
                try:
                    # 获取对应的 file_id（如果有）
                    file_id = file_ids[i] if i < len(file_ids) else None
                    image_path = await self._download_xhs_image(
                        url, file_id=file_id, referer=result.source_url
                    )
                    image_paths.append(image_path)
                    media_paths.append(image_path)
                    media_components.append(Image.fromFileSystem(str(image_path.resolve())))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failed_images += 1
                    logger.warning("小红书图片下载失败%s [%d/%d]: %s", source_tag, i + 1, len(image_urls), str(exc))
        
        timing["download"] = time_module.perf_counter() - download_start
        # endregion

        if not media_components:
            logger.debug(
                "XHS 无媒体下载成功%s: url=%s, 失败图片=%d, 下载耗时=%.2fs",
                source_tag, result.source_url, failed_images, timing["download"]
            )
            return

        # region 渲染阶段
        render_start = time_module.perf_counter()
        card_path = await self._render_xhs_card(
            result,
            image_paths=image_paths,
            cover_path=cover_path,
            is_video=bool(result.video_url and not image_paths),
        )
        if card_path:
            media_paths.append(card_path)
            media_components.insert(0, Image.fromFileSystem(str(card_path.resolve())))
        timing["render"] = time_module.perf_counter() - render_start
        # endregion

        # region 发送阶段
        send_start = time_module.perf_counter()
        
        # 计算总大小
        total_size_bytes = sum(p.stat().st_size for p in media_paths if p.exists())
        total_size_mb = total_size_bytes / (1024 * 1024)
        
        # 判断是否触发解合阈值
        threshold = getattr(self, 'xhs_auto_unmerge_threshold_mb', 20)
        force_unmerge = False
        if threshold > 0 and total_size_mb > threshold:
            logger.info("XHS 媒体总大小 (%.2fMB) 超过阈值 (%dMB)，强制逐条发送", total_size_mb, threshold)
            force_unmerge = True
        
        # 判断是否为图文笔记（有图片路径）
        is_image_post = bool(image_paths)
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
            self_id = str(event.get_self_id())
            for component in media_components:
                nodes.nodes.append(Node(uin=self_id, name="MyParser", content=[component]))
            yield event.chain_result([nodes])
        else:
            if is_image_post:
                # 图文笔记逐条发送（触发解合阈值时）
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

        timing["send"] = time_module.perf_counter() - send_start
        # endregion

        # 输出完整耗时日志
        total_elapsed = time_module.perf_counter() - process_start
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

        if media_paths:
            asyncio.create_task(self.cleanup_files(media_paths, []))
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
        self._register_parse_task("xhs", event)
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
