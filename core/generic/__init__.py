"""通用站点(yt-dlp 兜底)解析接口."""

from .extractor import (
    DEDICATED_DOMAINS,
    GENERIC_URL_PATTERN,
    GenericExtractor,
)

__all__ = [
    "DEDICATED_DOMAINS",
    "GENERIC_URL_PATTERN",
    "GenericExtractor",
]
