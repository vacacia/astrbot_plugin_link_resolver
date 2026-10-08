"""通过 yt-dlp 获取 YouTube 视频信息并下载媒体."""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

YOUTUBE_REQUEST_TIMEOUT_SEC = 60.0
YOUTUBE_URL_PATTERN = r"(?:https?://)?(?:www\.|m\.)?(?:youtube\.com|youtu\.be)(?=$|[/?#])(?:/[^\s'\"<>]*)?"
YOUTUBE_MESSAGE_PATTERN = rf"(?s).*(?:{YOUTUBE_URL_PATTERN})"
_YOUTUBE_RE = re.compile(YOUTUBE_URL_PATTERN, re.IGNORECASE)
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


class YoutubeParseError(RuntimeError):
    """输入链接不属于支持的 YouTube 域名."""


class YoutubeDownloadError(RuntimeError):
    """yt-dlp 无法获取视频信息或下载媒体."""


def is_youtube_403_error(exc: BaseException | str) -> bool:
    """识别 YouTube 请求的 HTTP 403 拒绝."""

    text = str(exc).lower()
    return "http error 403" in text or "403: forbidden" in text


@dataclass(slots=True)
class YoutubeResult:
    video_id: str
    title: str | None
    uploader: str | None
    duration: int | None
    source_url: str
    thumbnail_url: str | None = None
    filesize: int | None = None


def _normalize_url(raw: str) -> str:
    url = (raw or "").strip().rstrip(")],.!?;，。！？")
    if not url:
        return url
    return url if url.startswith(("http://", "https://")) else f"https://{url}"


def _is_youtube_url(url: str) -> bool:
    try:
        return (urlparse(url).hostname or "").lower() in _YOUTUBE_HOSTS
    except ValueError:
        return False


def extract_youtube_links(text: str) -> list[str]:
    """按消息顺序返回去重后的 YouTube 链接."""
    links: list[str] = []
    for match in _YOUTUBE_RE.finditer(text or ""):
        url = _normalize_url(match.group(0))
        if _is_youtube_url(url) and url not in links:
            links.append(url)
    return links


def _get_yt_dlp_class():
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise YoutubeDownloadError(
            "缺少 yt-dlp 依赖，请重载插件以安装 requirements.txt"
        ) from exc
    return YoutubeDL


