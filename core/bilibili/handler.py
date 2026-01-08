# region 导入
import asyncio
import json
import re
import shutil
from dataclasses import dataclass
from http import cookiejar
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image, Node, Nodes, Plain, Video
from bilibili_api import Credential, video
from bilibili_api.video import (
    AudioStreamDownloadURL,
    VideoCodecs,
    VideoDownloadURLDataDetecter,
    VideoQuality,
    VideoStreamDownloadURL,
)

from ..common import DOWNLOAD_HEADERS, BILIBILI_VIDEO_PATH, BILIBILI_THUMB_PATH, BILIBILI_CARD_PATH, BILI_COOKIES_FILE, SizeLimitExceeded
from ..common.card_renderer import UniversalCardRenderer, CardData, get_theme_for_platform
# endregion

# region 常量与正则
BILI_VIDEO_URL_PATTERN = (
    r"(https?://)?(?:(?:www|m)\.)?bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)"
)
BILI_SHORT_LINK_PATTERN = r"https?://(?:b23\.tv|bili2233\.cn)/[A-Za-z\d._?%&+\-=/#]+"
BILI_BV_PATTERN = r"\bBV[0-9A-Za-z]{10}\b"
BILI_AV_PATTERN = r"\bav\d+\b"
BILI_MESSAGE_PATTERN = (
    rf"(?s).*(?:{BILI_VIDEO_URL_PATTERN}|{BILI_SHORT_LINK_PATTERN}|{BILI_BV_PATTERN}|{BILI_AV_PATTERN})"
)

QUALITY_ALIAS_MAP = {
    "原画": "ORIGINAL",
    "原画(最高画质)": "ORIGINAL",
    "最高": "ORIGINAL",
    "ORIGINAL": "ORIGINAL",
    "最低": "LOWEST",
    "最低画质": "LOWEST",
    "LOWEST": "LOWEST",
    "杜比视界": "DOLBY",
    "杜比": "DOLBY",
    "DOLBY": "DOLBY",
    "HDR": "HDR",
    "8K": "8K",
    "4K": "4K",
    "1080P60": "1080P60",
    "1080P高帧率": "1080P60",
    "1080P+": "1080PPLUS",
    "1080PPLUS": "1080PPLUS",
    "1080P高码率": "1080PPLUS",
    "1080P": "1080P",
    "720P60": "720P60",
    "720P高帧率": "720P60",
    "720P": "720P",
    "480P": "480P",
    "360P": "360P",
    "240P": "240P",
}

CODECS_ALIAS_MAP = {
    "AVC": "AVC",
    "H264": "AVC",
    "H.264": "AVC",
    "HEVC": "HEVC",
    "H265": "HEVC",
    "H.265": "HEVC",
    "AV1": "AV1",
}
# endregion

# region 路径常量（使用 common 导出的路径）
BILI_VIDEO_PATH = BILIBILI_VIDEO_PATH
BILI_THUMBNAIL_PATH = BILIBILI_THUMB_PATH
BILI_QQ_THUMB_PATH = ""
# endregion

# region 数据类
@dataclass
class VideoRef:
    bvid: str | None
    avid: int | None
    page_index: int
    source_url: str | None


@dataclass
class CookieStatus:
    is_login: bool
    is_vip: bool | None
    vip_type: int | None
    message: str


# endregion


