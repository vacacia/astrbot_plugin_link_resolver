# region 导入
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import httpx
import msgspec
from astrbot.api import logger

from .errors import DouyinParseError
from .guest_api import DouyinGuestAPI
from .render import DouyinCardRenderer
from .slides import SlidesInfo
from .video import RouterData

# endregion

# region 常量
IOS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 "
        "Mobile/15E148 Safari/604.1 Edg/132.0.0.0"
    )
}

ANDROID_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 15; SM-G998B) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/132.0.0.0 Mobile Safari/537.36 "
        "Edg/132.0.0.0"
    )
}
# endregion

# region 链接正则
DOUYIN_SHORT_LINK_PATTERN = r"(?:https?://)?(?:v|jx)\.douyin\.com/[a-zA-Z0-9_\-]+/?"

_DOUYIN_LONG_PATTERNS = [
    r"(?:https?://)?(?:www\.)?douyin\.com/(?P<ty>video|note)/(?P<vid>\d+)",
    r"(?:https?://)?(?:www\.)?iesdouyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)",
    r"(?:https?://)?m\.douyin\.com/share/(?P<ty>slides|video|note)/(?P<vid>\d+)",
    r"(?:https?://)?jingxuan\.douyin\.com/m/(?P<ty>slides|video|note)/(?P<vid>\d+)",
]

_DOUYIN_LONG_DETECT_PATTERNS = [
    r"(?:https?://)?(?:www\.)?douyin\.com/(?:video|note)/\d+",
    r"(?:https?://)?(?:www\.)?iesdouyin\.com/share/(?:slides|video|note)/\d+",
    r"(?:https?://)?m\.douyin\.com/share/(?:slides|video|note)/\d+",
    r"(?:https?://)?jingxuan\.douyin\.com/m/(?:slides|video|note)/\d+",
]

DOUYIN_MESSAGE_PATTERN = (
    rf"(?s).*(?:{DOUYIN_SHORT_LINK_PATTERN}|{'|'.join(_DOUYIN_LONG_DETECT_PATTERNS)})"
)

_LONG_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _DOUYIN_LONG_PATTERNS]
_SHORT_RE = re.compile(DOUYIN_SHORT_LINK_PATTERN, re.IGNORECASE)
# endregion


# region 数据类
@dataclass(slots=True)
class DouyinResult:
    title: str | None
    author: str | None
    author_avatar: str | None
    duration: int | None
    video_url: str | None
    cover_url: str | None
    image_urls: list[str]
    dynamic_urls: list[str]
    source_url: str
    likes: int | None = None
    item_id: str | None = None
    comments: int | None = None
    video_urls: list[str] = field(default_factory=list)
    image_url_candidates: list[list[str]] = field(default_factory=list)
    dynamic_url_candidates: list[list[str]] = field(default_factory=list)


# endregion


# region 链接工具
def extract_douyin_links(text: str) -> list[str]:
    links: list[str] = []
    if not text:
        return links
    for pattern in [DOUYIN_SHORT_LINK_PATTERN, *_DOUYIN_LONG_PATTERNS]:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            links.append(_normalize_url(match.group(0)))
    return links


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        return url
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"https://{url}"


# endregion