class YoutubeExtractor:
    def __init__(self, timeout: float = YOUTUBE_REQUEST_TIMEOUT_SEC):
        self.timeout = timeout

    @staticmethod
    def _validate_url(url: str) -> str:
        normalized = _normalize_url(url)
        if not _is_youtube_url(normalized):
            raise YoutubeParseError(f"不是支持的 YouTube 链接: {url}")
        return normalized

    @staticmethod
    def _integer(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _result_from_info(self, info: dict[str, Any], source_url: str) -> YoutubeResult:
        video_id = str(info.get("id") or "").strip()
        if not video_id:
            raise YoutubeParseError("yt-dlp 未返回视频 ID")
        return YoutubeResult(
            video_id=video_id,
            title=str(info.get("title") or "").strip() or None,
            uploader=str(info.get("uploader") or info.get("channel") or "").strip()
            or None,
            duration=self._integer(info.get("duration")),
            source_url=str(info.get("webpage_url") or source_url),
            thumbnail_url=str(info.get("thumbnail") or "").strip() or None,
            filesize=self._integer(info.get("filesize") or info.get("filesize_approx")),
        )

    def _base_options(
        self,
        cookie_file: str | None,
        *,
        player_client: str | None = None,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": self.timeout,
        }
        if cookie_file:
            path = Path(cookie_file).expanduser()
            if not path.is_file():
                raise YoutubeDownloadError(f"YouTube Cookie 文件不存在: {path}")
            options["cookiefile"] = str(path)

        youtube_args: dict[str, list[str]] = {}
        if player_client and player_client != "default":
            youtube_args["player_client"] = [player_client]
        if youtube_args:
            options["extractor_args"] = {"youtube": youtube_args}
        return options

    async def inspect(
        self,
        url: str,
        cookie_file: str | None = None,
        *,
        player_client: str | None = None,
    ) -> YoutubeResult:
        return await asyncio.to_thread(
            self._inspect_sync,
            self._validate_url(url),
            cookie_file,
            player_client,
        )

    def _inspect_sync(
        self,
        url: str,
        cookie_file: str | None,
        player_client: str | None,
    ) -> YoutubeResult:
        try:
            options = {
                **self._base_options(
                    cookie_file,
                    player_client=player_client,
                ),
                "skip_download": True,
            }
            with _get_yt_dlp_class()(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except (YoutubeParseError, YoutubeDownloadError):
            raise
        except Exception as exc:
            raise YoutubeDownloadError(f"YouTube 信息获取失败: {exc}") from exc
        if not isinstance(info, dict):
            raise YoutubeParseError("yt-dlp 返回的信息格式异常")
        return self._result_from_info(info, url)

    async def download(
        self,
        url: str,
        output_dir: Path,
        request_id: str,
        *,
        max_height: int = 720,
        video_codec: str = "h264",
        max_bytes: int | None = None,
        cookie_file: str | None = None,
        player_client: str | None = None,
    ) -> tuple[YoutubeResult, Path]:
        cancelled = threading.Event()
        worker = asyncio.create_task(
            asyncio.to_thread(
                self._download_sync,
                self._validate_url(url),
                output_dir,
                request_id,
                max_height,
                video_codec,
                max_bytes,
                cookie_file,
                player_client,
                cancelled,
            )
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled.set()
            try:
                _, path = await asyncio.shield(worker)
            except Exception:
                pass
            else:
                path.unlink(missing_ok=True)
            raise

    def _download_sync(
        self,
        url: str,
        output_dir: Path,
        request_id: str,
        max_height: int,
        video_codec: str,
        max_bytes: int | None,
        cookie_file: str | None,
        player_client: str | None,
        cancelled: threading.Event,
    ) -> tuple[YoutubeResult, Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f"youtube_{request_id}_", dir=output_dir
        ) as directory:
            return self._download_to_directory(
                url,
                Path(directory),
                output_dir,
                request_id,
                max_height,
                video_codec,
                max_bytes,
                cookie_file,
                player_client,
                cancelled,
            )

    def _download_to_directory(
        self,
        url: str,
        temporary_dir: Path,
        output_dir: Path,
        request_id: str,
        max_height: int,
        video_codec: str,
        max_bytes: int | None,
        cookie_file: str | None,
        player_client: str | None,
        cancelled: threading.Event,
    ) -> tuple[YoutubeResult, Path]:
        def check_cancelled(_progress=None):
            if cancelled.is_set():
                raise YoutubeDownloadError("YouTube 下载已取消")

        check_cancelled()
        options = {
            **self._base_options(cookie_file, player_client=player_client),
            "format": self._build_format_selector(
                max_height=max_height,
                video_codec=video_codec,
                ffmpeg_available=bool(shutil.which("ffmpeg")),
            ),
            "outtmpl": str(temporary_dir / f"%(id)s_{request_id}.%(ext)s"),
            "max_filesize": max_bytes,
            "overwrites": False,
            "progress_hooks": [check_cancelled],
        }
        try:
            with _get_yt_dlp_class()(options) as ydl:
                info = ydl.extract_info(url, download=True)
                check_cancelled()
                if not isinstance(info, dict):
                    raise YoutubeParseError("yt-dlp 返回的信息格式异常")
                result = self._result_from_info(info, url)
                candidates = [Path(ydl.prepare_filename(info))]
                candidates.extend(
                    Path(str(item["filepath"]))
                    for item in info.get("requested_downloads") or []
                    if isinstance(item, dict) and item.get("filepath")
                )
        except (YoutubeParseError, YoutubeDownloadError):
            raise
        except Exception as exc:
            raise YoutubeDownloadError(f"YouTube 下载失败: {exc}") from exc
        output_path = next(
            (path for path in candidates if path.is_file() and path.stat().st_size > 0),
            None,
        )
        if output_path is None:
            output_path = next(
                (
                    path
                    for path in temporary_dir.glob(f"{result.video_id}_{request_id}.*")
                    if path.is_file()
                    and not path.name.endswith((".part", ".ytdl"))
                    and path.stat().st_size > 0
                ),
                None,
            )
        if output_path is None:
            raise YoutubeDownloadError("yt-dlp 未生成可发送的视频文件")
        if max_bytes is not None and output_path.stat().st_size > max_bytes:
            raise YoutubeDownloadError("下载后的视频超过大小限制")
        check_cancelled()
        destination = output_dir / output_path.name
        output_path.replace(destination)
        return result, destination

    @staticmethod
    def _build_format_selector(
        *, max_height: int, video_codec: str, ffmpeg_available: bool
    ) -> str:
        """优先匹配指定编码, 所有回退格式都保留音轨."""

        height = f"[height<={max_height}]" if max_height > 0 else ""
        codec_filter = "[vcodec^=av01]" if video_codec == "av1" else "[vcodec^=avc1]"
        preferred_video = f"bv*{codec_filter}[ext=mp4]{height}"
        fallback_video = f"bv*[ext=mp4]{height}"
        if ffmpeg_available:
            return (
                f"{preferred_video}+ba[ext=m4a]/b{codec_filter}[ext=mp4]{height}"
                f"/{fallback_video}+ba[ext=m4a]/b[ext=mp4]{height}/best{height}"
            )
        return f"b{codec_filter}[ext=mp4]{height}/b[ext=mp4]{height}/best{height}"
