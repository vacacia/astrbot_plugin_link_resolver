"""X/Twitter 内容提取器。

流程：
1. 从文本中识别 twitter.com / x.com 状态链接并按 tweet id 去重。
2. 调用 fxtwitter API 获取推文正文、作者、发布时间和媒体信息。
3. 将返回结构映射为插件内部统一结果，保留图片和视频直链列表。
4. 媒体字段是可选的：纯文字推文同样返回结果，图片和视频列表为空，由 handler 决定如何发送。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import httpx

TWITTER_REQUEST_TIMEOUT_SEC = 20.0
TWITTER_API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://x.com/",
}
TWITTER_DOWNLOAD_HEADERS = {
    **TWITTER_API_HEADERS,
    "Accept": "*/*",
}

TWITTER_STATUS_PATTERN = (
    r"(?:https?://)?(?:www\.)?(?:twitter\.com|x\.com)/"
    r"(?:(?:[A-Za-z0-9_]{1,32}|i/web)/status|statuses)/(?P<tid>\d+)"
)
TWITTER_MESSAGE_PATTERN = rf"(?s).*(?:{TWITTER_STATUS_PATTERN})"

_STATUS_RE = re.compile(TWITTER_STATUS_PATTERN, re.IGNORECASE)


@dataclass(slots=True)
class TwitterResult:
    text: str | None
    author: str | None
    created_at: str | None
    image_urls: list[str]
    video_urls: list[str]
    source_url: str
    tweet_id: str | None = None

    @property
    def has_media(self) -> bool:
        return bool(self.image_urls or self.video_urls)


class TwitterParseError(RuntimeError):
    pass


class TwitterRetryableError(TwitterParseError):
    pass


def _normalize_url(url: str) -> str:
    raw = (url or "").strip().rstrip(")],.!?;")
    if not raw:
        return raw
    if not raw.startswith(("http://", "https://")):
        raw = f"https://{raw}"

    match = _STATUS_RE.search(raw)
    if not match:
        return raw

    parsed = urlparse(raw)
    host = parsed.netloc or "x.com"
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{host}{path}"


def extract_twitter_links(text: str) -> list[str]:
    links: list[str] = []
    seen_ids: set[str] = set()
    if not text:
        return links

    for match in _STATUS_RE.finditer(text):
        tweet_id = match.group("tid")
        if tweet_id in seen_ids:
            continue
        seen_ids.add(tweet_id)
        links.append(_normalize_url(match.group(0)))
    return links


class TwitterExtractor:
    def __init__(self, timeout: float = TWITTER_REQUEST_TIMEOUT_SEC):
        self.timeout = timeout

    async def parse(self, text_or_url: str) -> TwitterResult:
        url = _normalize_url(text_or_url)
        tweet_id = self._extract_tweet_id(url)
        payload = await self._fetch_status_json(tweet_id)
        return self._build_result(payload, url)

    def _extract_tweet_id(self, url: str) -> str:
        if match := _STATUS_RE.search(url):
            return match.group("tid")
        raise TwitterParseError(f"无法从 X 链接提取 tweet id: {url}")

    async def _fetch_status_json(self, tweet_id: str) -> dict[str, Any]:
        api_url = f"https://api.fxtwitter.com/status/{tweet_id}"
        last_error: Exception | None = None

        for attempt in range(3):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    headers=TWITTER_API_HEADERS,
                    follow_redirects=True,
                ) as client:
                    response = await client.get(api_url)
                    if response.status_code >= 500:
                        raise TwitterRetryableError(
                            f"X 详情接口临时失败: {response.status_code}"
                        )
                    if response.status_code >= 400:
                        raise TwitterParseError(
                            f"X 详情接口失败: {response.status_code}"
                        )
                    payload = response.json()
            except TwitterRetryableError as exc:
                last_error = exc
            except asyncio.TimeoutError as exc:
                last_error = exc
            except httpx.HTTPError as exc:
                last_error = exc
            except TwitterParseError:
                raise
            else:
                if isinstance(payload, dict):
                    return payload
                raise TwitterParseError("X 详情接口返回格式异常")

            if attempt < 2:
                await asyncio.sleep(1.0 * (attempt + 1))

        raise TwitterRetryableError(f"X 详情接口请求失败: {last_error or '未知错误'}")

    def _build_result(self, payload: dict[str, Any], source_url: str) -> TwitterResult:
        tweet = payload.get("tweet")
        if not isinstance(tweet, dict):
            tweet = payload.get("status")
        if not isinstance(tweet, dict):
            raise TwitterParseError("X 详情接口未返回推文数据")

        text = str(tweet.get("text") or "").strip() or None
        author = self._format_author(tweet.get("author"))
        created_at = self._format_created_at(tweet.get("created_at"))
        media = tweet.get("media") if isinstance(tweet.get("media"), dict) else {}

        image_urls: list[str] = []
        seen_images: set[str] = set()
        for photo in media.get("photos", []) or []:
            if not isinstance(photo, dict):
                continue
            url = str(photo.get("url") or "").strip()
            if url and url not in seen_images:
                seen_images.add(url)
                image_urls.append(url)

        video_urls: list[str] = []
        seen_videos: set[str] = set()
        for video in media.get("videos", []) or []:
            if not isinstance(video, dict):
                continue
            url = str(video.get("url") or "").strip()
            if url and url not in seen_videos:
                seen_videos.add(url)
                video_urls.append(url)

        for item in media.get("all", []) or []:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").strip().lower()
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            if item_type == "photo" and url not in seen_images:
                seen_images.add(url)
                image_urls.append(url)
            elif item_type in {"video", "gif"} and url not in seen_videos:
                seen_videos.add(url)
                video_urls.append(url)

        external_media = media.get("external")
        if isinstance(external_media, dict):
            url = str(external_media.get("url") or "").strip()
            media_type = str(external_media.get("type") or "").strip().lower()
            if url and media_type in {"video", "gif"} and url not in seen_videos:
                seen_videos.add(url)
                video_urls.append(url)

        article = tweet.get("article")
        if isinstance(article, dict):
            article_media = [article.get("cover_media")]
            article_media.extend(article.get("media_entities") or [])
            for item in article_media:
                if not isinstance(item, dict):
                    continue
                media_info = item.get("media_info")
                if not isinstance(media_info, dict):
                    continue
                url = str(media_info.get("original_img_url") or "").strip()
                if url and url not in seen_images:
                    seen_images.add(url)
                    image_urls.append(url)

        return TwitterResult(
            text=text,
            author=author,
            created_at=created_at,
            image_urls=image_urls,
            video_urls=video_urls,
            source_url=_normalize_url(source_url),
            tweet_id=self._extract_tweet_id(source_url),
        )

    @staticmethod
    def _format_author(author_info: Any) -> str | None:
        if not isinstance(author_info, dict):
            return None
        name = str(author_info.get("name") or "").strip()
        screen_name = str(author_info.get("screen_name") or "").strip()
        if name and screen_name:
            return f"{name}(@{screen_name})"
        if name:
            return name
        if screen_name:
            return screen_name
        return None

    @staticmethod
    def _format_created_at(value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            dt = datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y")
        except ValueError:
            return text
        return dt.strftime("%Y-%m-%d")
