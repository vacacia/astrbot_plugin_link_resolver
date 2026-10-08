"""YouTube 链接识别和媒体下载接口."""

from .extractor import (
    YOUTUBE_MESSAGE_PATTERN,
    YoutubeDownloadError,
    YoutubeExtractor,
    YoutubeParseError,
    YoutubeResult,
    extract_youtube_links,
    is_youtube_403_error,
)

__all__ = [
    "YOUTUBE_MESSAGE_PATTERN",
    "YoutubeDownloadError",
    "YoutubeExtractor",
    "YoutubeParseError",
    "YoutubeResult",
    "extract_youtube_links",
    "is_youtube_403_error",
]
