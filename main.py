# region 导入
import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register

from .core.bilibili import BILI_MESSAGE_PATTERN, BilibiliMixin
from .core.common import SizeLimitExceeded, get_bili_cookies_file
from .core.douyin import DOUYIN_MESSAGE_PATTERN, DouyinExtractor
from .core.douyin.handler import DouyinMixin
from .core.xiaohongshu import (
    XHS_MESSAGE_PATTERN,
    XiaohongshuCardRenderer,
    XiaohongshuExtractor,
    find_default_font,
)
from .core.xiaohongshu.handler import XiaohongshuMixin
# endregion

# region 运行时常量
TASK_NAME_PREFIX = "myparser-parse"
# endregion

# region MyParser 类
@register("astrbot_plugin_link_resolver", "acacia", "解析 & 下载 Bilibili/抖音/小红书", "1.0.8")
class MyParser(BilibiliMixin, DouyinMixin, XiaohongshuMixin, Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or context.get_config()
        self._migrate_legacy_config()
        # 注意：必须在 _active_parse_tasks 初始化之前调用；
        # 该方法通过 asyncio.all_tasks() 扫描清理旧任务，不依赖实例任务池。
        self._cancel_previous_parse_tasks()
        self._active_parse_tasks: set[asyncio.Task] = set()
        self.douyin_extractor = DouyinExtractor()
        self.xhs_extractor = XiaohongshuExtractor()
        self.xhs_renderer = XiaohongshuCardRenderer(find_default_font())
        self._refresh_config()

    # region 配置
    # 旧 flat key → (分组, 子key) 的映射，用于一次性迁移旧版配置
    _LEGACY_KEY_MAP: dict[str, tuple[str, str]] = {
        "bili_video_quality": ("bili_settings", "video_quality"),
        "bili_video_codecs": ("bili_settings", "video_codecs"),
        "bili_allow_hdr": ("bili_settings", "allow_hdr"),
        "bili_allow_dolby": ("bili_settings", "allow_dolby"),
        "bili_merge_send": ("bili_settings", "merge_send"),
        "bili_enable_multi_page": ("bili_settings", "enable_multi_page"),
        "bili_multi_page_max": ("bili_settings", "multi_page_max"),
        "bili_max_duration_seconds": ("bili_settings", "max_duration_seconds"),
        "bili_allow_quality_fallback": ("bili_settings", "allow_quality_fallback"),
        "bili_cookies": ("bili_settings", "cookies"),
        "douyin_max_media": ("douyin_settings", "max_media"),
        "douyin_merge_send": ("douyin_settings", "merge_send"),
        "xhs_max_media": ("xhs_settings", "max_media"),
        "xhs_merge_send": ("xhs_settings", "merge_send"),
        "xhs_download_original": ("xhs_settings", "download_original"),
        "xhs_prefer_ci_png": ("xhs_settings", "prefer_ci_png"),
        "xhs_auto_unmerge_threshold_mb": ("xhs_settings", "auto_unmerge_threshold_mb"),
        "xhs_concurrent_download": ("xhs_settings", "concurrent_download"),
        "retry_count": ("general_settings", "retry_count"),
        "reaction_emoji_enabled": ("general_settings", "reaction_emoji_enabled"),
        "reaction_emoji_id": ("general_settings", "reaction_emoji_id"),
        "max_video_size_mb": ("general_settings", "max_video_size_mb"),
        "merge_send_as_sender": ("general_settings", "merge_send_as_sender"),
        "error_notify_mode": ("general_settings", "error_notify_mode"),
    }

    def _migrate_legacy_config(self) -> None:
        """将旧版 flat 配置迁移到嵌套结构，保证面板显示与实际生效一致。"""
        if not isinstance(self.config, dict):
            return
        migrated = False
        for old_key, (group, sub_key) in self._LEGACY_KEY_MAP.items():
            if old_key not in self.config:
                continue
            if group not in self.config or not isinstance(self.config[group], dict):
                self.config[group] = {}
            # 仅在新位置尚无值时迁移
            if sub_key not in self.config[group]:
                self.config[group][sub_key] = self.config[old_key]
            del self.config[old_key]
            migrated = True
        if migrated and hasattr(self.config, "save_config"):
            self.config.save_config()
            logger.info("📦 已将旧版配置迁移到分组结构")

    def _get_config_value(self, key: str, default):
        keys = key.split(".")
        val = self.config
        for k in keys:
            if isinstance(val, dict):
                val = val.get(k)
            else:
                return default
            if val is None:
                return default
        return val

    def _refresh_config(self) -> None:
        # 平台启用列表
        enable_platforms = self._get_config_value("enable_platforms", ["B站", "抖音", "小红书"])
        if not isinstance(enable_platforms, list):
            enable_platforms = ["B站", "抖音", "小红书"]
        self.bili_enabled = "B站" in enable_platforms
        self.douyin_enabled = "抖音" in enable_platforms
        self.xhs_enabled = "小红书" in enable_platforms
        
        # B站配置
        self.quality_label = str(self._get_config_value("bili_settings.video_quality", "720P"))
        self.codecs_label = str(self._get_config_value("bili_settings.video_codecs", "AVC"))
        self.allow_hdr = bool(self._get_config_value("bili_settings.allow_hdr", False))
        self.allow_dolby = bool(self._get_config_value("bili_settings.allow_dolby", False))
        self.bili_merge_send = bool(self._get_config_value("bili_settings.merge_send", False))
        self.enable_multi_page = bool(self._get_config_value("bili_settings.enable_multi_page", True))
        self.multi_page_max = max(1, int(self._get_config_value("bili_settings.multi_page_max", 3)))
        self.bili_max_duration_seconds = max(
            0, int(self._get_config_value("bili_settings.max_duration_seconds", 300))
        )
        self.allow_quality_fallback = bool(self._get_config_value("bili_settings.allow_quality_fallback", True))
        # 从配置读取 Cookie 并写入文件
        bili_cookies_str = str(self._get_config_value("bili_settings.cookies", "")).strip()
        if bili_cookies_str:
            try:
                cookies_file = get_bili_cookies_file()
                cookies_file.parent.mkdir(parents=True, exist_ok=True)
                # 恢复 Netscape 格式的换行符（网页配置粘贴时可能丢失）
                if "\n" not in bili_cookies_str and ".bilibili.com" in bili_cookies_str:
                    bili_cookies_str = re.sub(
                        r"\s+(\.(?:www\.)?bilibili\.com\s)",
                        r"\n\1",
                        bili_cookies_str,
                    )
                    bili_cookies_str = bili_cookies_str.replace("# ", "\n# ").strip()
                cookies_file.write_text(bili_cookies_str, encoding="utf-8")
                logger.info("🍪 B站 Cookie 已从配置写入文件")
            except Exception as exc:
                logger.warning("⚠️ 写入 B站 Cookie 文件失败: %s", str(exc))
        
        # 抖音配置
        self.douyin_max_media = max(1, int(self._get_config_value("douyin_settings.max_media", 9)))
        self.douyin_merge_send = bool(self._get_config_value("douyin_settings.merge_send", True))
        
        # 小红书配置
        self.xhs_max_media = max(1, int(self._get_config_value("xhs_settings.max_media", 99)))
        self.xhs_merge_send = bool(self._get_config_value("xhs_settings.merge_send", True))
        self.xhs_download_original = bool(self._get_config_value("xhs_settings.download_original", True))
        self.xhs_prefer_ci_png = bool(self._get_config_value("xhs_settings.prefer_ci_png", False))
        self.xhs_auto_unmerge_threshold_mb = int(self._get_config_value("xhs_settings.auto_unmerge_threshold_mb", 50))
        self.xhs_concurrent_download = bool(self._get_config_value("xhs_settings.concurrent_download", True))
        
        # 通用配置
        self.retry_count = max(0, int(self._get_config_value("general_settings.retry_count", 3)))
        self.reaction_emoji_enabled = bool(self._get_config_value("general_settings.reaction_emoji_enabled", True))
        self.reaction_emoji_id = self._coerce_positive_int(self._get_config_value("general_settings.reaction_emoji_id", 128169), 128169)
        self.reaction_emoji_type = "1"  # 固定值，无需配置
        self.max_video_size_mb = int(self._get_config_value("general_settings.max_video_size_mb", 200))
        self.merge_send_as_sender = bool(self._get_config_value("general_settings.merge_send_as_sender", False))
        _mode = str(self._get_config_value("general_settings.error_notify_mode", "静默")).strip()
        self.error_notify_mode = _mode if _mode in ("静默", "脱敏", "报错") else "静默"

        alias = self._normalize_quality_alias(self.quality_label)
        if alias == "HDR":
            self.allow_hdr = True
        if alias == "DOLBY":
            self.allow_dolby = True

        self.quality_enum_name, self.video_quality = self._resolve_quality(alias)
        self.codecs_enum_name, self.video_codecs = self._resolve_codecs(self.codecs_label)

        # 构建启用平台列表
        enabled_list = [p for p in ["B站", "抖音", "小红书"] if p in enable_platforms]
        duration_label = (
            f"{self.bili_max_duration_seconds}s"
            if self.bili_max_duration_seconds > 0
            else "无限制"
        )
        logger.info(
            "📹 LinkResolver 配置: 平台=%s, B站(画质=%s,合并=%s,时长<=%s), 抖音(合并=%s), 小红书(原图=%s), 重试=%d",
            "/".join(enabled_list) if enabled_list else "无",
            self.video_quality.name,
            "开" if self.bili_merge_send else "关",
            duration_label,
            "开" if self.douyin_merge_send else "关",
            "开" if self.xhs_download_original else "关",
            self.retry_count,
        )
    # endregion

    # region 解析任务管理
    def _register_parse_task(self, kind: str, event: AstrMessageEvent | None = None) -> None:
        task = asyncio.current_task()
        if task is None:
            return
        message_id = None
        if event is not None:
            message_id = self._extract_reaction_message_id(event)
        tag = f"{kind}:{message_id or 'unknown'}"
        try:
            task.set_name(f"{TASK_NAME_PREFIX}:{tag}:{int(time.time() * 1000)}")
        except Exception:
            pass
        self._active_parse_tasks.add(task)
        task.add_done_callback(lambda t: self._active_parse_tasks.discard(t))

    def _cancel_previous_parse_tasks(self) -> None:
        """通过 asyncio.all_tasks() 按任务名/协程名扫描并取消旧解析任务。

        不依赖 self._active_parse_tasks（调用时该属性尚未初始化）。
        """
        cancelled: list[str] = []
        candidates: set[asyncio.Task] = set()

        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = None

        if loop:
            try:
                current_task = asyncio.current_task(loop=loop)
            except Exception:
                current_task = None
            try:
                tasks = asyncio.all_tasks(loop)
            except Exception:
                tasks = set()
            for task in tasks:
                if task is current_task:
                    continue
                name = task.get_name() if hasattr(task, "get_name") else ""
                if isinstance(name, str) and name.startswith(TASK_NAME_PREFIX):
                    candidates.add(task)
                    continue
                try:
                    qualname = getattr(task.get_coro(), "__qualname__", "")
                except Exception:
                    qualname = ""
                if any(
                    token in qualname
                    for token in (
                        "handle_xhs",
                        "handle_douyin",
                        "handle_bili_video",
                        "_process_xhs",
                        "_process_douyin",
                        "_process_bili_video",
                    )
                ):
                    candidates.add(task)

        for task in candidates:
            if task.done():
                continue
            try:
                task.cancel()
                name = task.get_name() if hasattr(task, "get_name") else ""
                if name:
                    cancelled.append(name)
            except Exception:
                continue

        if cancelled:
            sample = ", ".join(cancelled[:5])
            suffix = "..." if len(cancelled) > 5 else ""
            logger.info("♻️ 插件重载，已中断旧解析任务 %d 个: %s%s", len(cancelled), sample, suffix)
        else:
            logger.info("♻️ 插件重载，未发现可中断的旧解析任务")
    # endregion

    # region 通用工具
    def _has_json_component(self, event: AstrMessageEvent) -> bool:
        if not hasattr(event, "message_obj") or not hasattr(event.message_obj, "message"):
            return False
        for component in event.message_obj.message:
            if isinstance(component, dict):
                comp_type = component.get("type")
                if comp_type == "reply":
                    continue
                if comp_type and "json" in str(comp_type).lower():
                    return True
                continue
            if isinstance(component, Comp.Json):
                return True
            comp_type = getattr(component, "type", None)
            if comp_type and "json" in str(comp_type).lower():
                return True
        return False

    @staticmethod
    def _coerce_positive_int(value: object, default: int) -> int:
        if value is None:
            return default
        if isinstance(value, bool):
            return default
        try:
            if isinstance(value, (int, float)):
                parsed = int(value)
                return parsed if parsed > 0 else default
            text = str(value).strip()
            if text.isdigit():
                parsed = int(text)
                return parsed if parsed > 0 else default
        except Exception:
            return default
        return default

    @staticmethod
    def _format_duration(duration_seconds: int | None) -> str | None:
        if not duration_seconds:
            return None
        minutes = int(duration_seconds) // 60
        seconds = int(duration_seconds) % 60
        return f"{minutes}:{seconds:02d}"

    @staticmethod
    def _hash_url(url: str) -> str:
        return hashlib.md5(url.encode()).hexdigest()[:16]

    @staticmethod
    def _guess_media_suffix(url: str, default: str) -> str:
        try:
            suffix = Path(urlparse(url).path).suffix
        except Exception:
            suffix = ""
        if suffix and len(suffix) <= 5:
            return suffix
        return default

    # endregion

    # region 链接提取
    @staticmethod
    def _extract_urls_from_text(text: str) -> list[str]:
        if not text:
            return []
        return re.findall(r"https?://[^\s'\"<>]+", text)

    def _coerce_json_payload(self, json_component) -> dict | None:
        def unwrap(value, depth: int = 0) -> dict | None:
            if depth > 4 or value is None:
                return None
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    return None
                try:
                    return unwrap(json.loads(value), depth + 1)
                except Exception:
                    return None
            if isinstance(value, dict):
                if any(key in value for key in ("meta", "prompt", "ver", "app", "view", "config")):
                    return value
                if "data" in value:
                    return unwrap(value["data"], depth + 1)
                return value
            if isinstance(value, list):
                for item in value:
                    payload = unwrap(item, depth + 1)
                    if payload:
                        return payload
            return None

        if hasattr(json_component, "data"):
            return unwrap(json_component.data)
        return unwrap(json_component)

    def extract_links_from_json(self, json_component) -> list[str]:
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
                            found.extend(self._extract_urls_from_text(value))
                        elif isinstance(value, (dict, list)):
                            found.extend(search_json_for_links(value))
                elif isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, str):
                            found.extend(self._extract_urls_from_text(item))
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
                            links.extend(self._extract_urls_from_text(value))
        except Exception as exc:
            logger.warning("⚠️ 解析 JSON 消息组件失败: %s", str(exc))
        return links
    # endregion

    # region 消息基础判断
    @staticmethod
    def _is_self_message(event: AstrMessageEvent) -> bool:
        try:
            return str(event.get_sender_id()) == str(event.get_self_id())
        except Exception:
            return False

    async def _is_bot_muted(self, event: AstrMessageEvent) -> bool:
        """检测 Bot 是否在群中被禁言。
        
        通过 OneBot V11 的 get_group_member_info API 获取 Bot 在群中的信息，
        检查 shut_up_timestamp 字段判断是否被禁言。
        
        Returns:
            True 如果 Bot 被禁言，False 如果未被禁言或无法检测。
        """
        group_id = event.get_group_id()
        if not group_id:
            return False
        
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "call_action"):
            return False
        
        self_id = event.get_self_id()
        if not self_id:
            return False
        
        try:
            member_info = await bot.call_action(
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(self_id),
                no_cache=True,
            )
            shut_up_timestamp = member_info.get("shut_up_timestamp", 0)
            if shut_up_timestamp and shut_up_timestamp > time.time():
                logger.info("🔇 Bot 在群 %s 中被禁言，跳过处理", group_id)
                return True
        except Exception as exc:
            logger.debug("检测禁言状态失败: %s", str(exc))
        
        return False
    # endregion

    # region 表情回应
    def _extract_reaction_message_id(self, event: AstrMessageEvent) -> int | None:
        raw = getattr(event.message_obj, "raw_message", None)
        candidates: list[object] = []
        if isinstance(raw, dict):
            candidates.append(raw.get("message_id"))
        elif raw is not None and hasattr(raw, "message_id"):
            candidates.append(getattr(raw, "message_id", None))
        candidates.append(getattr(event.message_obj, "message_id", None))
        for value in candidates:
            if value is None:
                continue
            try:
                mid = int(value)
            except Exception:
                continue
            if mid > 0:
                return mid
        return None

    async def _send_reaction_emoji(self, event: AstrMessageEvent, source_tag: str) -> None:
        if not self.reaction_emoji_enabled:
            return
        if not event.get_group_id():
            logger.debug("表情回应跳过%s: 非群消息", source_tag)
            return
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "set_msg_emoji_like"):
            logger.debug("表情回应跳过%s: 平台不支持", source_tag)
            return
        message_id = self._extract_reaction_message_id(event)
        if message_id is None:
            logger.debug("表情回应跳过%s: 无法获取 message_id", source_tag)
            return
        try:
            await bot.set_msg_emoji_like(
                message_id=message_id,
                emoji_id=self.reaction_emoji_id,
                emoji_type=self.reaction_emoji_type,
                set=True,
            )
        except Exception as exc:
            logger.warning("⚠️ 表情回应失败%s: %s", source_tag, str(exc))
    # endregion

    # region 下载工具
    async def _probe_stream_size(
        self,
        url: str,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> int | None:
        try:
            headers = headers or {}
            cookies = cookies or {}
            async with httpx.AsyncClient(timeout=10.0, headers=headers, cookies=cookies) as client:
                response = await client.head(url, follow_redirects=True)
                if response.status_code >= 400:
                    return None
                length = response.headers.get("Content-Length")
                if length:
                    return int(length)
                range_headers = {**headers, "Range": "bytes=0-0"}
                response = await client.get(url, headers=range_headers)
                content_range = response.headers.get("Content-Range", "")
                if "/" in content_range:
                    return int(content_range.split("/")[-1])
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        return None

    async def _estimate_total_size_mb(
        self,
        video_url: str,
        audio_url: str | None,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> float | None:
        total = 0
        unknown = False
        for url in (video_url, audio_url):
            if not url:
                continue
            size = await self._probe_stream_size(url, cookies=cookies, headers=headers)
            if size is None:
                unknown = True
                continue
            total += size
        if total == 0 and unknown:
            return None
        return total / 1024 / 1024

    async def _download_stream(
        self,
        url: str,
        output_path: Path,
        cookies: dict[str, str] | None,
        max_bytes: int | None,
        headers: dict[str, str] | None = None,
        retries: int = 3,
    ) -> int:
        temp_path = output_path.with_suffix(output_path.suffix + ".part")
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                hdrs = headers or {}
                cks = cookies or {}
                async with httpx.AsyncClient(timeout=None, headers=hdrs, cookies=cks) as client:
                    async with client.stream("GET", url, follow_redirects=True) as response:
                        response.raise_for_status()
                        content_length = response.headers.get("Content-Length")
                        if content_length and max_bytes and int(content_length) > max_bytes:
                            raise SizeLimitExceeded("超过大小限制")
                        bytes_written = 0
                
                        # Actually, wrapping each write is fine if chunks are large
                        with open(temp_path, "wb") as file:
                            async for chunk in response.aiter_bytes(1024 * 1024):
                                if not chunk:
                                    continue
                                bytes_written += len(chunk)
                                if max_bytes and bytes_written > max_bytes:
                                    raise SizeLimitExceeded("超过大小限制")
                                await asyncio.to_thread(file.write, chunk)
                await asyncio.to_thread(temp_path.replace, output_path)
                return bytes_written
            except asyncio.CancelledError:
                if temp_path.exists():
                    await asyncio.to_thread(temp_path.unlink, missing_ok=True)
                raise
            except SizeLimitExceeded:
                if temp_path.exists():
                    await asyncio.to_thread(temp_path.unlink, missing_ok=True)
                raise
            except Exception as exc:
                last_error = exc
                if temp_path.exists():
                    await asyncio.to_thread(temp_path.unlink, missing_ok=True)
                if attempt < retries - 1:
                    wait_time = 2 ** attempt
                    logger.warning("⚠️ 下载失败, %d秒后重试 (%d/%d): %s", wait_time, attempt + 1, retries, str(exc))
                    await asyncio.sleep(wait_time)
        if last_error:
            raise last_error
        raise RuntimeError("下载失败")

    async def _merge_av(self, v_path: Path, a_path: Path, output_path: Path) -> None:
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(v_path),
            "-i",
            str(a_path),
            "-c",
            "copy",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            str(output_path),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _, stderr = await process.communicate()
            if process.returncode != 0:
                raise RuntimeError(stderr.decode().strip())
        finally:
            await asyncio.to_thread(v_path.unlink, missing_ok=True)
            await asyncio.to_thread(a_path.unlink, missing_ok=True)

    async def download_thumbnail(self, url: str, save_path: Path) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    await asyncio.to_thread(save_path.write_bytes, response.content)
                    return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("⚠️ 下载封面失败: %s", str(exc))
        return False

    async def calculate_md5(self, file_path: Path) -> str:
        def _sync_md5():
            hasher = hashlib.md5()
            with open(file_path, "rb") as file:
                while chunk := file.read(8192):
                    hasher.update(chunk)
            return hasher.hexdigest()
        return await asyncio.to_thread(_sync_md5)

    async def cleanup_files(self, video_paths: list[Path], thumbnail_paths: list[Path]) -> None:
        # Direct Send Pattern: 调用此方法时，文件已通过 await event.send() 被读取完毕
        # 无需延迟，立即清理以避免与后续相同 URL 请求产生竞态条件
        for video_path in video_paths:
            existed = await asyncio.to_thread(video_path.exists)
            await asyncio.to_thread(video_path.unlink, missing_ok=True)
            logger.debug("🧹 清理视频文件: path=%s, existed=%s", video_path, existed)
        for thumb_path in thumbnail_paths:
            existed = await asyncio.to_thread(thumb_path.exists)
            await asyncio.to_thread(thumb_path.unlink, missing_ok=True)
            logger.debug("🧹 清理缩略图文件: path=%s, existed=%s", thumb_path, existed)
    # endregion

    # region 合并转发发送人获取
    def _get_merge_sender_uin(self, event: AstrMessageEvent) -> str:
        """获取合并转发使用的 uin

        根据 merge_send_as_sender 配置决定使用发送者的 uin 还是 Bot 的 uin
        """
        if self.merge_send_as_sender:
            sender_id = event.get_sender_id()
            if sender_id:
                return str(sender_id)
        return str(event.get_self_id())
    # endregion




    # region 事件处理器
    @filter.regex(BILI_MESSAGE_PATTERN, priority=10)
    async def handle_bili_video(self, event: AstrMessageEvent):
        if self._has_json_component(event):
            return
        self._register_parse_task("bili", event)
        await BilibiliMixin.handle_bili_video(self, event)

    @filter.regex(DOUYIN_MESSAGE_PATTERN, priority=10)
    async def handle_douyin(self, event: AstrMessageEvent):
        if self._has_json_component(event):
            return
        self._register_parse_task("douyin", event)
        await DouyinMixin.handle_douyin(self, event)

    @filter.regex(XHS_MESSAGE_PATTERN, priority=10)
    async def handle_xhs(self, event: AstrMessageEvent):
        if self._has_json_component(event):
            return
        self._register_parse_task("xhs", event)
        await XiaohongshuMixin.handle_xhs(self, event)

    @filter.regex(r".*")
    async def handle_json_card(self, event: AstrMessageEvent):
        if self._is_self_message(event):
            return

        links: list[str] = []
        has_json_component = False
        if hasattr(event, "message_obj") and hasattr(event.message_obj, "message"):
            for component in event.message_obj.message:
                is_json_component = False
                comp_payload = component
                if isinstance(component, dict):
                    comp_type = component.get("type")
                    if comp_type == "reply": # 忽略引用回复组件，防止回复时递归解析原消息
                        continue
                    comp_payload = component.get("data") or component
                    is_json_component = bool(comp_type) and "json" in str(comp_type).lower()
                else:
                    if isinstance(component, Comp.Json):
                        is_json_component = True
                    comp_type = getattr(component, "type", None)
                    if not is_json_component and comp_type:
                        is_json_component = "json" in str(comp_type).lower()
                    if is_json_component and hasattr(component, "data"):
                        comp_payload = component.data
                if is_json_component:
                    has_json_component = True
                    logger.info("🔗 检测到 JSON 卡片消息: %s", component)
                    links.extend(self.extract_links_from_json(comp_payload))
        if not has_json_component:
            return

        if await self._is_bot_muted(event):
            return

        if not links:
            return
        unique_links = list(dict.fromkeys(links))
        bili_links = [link for link in unique_links if re.search(BILI_MESSAGE_PATTERN, link)]
        douyin_links = [link for link in unique_links if re.search(DOUYIN_MESSAGE_PATTERN, link)]
        xhs_links = [link for link in unique_links if re.search(XHS_MESSAGE_PATTERN, link)]

        if bili_links and self.bili_enabled:
            self._register_parse_task("json-bili", event)
            event.should_call_llm(True)
            try:
                ref = await self._resolve_video_ref_from_links(bili_links)
                if ref:
                    await self._process_bili_video(event, ref=ref, is_from_card=True)
                    return
                logger.warning("⚠️ 从卡片中找到 B 站链接但无法解析: %s", bili_links)
            except asyncio.CancelledError:
                logger.info("♻️ JSON卡片解析任务已中断")
                return

        if douyin_links and self.douyin_enabled:
            self._register_parse_task("json-douyin", event)
            event.should_call_llm(True)
            try:
                await self._process_douyin(event, douyin_links[0], is_from_card=True)
                return
            except asyncio.CancelledError:
                logger.info("♻️ JSON卡片解析任务已中断")
                return

        if xhs_links and self.xhs_enabled:
            self._register_parse_task("json-xhs", event)
            event.should_call_llm(True)
            try:
                await self._process_xhs(event, xhs_links[0], is_from_card=True)
                return
            except asyncio.CancelledError:
                logger.info("♻️ JSON卡片解析任务已中断")
                return

        logger.warning("⚠️ 从卡片中找到链接但无法解析: %s", unique_links)
    # endregion
# endregion