# region 解析器
class DouyinExtractor:
    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self.guest_api = DouyinGuestAPI(timeout=timeout)

    async def resolve_short_url(self, url: str) -> str:
        url = _normalize_url(url)
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=self.timeout, headers=IOS_HEADERS
        ) as client:
            try:
                response = await client.head(url)
                if response.status_code >= 400:
                    response = await client.get(url)
            except Exception:
                response = await client.get(url)
        return str(response.url)

    async def parse(self, text_or_url: str) -> DouyinResult | None:
        url = _normalize_url(text_or_url)
        if _SHORT_RE.search(url):
            url = await self.resolve_short_url(url)

        ty, vid = self._match_type_and_id(url)
        if not vid:
            raise DouyinParseError("no valid douyin id found")

        errors: list[str] = []
        if not ty:
            return await self._parse_unknown_type(vid, url)
        if ty == "slides":
            try:
                return await self.parse_slides(vid, url)
            except DouyinParseError as exc:
                errors.append(f"slides:{exc}")
            try:
                return await self.parse_iteminfo(vid, url)
            except DouyinParseError as exc:
                errors.append(f"iteminfo:{exc}")
            raise DouyinParseError("; ".join(errors) or "failed to parse douyin slides")

        try:
            return await self.parse_iteminfo(vid, url)
        except DouyinParseError as exc:
            errors.append(f"iteminfo:{exc}")

        if "iesdouyin.com" in url or "m.douyin.com/share" in url:
            try:
                return await self.parse_video(url, url)
            except DouyinParseError as exc:
                errors.append(f"share:{exc}")

        for share_url in (
            self._build_m_douyin_url(ty, vid),
            self._build_iesdouyin_url(ty, vid),
        ):
            try:
                return await self.parse_video(share_url, url)
            except DouyinParseError as exc:
                errors.append(f"share:{exc}")

        raise DouyinParseError("; ".join(errors) or "failed to parse douyin link")

    def _match_type_and_id(self, url: str) -> tuple[str | None, str | None]:
        for pattern in _LONG_PATTERNS:
            if match := pattern.search(url):
                return match.group("ty"), match.group("vid")
        return None, self._extract_id_from_query(url)

    @staticmethod
    def _extract_id_from_query(url: str) -> str | None:
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
        except Exception:
            return None
        for key in ("modal_id", "aweme_id", "item_id", "video_id", "note_id", "id"):
            values = params.get(key) or []
            for value in values:
                if value and value.isdigit():
                    return value
        return None

    async def _parse_unknown_type(self, vid: str, source_url: str) -> DouyinResult:
        errors: list[str] = []
        try:
            return await self.parse_iteminfo(vid, source_url)
        except DouyinParseError as exc:
            errors.append(f"iteminfo:{exc}")

        for ty_guess in ("video", "note"):
            for share_url in (
                self._build_m_douyin_url(ty_guess, vid),
                self._build_iesdouyin_url(ty_guess, vid),
            ):
                try:
                    return await self.parse_video(share_url, source_url)
                except DouyinParseError as exc:
                    errors.append(f"{ty_guess}:{exc}")

        try:
            return await self.parse_slides(vid, source_url)
        except DouyinParseError as exc:
            errors.append(f"slides:{exc}")
        raise DouyinParseError("; ".join(errors) or "failed to parse douyin link")

    @staticmethod
    def _build_iesdouyin_url(ty: str, vid: str) -> str:
        return f"https://www.iesdouyin.com/share/{ty}/{vid}"

    @staticmethod
    def _build_m_douyin_url(ty: str, vid: str) -> str:
        return f"https://m.douyin.com/share/{ty}/{vid}"

    # region parse video
    async def parse_video(self, url: str, source_url: str) -> DouyinResult:
        pattern = re.compile(
            r"window\._ROUTER_DATA\s*=\s*(.*?)</script>",
            flags=re.DOTALL,
        )

        response = await self._fetch_html(url, follow_redirects=False)
        matched = pattern.search(response.text) if response.text else None
        if not matched or not matched.group(1):
            response = await self._fetch_html(url, follow_redirects=True)
            matched = pattern.search(response.text) if response.text else None
        if not matched or not matched.group(1):
            raise DouyinParseError("missing router data")

        router_data = msgspec.json.decode(matched.group(1).strip(), type=RouterData)
        video_data = router_data.video_data

        image_url_candidates = video_data.image_url_candidates
        image_urls = video_data.image_urls
        dynamic_url_candidates = video_data.dynamic_url_candidates
        dynamic_urls = [urls[0] for urls in dynamic_url_candidates]
        video_urls = video_data.video_urls
        video_url = video_data.video_url
        if image_urls or dynamic_urls:
            video_urls = []
            video_url = None
        cover_url = video_data.cover_url
        duration = video_data.video.duration if video_data.video else 0

        return DouyinResult(
            title=video_data.desc,
            author=video_data.author.nickname,
            author_avatar=video_data.avatar_url,
            duration=duration,
            video_url=video_url,
            cover_url=cover_url,
            image_urls=image_urls,
            dynamic_urls=dynamic_urls,
            source_url=source_url,
            item_id=video_data.aweme_id,
            video_urls=video_urls,
            image_url_candidates=image_url_candidates,
            dynamic_url_candidates=dynamic_url_candidates,
        )

    async def parse_iteminfo(self, video_id: str, source_url: str) -> DouyinResult:
        url = "https://www.iesdouyin.com/web/api/v2/aweme/iteminfo/"
        params = {"item_ids": video_id}
        headers = {**ANDROID_HEADERS, "Referer": "https://www.douyin.com/"}
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            response = await client.get(url, params=params)
        data = None
        if response.status_code == 200 and response.content:
            try:
                data = response.json()
            except ValueError:
                data = None
        items = (
            (data or {}).get("item_list")
            or (data or {}).get("aweme_details")
            or (data or {}).get("aweme_list")
            or []
        )
        if not items:
            item = await self.guest_api.fetch_detail(video_id)
        else:
            item = items[0] or {}

        author = item.get("author") or {}
        author_name = author.get("nickname")
        author_avatar = self._pick_url(
            (author.get("avatar_thumb") or {}).get("url_list")
            or (author.get("avatar_larger") or {}).get("url_list")
            or (author.get("avatar_medium") or {}).get("url_list")
        )

        video = item.get("video") or {}
        play_addr = (
            video.get("play_addr")
            or video.get("play_addr_h264")
            or video.get("play_addr_h265")
            or video.get("play_addr_lowbr")
            or {}
        )
        video_urls = self._select_highest_quality_video_urls(video, play_addr)
        video_url = video_urls[0] if video_urls else None
        selected_quality = self._find_selected_video_quality(video, video_url)
        if selected_quality:
            logger.debug(
                "🎵 抖音首选画质: %sx%s, %sKbps, 编码=%s, 档位=%s, 本档候选=%d, 总候选=%d",
                selected_quality["width"],
                selected_quality["height"],
                selected_quality["bit_rate"] // 1000,
                selected_quality["codec"],
                selected_quality["gear_name"] or "未知",
                selected_quality["candidate_count"],
                len(video_urls),
            )
        elif video_url:
            logger.debug("🎵 抖音首选画质: 默认播放地址, 总候选=%d", len(video_urls))

        cover = (
            video.get("cover")
            or video.get("origin_cover")
            or video.get("cover_hd")
            or {}
        )
        cover_url = self._pick_url(cover.get("url_list"))

        duration = item.get("duration") or video.get("duration") or 0

        image_urls: list[str] = []
        image_url_candidates: list[list[str]] = []
        dynamic_urls: list[str] = []
        dynamic_url_candidates: list[list[str]] = []
        for image in item.get("images") or []:
            image_candidates = list(
                dict.fromkeys(
                    url
                    for url in image.get("url_list") or []
                    if isinstance(url, str) and url
                )
            )
            if image_candidates:
                image_urls.append(image_candidates[0])
                image_url_candidates.append(image_candidates)
            image_video = image.get("video") or {}
            image_plays: list[str] = []
            for key in (
                "play_addr",
                "play_addr_h264",
                "play_addr_h265",
                "play_addr_lowbr",
            ):
                for url in self._order_video_urls(
                    (image_video.get(key) or {}).get("url_list")
                ):
                    if url not in image_plays:
                        image_plays.append(url)
            if image_plays:
                dynamic_urls.append(image_plays[0])
                dynamic_url_candidates.append(image_plays)

        if image_urls or dynamic_urls:
            video_urls = []
            video_url = None

        # 提取统计数据
        statistics = item.get("statistics") or {}
        likes = statistics.get("digg_count")
        comments = statistics.get("comment_count")

        return DouyinResult(
            title=item.get("desc"),
            author=author_name,
            author_avatar=author_avatar,
            duration=duration,
            video_url=video_url,
            cover_url=cover_url,
            image_urls=image_urls,
            dynamic_urls=dynamic_urls,
            source_url=source_url,
            likes=likes,
            comments=comments,
            item_id=video_id,
            video_urls=video_urls,
            image_url_candidates=image_url_candidates,
            dynamic_url_candidates=dynamic_url_candidates,
        )

    # region parse slides
    async def parse_slides(self, video_id: str, source_url: str) -> DouyinResult:
        url = "https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/"
        params = {
            "aweme_ids": f"[{video_id}]",
            "request_source": "200",
        }
        headers = {**ANDROID_HEADERS, "Referer": "https://www.douyin.com/"}
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            response = await client.get(url, params=params)
        response.raise_for_status()

        slides_data = msgspec.json.decode(
            response.content, type=SlidesInfo
        ).aweme_details
        if not slides_data:
            raise DouyinParseError("slides data is empty")
        slides = slides_data[0]

        return DouyinResult(
            title=slides.desc,
            author=slides.name,
            author_avatar=slides.avatar_url,
            duration=0,
            video_url=None,
            cover_url=None,
            image_urls=slides.image_urls,
            dynamic_urls=slides.dynamic_urls,
            source_url=source_url,
            image_url_candidates=slides.image_url_candidates,
            dynamic_url_candidates=slides.dynamic_url_candidates,
        )

    @staticmethod
    def _pick_url(urls) -> str | None:
        if not urls:
            return None
        for url in urls:
            if isinstance(url, str) and url:
                return url
        return None

    @staticmethod
    def _pick_video_url(urls) -> str | None:
        ordered_urls = DouyinExtractor._order_video_urls(urls)
        return ordered_urls[0] if ordered_urls else None

    @staticmethod
    def _order_video_urls(urls) -> list[str]:
        if not urls:
            return []
        valid_urls: list[str] = []
        for url in urls:
            if not isinstance(url, str) or not url:
                continue
            url = url.replace("playwm", "play")
            if url not in valid_urls:
                valid_urls.append(url)
        return sorted(
            valid_urls,
            key=lambda url: (
                urlparse(url).hostname not in {"douyin.com", "www.douyin.com"}
            ),
        )

    @staticmethod
    def _select_highest_quality_video_urls(
        video: dict, fallback_play_addr: dict
    ) -> list[str]:
        def number(value) -> int:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        def quality_key(rate: dict) -> tuple[int, int, int]:
            play_addr = rate.get("play_addr") or {}
            bit_rate = rate.get("bit_rate") or play_addr.get("bit_rate") or 0
            width = play_addr.get("width") or rate.get("width") or 0
            height = play_addr.get("height") or rate.get("height") or 0
            data_size = play_addr.get("data_size") or rate.get("data_size") or 0
            return number(width) * number(height), number(bit_rate), number(data_size)

        candidates: list[str] = []
        quality_rates = [
            rate for rate in video.get("bit_rate") or [] if isinstance(rate, dict)
        ]
        for rate in sorted(quality_rates, key=quality_key, reverse=True):
            play_addr = rate.get("play_addr") or {}
            for url in DouyinExtractor._order_video_urls(play_addr.get("url_list")):
                if url not in candidates:
                    candidates.append(url)

        fallback_addrs = [
            video.get(key) or {}
            for key in (
                "play_addr",
                "play_addr_h264",
                "play_addr_h265",
                "play_addr_lowbr",
            )
        ]
        fallback_addrs.append(fallback_play_addr)
        for play_addr in fallback_addrs:
            for url in DouyinExtractor._order_video_urls(play_addr.get("url_list")):
                if url not in candidates:
                    candidates.append(url)
        return candidates

    @staticmethod
    def _find_selected_video_quality(
        video: dict, selected_url: str | None
    ) -> dict | None:
        if not selected_url:
            return None

        def number(value) -> int:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        for rate in video.get("bit_rate") or []:
            if not isinstance(rate, dict):
                continue
            play_addr = rate.get("play_addr") or {}
            candidates = DouyinExtractor._order_video_urls(play_addr.get("url_list"))
            if selected_url not in candidates:
                continue
            is_h265 = rate.get("is_h265")
            codec = "H.265" if is_h265 == 1 else "H.264" if is_h265 == 0 else "未知"
            return {
                "width": number(play_addr.get("width") or rate.get("width")),
                "height": number(play_addr.get("height") or rate.get("height")),
                "bit_rate": number(rate.get("bit_rate") or play_addr.get("bit_rate")),
                "codec": codec,
                "gear_name": rate.get("gear_name"),
                "candidate_count": len(candidates),
            }
        return None

    async def _fetch_html(self, url: str, follow_redirects: bool) -> httpx.Response:
        headers = {**IOS_HEADERS, "Referer": "https://www.douyin.com/"}
        async with httpx.AsyncClient(timeout=self.timeout, headers=headers) as client:
            response = await client.get(url, follow_redirects=follow_redirects)
        if response.status_code != 200:
            raise DouyinParseError(f"status: {response.status_code}")
        return response


# endregion


# region 导出
__all__ = [
    "ANDROID_HEADERS",
    "DOUYIN_MESSAGE_PATTERN",
    "DOUYIN_SHORT_LINK_PATTERN",
    "IOS_HEADERS",
    "DouyinExtractor",
    "DouyinParseError",
    "DouyinResult",
    "DouyinCardRenderer",
    "extract_douyin_links",
]
# endregion