# region B站混入
class BilibiliMixin:
    # region 画质与编码
    @staticmethod
    def _normalize_quality_alias(label: str) -> str:
        cleaned = label.strip()
        cleaned = cleaned.replace(" ", "")
        return QUALITY_ALIAS_MAP.get(cleaned, cleaned.upper())

    def _resolve_quality(self, alias: str) -> tuple[str, VideoQuality]:
        if alias == "ORIGINAL":
            return self._max_allowed_quality()
        if alias == "LOWEST":
            return self._min_allowed_quality()

        candidates = self._quality_name_candidates(alias)
        for name in candidates:
            if hasattr(VideoQuality, name):
                return name, getattr(VideoQuality, name)

        if alias in VideoQuality.__members__:
            return alias, VideoQuality[alias]

        fallback = "_720P"
        return fallback, getattr(VideoQuality, fallback)

    def _max_allowed_quality(self) -> tuple[str, VideoQuality]:
        candidates: list[VideoQuality] = []
        for quality in VideoQuality:
            name = quality.name.upper()
            if not self.allow_hdr and "HDR" in name:
                continue
            if not self.allow_dolby and "DOLBY" in name:
                continue
            candidates.append(quality)
        if not candidates:
            fallback = "_720P"
            return fallback, getattr(VideoQuality, fallback)
        best = max(candidates, key=lambda item: item.value)
        return best.name, best

    def _min_allowed_quality(self) -> tuple[str, VideoQuality]:
        candidates: list[VideoQuality] = []
        for quality in VideoQuality:
            name = quality.name.upper()
            if not self.allow_hdr and "HDR" in name:
                continue
            if not self.allow_dolby and "DOLBY" in name:
                continue
            candidates.append(quality)
        if not candidates:
            fallback = "_720P"
            return fallback, getattr(VideoQuality, fallback)
        lowest = min(candidates, key=lambda item: item.value)
        return lowest.name, lowest

    def _get_lower_qualities(self, current_quality: VideoQuality) -> list[VideoQuality]:
        candidates: list[VideoQuality] = []
        for quality in VideoQuality:
            name = quality.name.upper()
            if not self.allow_hdr and "HDR" in name:
                continue
            if not self.allow_dolby and "DOLBY" in name:
                continue
            if quality.value < current_quality.value:
                candidates.append(quality)
        return sorted(candidates, key=lambda q: q.value, reverse=True)

    @staticmethod
    def _quality_name_candidates(alias: str) -> list[str]:
        alias = alias.upper()
        candidates = [alias, f"_{alias}"]
        if "PLUS" in alias:
            candidates.append(alias.replace("PLUS", "_PLUS"))
            candidates.append(f"_{alias.replace('PLUS', '_PLUS')}")
        match = re.search(r"(\d+P)(\d+)", alias)
        if match:
            composite = f"{match.group(1)}_{match.group(2)}"
            candidates.extend([composite, f"_{composite}"])
        unique: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        return unique

    @staticmethod
    def _resolve_codecs(label: str) -> tuple[str, VideoCodecs]:
        normalized = label.strip().upper().replace(" ", "")
        normalized = CODECS_ALIAS_MAP.get(normalized, normalized)
        if hasattr(VideoCodecs, normalized):
            return normalized, getattr(VideoCodecs, normalized)
        return "AVC", VideoCodecs.AVC

    @staticmethod
    def _normalize_bvid(bvid: str) -> str | None:
        if not bvid:
            return None
        bvid = bvid.strip()
        if len(bvid) != 12:
            return None
        if bvid.startswith("BV"):
            candidate = bvid
        elif bvid[:2].lower() == "bv":
            candidate = "BV" + bvid[2:]
        else:
            return None
        return candidate if re.fullmatch(r"BV[0-9A-Za-z]{10}", candidate) else None
    # endregion

    # region 链接与解析
    @staticmethod
    def _parse_page_index(text: str) -> int:
        try:
            parsed = urlparse(text)
            page = parse_qs(parsed.query).get("p", ["1"])[0]
            return max(int(page) - 1, 0)
        except Exception:
            return 0

    @staticmethod
    def _extract_bvid(text: str) -> str | None:
        match = re.search(BILI_BV_PATTERN, text)
        return match.group(0) if match else None

    @staticmethod
    def _extract_avid(text: str) -> int | None:
        match = re.search(BILI_AV_PATTERN, text, re.IGNORECASE)
        return int(match.group(0)[2:]) if match else None

    def extract_links_from_text(self, text: str, include_ids: bool = True) -> list[str]:
        links: list[str] = []
        url_patterns = [
            r"https?://(?:www\.)?bilibili\.com/video/[^\s\'\"<>]+",
            r"https?://m\.bilibili\.com/video/[^\s\'\"<>]+",
            r"https?://(?:b23\.tv|bili2233\.cn)/[^\s\'\"<>]+",
        ]
        for pattern in url_patterns:
            links.extend(re.findall(pattern, text, re.IGNORECASE))
        if include_ids:
            links.extend(re.findall(BILI_BV_PATTERN, text))
            links.extend(re.findall(BILI_AV_PATTERN, text, re.IGNORECASE))
        return links

    def _parse_video_ref_from_text(
        self, text: str, source_url: str | None = None
    ) -> VideoRef | None:
        if bvid := self._extract_bvid(text):
            bvid = self._normalize_bvid(bvid)
            if bvid:
                return VideoRef(
                    bvid=bvid,
                    avid=None,
                    page_index=self._parse_page_index(text),
                    source_url=source_url or text,
                )
        if avid := self._extract_avid(text):
            return VideoRef(
                bvid=None,
                avid=avid,
                page_index=self._parse_page_index(text),
                source_url=source_url or text,
            )
        return None

    async def resolve_short_url(self, short_url: str) -> str | None:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
                try:
                    response = await client.head(short_url)
                except Exception:
                    response = await client.get(short_url)
            final_url = str(response.url)
            logger.info("🔗 短链接重定向: %s -> %s", short_url, final_url)
            return final_url
        except Exception as exc:
            logger.error("❌ 解析短链接失败: %s", str(exc))
        return None

    async def _resolve_video_ref_from_text(self, text: str) -> VideoRef | None:
        links = self.extract_links_from_text(text, include_ids=True)
        if not links:
            return None
        for token in links:
            if re.match(BILI_SHORT_LINK_PATTERN, token, re.IGNORECASE):
                final_url = await self.resolve_short_url(token)
                if final_url:
                    if ref := self._parse_video_ref_from_text(final_url, source_url=token):
                        return ref
                continue
            if ref := self._parse_video_ref_from_text(token):
                return ref
        return None

    async def _resolve_video_ref_from_links(self, links: list[str]) -> VideoRef | None:
        for link in links:
            if ref := await self._resolve_video_ref_from_text(link):
                return ref
        return None
    # endregion

    # region JSON 卡片提取
    def extract_bilibili_links_from_json(self, json_component) -> list[str]:
        links: list[str] = []
        try:
            json_data = self._coerce_json_payload(json_component)
            if not json_data:
                return links

            def search_json_for_links(obj):
                found: list[str] = []
                if isinstance(obj, dict):
                    for value in obj.values():
                        if isinstance(value, str):
                            found.extend(self.extract_links_from_text(value, include_ids=False))
                        elif isinstance(value, (dict, list)):
                            found.extend(search_json_for_links(value))
                elif isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, str):
                            found.extend(self.extract_links_from_text(item, include_ids=False))
                        elif isinstance(item, (dict, list)):
                            found.extend(search_json_for_links(item))
                return found

            links.extend(search_json_for_links(json_data))

            if isinstance(json_data, dict):
                meta = json_data.get("meta", {})
                detail = meta.get("detail_1", {}) if meta else {}
                if detail:
                    for key in ("qqdocurl", "url"):
                        value = detail.get(key, "")
                        if value:
                            links.extend(self.extract_links_from_text(value, include_ids=False))

            logger.info("从 JSON 组件中提取到链接: %s", links)
        except Exception as exc:
            logger.warning("解析 JSON 消息组件失败: %s", str(exc))
        return links
    # endregion

    # region Cookie凭证
    def _load_cookies(self) -> dict[str, str]:
        if not BILI_COOKIES_FILE.exists():
            return {}
        try:
            raw = BILI_COOKIES_FILE.read_text(encoding="utf-8").strip()
            if not raw:
                return {}
            if raw.lstrip().startswith("{"):
                data = json.loads(raw)
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
        except Exception:
            pass

        try:
            jar = cookiejar.MozillaCookieJar()
            jar.load(BILI_COOKIES_FILE, ignore_discard=True, ignore_expires=True)
            return {cookie.name: cookie.value for cookie in jar}
        except Exception as exc:
            logger.warning("🍪 读取 cookies 失败: %s", str(exc))
            return {}

    def _build_credential(self, cookies: dict[str, str]) -> Credential:
        if not cookies:
            return Credential(sessdata=None)
        try:
            return Credential.from_cookies(cookies)
        except Exception as exc:
            logger.warning("🍪 读取 cookies 失败，使用简化凭证: %s", str(exc))
            return Credential(sessdata=cookies.get("SESSDATA"))

    async def _check_cookie_status(self, cookies: dict[str, str]) -> CookieStatus:
        if not cookies:
            return CookieStatus(is_login=False, is_vip=None, vip_type=None, message="cookies 为空")
        try:
            async with httpx.AsyncClient(
                timeout=10.0,
                headers=DOWNLOAD_HEADERS,
                cookies=cookies,
                follow_redirects=True,
            ) as client:
                response = await client.get("https://api.bilibili.com/x/web-interface/nav")
            if response.status_code != 200:
                return CookieStatus(False, None, None, f"HTTP {response.status_code}")
            data = response.json()
            if data.get("code") != 0:
                return CookieStatus(False, None, None, f"code={data.get('code')}")
            info = data.get("data") or {}
            is_login = bool(info.get("isLogin"))
            vip = info.get("vip") or {}
            vip_status = vip.get("status") if isinstance(vip, dict) else None
            vip_type = vip.get("vipType") if isinstance(vip, dict) else None
            is_vip = vip_status == 1 if vip_status is not None else None
            message = "ok" if is_login else "not login"
            return CookieStatus(is_login, is_vip, vip_type, message)
        except Exception as exc:
            return CookieStatus(False, None, None, f"error: {exc}")
    # endregion

    # region B站视频处理
    async def _select_streams(
        self,
        video_obj: video.Video,
        page_index: int,
        video_quality: VideoQuality | None = None,
    ) -> tuple:
        """选择视频流和音频流。可指定画质，否则使用配置的默认画质。
        
        Returns:
            tuple: (video_stream, audio_stream, estimated_size_mb)
                - estimated_size_mb: 从 API 的 bandwidth 和 timelength 计算的预估大小 (MB)，如果无法计算则为 None
        """
        target_quality = video_quality or self.video_quality
        download_url_data = await video_obj.get_download_url(page_index=page_index)
        detecter = VideoDownloadURLDataDetecter(download_url_data)
        streams = detecter.detect_best_streams(
            video_max_quality=target_quality,
            codecs=[self.video_codecs],
            no_dolby_video=not self.allow_dolby,
            no_hdr=not self.allow_hdr,
        )
        if not streams:
            raise RuntimeError("未找到可下载的视频流")
        video_stream = streams[0]
        if not isinstance(video_stream, VideoStreamDownloadURL):
            raise RuntimeError("未找到可下载的视频流")

        audio_stream = None
        if len(streams) > 1 and isinstance(streams[1], AudioStreamDownloadURL):
            audio_stream = streams[1]

        # 从原始 API 数据计算预估文件大小
        estimated_size_mb = self._estimate_size_from_api_data(
            download_url_data, video_stream, audio_stream
        )

        logger.info(
            "🎞️ 实际选用画质: %s, 编码: %s, 预估大小: %s MB",
            video_stream.video_quality.name,
            video_stream.video_codecs,
            f"{estimated_size_mb:.2f}" if estimated_size_mb else "未知",
        )
        return video_stream, audio_stream, estimated_size_mb

    def _estimate_size_from_api_data(
        self,
        download_url_data: dict,
        video_stream: VideoStreamDownloadURL,
        audio_stream: AudioStreamDownloadURL | None,
    ) -> float | None:
        """从 API 返回的 bandwidth 和 timelength 字段计算预估文件大小。
        
        公式: size_bytes = bandwidth * (timelength / 1000) / 8
        """
        try:
            dash = download_url_data.get("dash")
            if not dash:
                return None
            
            timelength_ms = download_url_data.get("timelength")  # 毫秒
            if not timelength_ms:
                return None
            timelength_sec = timelength_ms / 1000
            
            total_bandwidth = 0
            
            # 查找匹配的视频流 bandwidth
            video_url = video_stream.url
            for v in dash.get("video", []):
                v_url = v.get("baseUrl") or v.get("base_url", "")
                if v_url == video_url:
                    total_bandwidth += v.get("bandwidth", 0)
                    break
            
            # 查找匹配的音频流 bandwidth
            if audio_stream:
                audio_url = audio_stream.url
                for a in dash.get("audio", []):
                    a_url = a.get("baseUrl") or a.get("base_url", "")
                    if a_url == audio_url:
                        total_bandwidth += a.get("bandwidth", 0)
                        break
            
            if total_bandwidth == 0:
                return None
            
            # bandwidth 单位是 bps (bits per second)
            size_bytes = total_bandwidth * timelength_sec / 8
            size_mb = size_bytes / 1024 / 1024
            return size_mb
        except Exception as exc:
            logger.debug("从 API 数据计算文件大小失败: %s", str(exc))
            return None

    async def _download_video(
        self,
        video_obj: video.Video,
        bvid: str,
        page_index: int,
        page_count: int,
        cookies: dict[str, str],
    ) -> tuple[Path, str]:
        """下载视频。如果超过大小限制且开启了自动降画质，会尝试更低画质。"""
        current_quality = self.video_quality
        max_bytes = self.max_video_size_mb * 1024 * 1024 if self.max_video_size_mb > 0 else None

        while True:
            video_stream, audio_stream, size_mb = await self._select_streams(
                video_obj, page_index, video_quality=current_quality
            )
            video_url = video_stream.url
            audio_url = audio_stream.url if audio_stream else None
            actual_quality = video_stream.video_quality

            # size_mb 现在从 API 的 bandwidth 和 timelength 计算，比 HTTP HEAD 更可靠
            if size_mb is None and max_bytes is not None:
                logger.warning(
                    "⚠️ 无法从 API 获取视频大小估算，降画质功能可能不生效"
                )

            size_exceeds = (
                size_mb is not None
                and max_bytes is not None
                and size_mb > self.max_video_size_mb
            )

            if size_exceeds:
                if self.allow_quality_fallback:
                    lower_qualities = self._get_lower_qualities(actual_quality)
                    if lower_qualities:
                        next_quality = lower_qualities[0]
                        logger.info(
                            "⚠️ 画质 %s 超限 (%.2fMB > %dMB)，尝试降至 %s",
                            actual_quality.name,
                            size_mb,
                            self.max_video_size_mb,
                            next_quality.name,
                        )
                        current_quality = next_quality
                        continue
                # 无法降级或禁用降级
                raise SizeLimitExceeded("超过大小限制")

            # 大小合适，开始下载
            break

        suffix = f"_p{page_index + 1}" if page_count > 1 else ""
        output_path = BILI_VIDEO_PATH / f"{bvid}{suffix}.mp4"

        if audio_url:
            temp_video = output_path.with_suffix(".video")
            temp_audio = output_path.with_suffix(".audio")
            await self._download_stream(video_url, temp_video, cookies, max_bytes)
            await self._download_stream(audio_url, temp_audio, cookies, max_bytes)
            await self._merge_av(temp_video, temp_audio, output_path)
        else:
            await self._download_stream(video_url, output_path, cookies, max_bytes)

        return output_path, actual_quality.name

    async def _get_video_info(self, video_obj: video.Video, source_tag: str = "") -> dict:
        """获取视频信息，带重试机制"""
        retry_count = getattr(self, 'retry_count', 3)
        last_error: Exception | None = None
        
        for attempt in range(retry_count + 1):
            try:
                return await video_obj.get_info()
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError as exc:
                last_error = exc
                if attempt < retry_count:
                    wait_time = 2 ** attempt  # 指数退避: 1s, 2s, 4s...
                    logger.warning(
                        "⚠️ B站视频信息获取超时%s, %d秒后重试 (%d/%d)",
                        source_tag, wait_time, attempt + 1, retry_count
                    )
                    await asyncio.sleep(wait_time)
                else:
                    logger.error("❌ B站视频信息获取超时%s (已重试%d次)", source_tag, retry_count)
            except Exception as exc:
                last_error = exc
                # 检查是否为 curl 超时错误
                error_str = str(exc).lower()
                is_timeout = "timeout" in error_str or "curl: (28)" in error_str
                
                if is_timeout and attempt < retry_count:
                    wait_time = 2 ** attempt
                    logger.warning(
                        "⚠️ B站视频信息获取失败%s: %s, %d秒后重试 (%d/%d)",
                        source_tag, str(exc), wait_time, attempt + 1, retry_count
                    )
                    await asyncio.sleep(wait_time)
                elif attempt < retry_count and self._is_retryable_error(exc):
                    wait_time = 2 ** attempt
                    logger.warning(
                        "⚠️ B站视频信息获取失败%s: %s, %d秒后重试 (%d/%d)",
                        source_tag, str(exc), wait_time, attempt + 1, retry_count
                    )
                    await asyncio.sleep(wait_time)
                else:
                    logger.error("❌ B站视频信息获取失败%s: %s (已重试%d次)", source_tag, str(exc), retry_count)
                    break
        
        if last_error:
            raise last_error
        raise RuntimeError("获取视频信息失败")
    
    @staticmethod
    def _is_retryable_error(exc: Exception) -> bool:
        """判断是否为可重试的网络错误"""
        error_str = str(exc).lower()
        retryable_patterns = [
            "timeout", "timed out", "connection", "reset", "refused",
            "curl:", "network", "temporary", "unavailable", "503", "502"
        ]
        return any(pattern in error_str for pattern in retryable_patterns)

    async def _download_bili_cover(self, cover_url: str, bvid: str) -> Path | None:
        """下载封面图到缓存目录"""
        if not cover_url:
            return None
        try:
            cover_path = BILIBILI_CARD_PATH / f"{bvid}_cover.jpg"
            await self._download_stream(cover_url, cover_path, cookies=None, max_bytes=None)
            return cover_path
        except Exception as exc:
            logger.warning("❌ 下载B站封面失败: %s", str(exc))
            return None

    def _format_count(self, count: int) -> str:
        """格式化数字为易读形式"""
        if count >= 100000000:
            return f"{count / 100000000:.1f}亿"
        if count >= 10000:
            return f"{count / 10000:.1f}万"
        return str(count)

    async def _render_bili_card(
        self,
        *,
        title: str,
        author: str,
        cover_url: str,
        bvid: str,
        views: int,
        likes: int,
        coins: int,
    ) -> Path | None:
        """渲染B站视频卡片"""
        try:
            cover_path = await self._download_bili_cover(cover_url, bvid)

            theme = get_theme_for_platform("bilibili")
            renderer = UniversalCardRenderer(theme)

            data = CardData(
                title=title,
                author=author,
                text=None,
                image_paths=[],
                cover_path=cover_path,
                is_video=True,
                stats={
                    "👁": self._format_count(views),
                    "👍": self._format_count(likes),
                    "🪙": self._format_count(coins),
                },
            )

            card_img = renderer.render(data)
            card_path = BILIBILI_CARD_PATH / f"{bvid}_card.png"
            card_img.save(card_path)

            logger.info("✅ B站卡片渲染成功: %s", card_path)
            return card_path
        except Exception as exc:
            logger.warning("❌ B站卡片渲染失败: %s", str(exc))
            return None
    # endregion

    # region B站主处理
    async def _process_bili_video(
        self, event: AstrMessageEvent, ref: VideoRef, is_from_card: bool = False
    ):
        import time as time_module
        process_start = time_module.perf_counter()
        timing = {}  # 记录各步骤耗时
        
        self._refresh_config()
        if not self.bili_enabled:
            return
            
        dedup_key = ref.bvid or str(ref.avid or "")
        if dedup_key and not self._check_and_record_url(f"bili:{dedup_key}"):
            return
            
        source_tag = "(来自卡片)" if is_from_card else ""
        await self._send_reaction_emoji(event, source_tag)

        cookies = self._load_cookies()
        credential = self._build_credential(cookies)
        cookie_status = await self._check_cookie_status(cookies)
        logger.debug(
            "🍪 Cookie检测%s: 登录=%s, 会员=%s, vipType=%s, 状态=%s",
            source_tag,
            cookie_status.is_login,
            cookie_status.is_vip,
            cookie_status.vip_type,
            cookie_status.message,
        )

        if ref.bvid:
            video_obj = video.Video(bvid=ref.bvid, credential=credential)
        elif ref.avid:
            video_obj = video.Video(aid=ref.avid, credential=credential)
        else:
            return

        try:
            # region 解析阶段
            parse_start = time_module.perf_counter()
            try:
                info = await self._get_video_info(video_obj, source_tag)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 错误日志已在 _get_video_info 中输出
                return
            timing["parse"] = time_module.perf_counter() - parse_start
            # endregion

            stat = info.get("stat", {})
            bvid = info.get("bvid") or ref.bvid
            if not bvid:
                logger.warning("❌ 无法获取 bvid%s", source_tag)
                return

            title = info.get("title", "未知标题")
            up_name = info.get("owner", {}).get("name", "未知UP主")
            duration_seconds = info.get("duration", 0)
            view_count = stat.get("view", 0)
            likes = stat.get("like", 0)
            coins = stat.get("coin", 0)
            shares = stat.get("share", 0)
            comments = stat.get("reply", 0)
            cover_url = info.get("pic", "")

            logger.debug(
                "� B站解析完成%s: bvid=%s, 标题=%s, 解析耗时=%.2fs",
                source_tag,
                bvid,
                title[:30],
                timing["parse"],
            )

            pages = info.get("pages") or []
            page_count = len(pages) if pages else 1
            page_index = min(ref.page_index, max(page_count - 1, 0))
            has_page_param = bool(ref.source_url and re.search(r"[?&]p=\\d+", ref.source_url))
            is_multi_page = page_count > 1 and self.enable_multi_page and not has_page_param
            page_indexes = [page_index]
            if is_multi_page:
                page_indexes = list(range(min(self.multi_page_max, page_count)))

            video_paths: list[Path] = []
            thumbnail_paths: list[Path] = []

            # region 下载阶段
            download_start = time_module.perf_counter()
            
            if is_multi_page:
                nodes = Nodes([])
                self_id = str(event.get_self_id())
                header_text = (
                    f"🎬 标题: {title}\n"
                    f"👤 UP主: {up_name}\n"
                    f"📄 分P数量: {page_count}\n"
                    f"🔢 播放量: {view_count}\n"
                    f"❤️ 点赞: {likes}\n"
                    f"🏆 投币: {coins}\n"
                    f"🔄 分享: {shares}\n"
                    f"💬 评论: {comments}\n"
                    f"🎚️ 画质设置: {self.quality_label}"
                )
                nodes.nodes.append(Node(uin=self_id, name="BiliBot", content=[Plain(header_text)]))

                for idx in page_indexes:
                    page_info = pages[idx] if idx < len(pages) else {}
                    page_title = page_info.get("part") or title
                    page_duration = page_info.get("duration") or duration_seconds
                    try:
                        page_start = time_module.perf_counter()
                        video_path, actual_quality = await self._download_video(
                            video_obj,
                            bvid,
                            idx,
                            page_count,
                            cookies,
                        )
                        video_paths.append(video_path)
                        page_elapsed = time_module.perf_counter() - page_start
                        logger.debug(
                            "� B站分P下载成功%s [%d/%d]: size=%.2fMB, 画质=%s, 耗时=%.2fs",
                            source_tag, idx + 1, len(page_indexes),
                            video_path.stat().st_size / 1024 / 1024,
                            actual_quality,
                            page_elapsed
                        )
                        page_text = (
                            f"📄 分P {idx + 1}/{page_count}: {page_title}\n"
                            f"⏱️ 时长: {page_duration // 60}:{page_duration % 60:02d}\n"
                            f"🎞️ 实际画质: {actual_quality}"
                        )
                        nodes.nodes.append(Node(uin=self_id, name="BiliBot", content=[Plain(page_text)]))
                        abs_video_path = str(video_path.resolve())
                        nodes.nodes.append(
                            Node(uin=self_id, name="BiliBot", content=[Video.fromFileSystem(abs_video_path)])
                        )
                    except asyncio.CancelledError:
                        raise
                    except SizeLimitExceeded:
                        warn_text = (
                            f"❌ 分P {idx + 1} 超过大小限制 ({self.max_video_size_mb}MB)，跳过下载"
                        )
                        nodes.nodes.append(Node(uin=self_id, name="BiliBot", content=[Plain(warn_text)]))
                    except Exception as exc:
                        logger.error("❌ 视频下载失败%s: %s", source_tag, str(exc))
                        error_text = f"❌ 分P {idx + 1} 下载失败: {str(exc)}"
                        nodes.nodes.append(Node(uin=self_id, name="BiliBot", content=[Plain(error_text)]))

                timing["download"] = time_module.perf_counter() - download_start
                
                # region 发送阶段
                send_start = time_module.perf_counter()
                yield event.chain_result([nodes])
                timing["send"] = time_module.perf_counter() - send_start
                # endregion

                if BILI_QQ_THUMB_PATH and cover_url and video_paths:
                    for path in video_paths:
                        video_md5 = self.calculate_md5(path)
                        thumbnail_save_path = BILI_THUMBNAIL_PATH / f"{video_md5}.png"
                        qq_thumb_path = Path(BILI_QQ_THUMB_PATH) / f"{video_md5}_0.png"
                        if await self.download_thumbnail(cover_url, thumbnail_save_path):
                            shutil.copy(thumbnail_save_path, qq_thumb_path)
                            thumbnail_paths.append(thumbnail_save_path)

                # 输出完整耗时日志
                total_elapsed = time_module.perf_counter() - process_start
                logger.info(
                    "🎬 B站处理完成%s: 标题=%s, 分P=%d | 耗时: 解析=%.2fs, 下载=%.2fs, 发送=%.2fs, 总计=%.2fs",
                    source_tag,
                    title[:20],
                    len(video_paths),
                    timing.get("parse", 0),
                    timing.get("download", 0),
                    timing.get("send", 0),
                    total_elapsed,
                )

                if video_paths or thumbnail_paths:
                    asyncio.create_task(self.cleanup_files(video_paths, thumbnail_paths))
                return

            # 单P视频处理
            try:
                video_path, actual_quality = await self._download_video(
                    video_obj, bvid, page_index, page_count, cookies
                )
                video_paths.append(video_path)
                logger.debug(
                    "📥 B站视频下载成功%s: size=%.2fMB, 画质=%s, 耗时=%.2fs",
                    source_tag,
                    video_path.stat().st_size / 1024 / 1024,
                    actual_quality,
                    time_module.perf_counter() - download_start
                )
            except asyncio.CancelledError:
                raise
            except SizeLimitExceeded:
                video_info_text = (
                    f"🎬 标题: {title}\n"
                    f"👤 UP主: {up_name}\n"
                    f"🔢 播放量: {view_count}\n"
                    f"❤️ 点赞: {likes}\n"
                    f"🏆 投币: {coins}\n"
                    f"🔄 分享: {shares}\n"
                    f"💬 评论: {comments}\n\n"
                    f"❌ 视频大小超过限制 ({self.max_video_size_mb}MB)，无法下载\n"
                    f"💡 当前画质设置: {self.quality_label}"
                )
                yield event.plain_result(video_info_text)
                return
            except Exception as exc:
                logger.error("❌ 视频下载失败%s: %s", source_tag, str(exc))
                yield event.plain_result(f"❌ 视频下载失败: {str(exc)}")
                return

            timing["download"] = time_module.perf_counter() - download_start
            # endregion

            # region 渲染阶段
            render_start = time_module.perf_counter()
            card_path = None
            if self.bili_merge_send:
                card_path = await self._render_bili_card(
                    title=title,
                    author=up_name,
                    cover_url=cover_url,
                    bvid=bvid,
                    views=view_count,
                    likes=likes,
                    coins=coins,
                )
            timing["render"] = time_module.perf_counter() - render_start
            # endregion

            # region 发送阶段
            send_start = time_module.perf_counter()
            
            try:
                abs_video_path = str(video_path.resolve())
                video_component = Video.fromFileSystem(abs_video_path)
                
                if self.bili_merge_send:
                    nodes = Nodes([])
                    self_id = str(event.get_self_id())
                    
                    if card_path and card_path.exists():
                        card_component = Image.fromFileSystem(str(card_path.resolve()))
                        nodes.nodes.append(
                            Node(uin=self_id, name="BiliBot", content=[card_component])
                        )

                    nodes.nodes.append(
                        Node(uin=self_id, name="BiliBot", content=[video_component])
                    )
                    logger.debug("🚀 B站合并消息准备发送%s: 节点数=%d", source_tag, len(nodes.nodes))
                    yield event.chain_result([nodes])
                else:
                    # 非合并转发：只发视频
                    logger.debug("� B站普通消息准备发送%s", source_tag)
                    yield event.chain_result([video_component])

                timing["send"] = time_module.perf_counter() - send_start
                # endregion

                if BILI_QQ_THUMB_PATH and cover_url:
                    video_md5 = self.calculate_md5(video_path)
                    thumbnail_save_path = BILI_THUMBNAIL_PATH / f"{video_md5}.png"
                    qq_thumb_path = Path(BILI_QQ_THUMB_PATH) / f"{video_md5}_0.png"
                    if await self.download_thumbnail(cover_url, thumbnail_save_path):
                        shutil.copy(thumbnail_save_path, qq_thumb_path)
                        thumbnail_paths.append(thumbnail_save_path)

                # 输出完整耗时日志
                total_elapsed = time_module.perf_counter() - process_start
                logger.info(
                    "🎬 B站处理完成%s: 标题=%s, 画质=%s | 耗时: 解析=%.2fs, 下载=%.2fs, 渲染=%.2fs, 发送=%.2fs, 总计=%.2fs",
                    source_tag,
                    title[:20],
                    actual_quality,
                    timing.get("parse", 0),
                    timing.get("download", 0),
                    timing.get("render", 0),
                    timing.get("send", 0),
                    total_elapsed,
                )

                if video_paths or thumbnail_paths:
                    asyncio.create_task(self.cleanup_files(video_paths, thumbnail_paths))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("❌ 视频发送失败%s: %s", source_tag, str(exc))
                yield event.plain_result(f"❌ 视频发送失败: {str(exc)}")
                if video_paths or thumbnail_paths:
                    asyncio.create_task(self.cleanup_files(video_paths, thumbnail_paths))
        except asyncio.CancelledError:
            logger.info("♻️ B站解析任务已中断%s", source_tag)
            return
    # endregion

    # region 事件处理器
    # 事件过滤器由 main.py 注册，确保绑定插件实例。
    async def handle_bili_video(self, event: AstrMessageEvent):
        if not self.bili_enabled:
            return
        if self._is_self_message(event):
            return
        if await self._is_bot_muted(event):
            return
        self._register_parse_task("bili", event)
        event.should_call_llm(True)
        try:
            ref = await self._resolve_video_ref_from_text(event.message_str)
            if not ref:
                return
            async for result in self._process_bili_video(event, ref=ref, is_from_card=False):
                yield result
        except asyncio.CancelledError:
            logger.info("♻️ B站解析任务已中断")
            return
    # endregion


# endregion
