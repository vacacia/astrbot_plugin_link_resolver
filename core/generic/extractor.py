"""通用站点解析: 凡是 yt-dlp 有专用提取器认识的链接, 都交给 yt-dlp 下载.

设计出发点: 用户期望"链接来了就能解析", 不想手工维护站点名单. yt-dlp 自带上千个
站点提取器, ``suitable(url)`` 只做本地正则匹配, 用它判断"认不认识"即可.

安全边界(重要):
- 禁用 yt-dlp 的 generic 提取器(它会抓取任意网页, 包括内网地址), 只允许站点专用提取器.
  因此新闻页, 内网地址, IP 字面量都不会命中, 也就不会被访问.
- ``DEDICATED_DOMAINS`` 是已有专用解析器的平台, 通用解析永不接管, 防止重复处理,
  也避免绕过这些平台各自的启用开关.
- 用户可通过配置追加 ``blocked_domains`` 排除不想解析的站点(默认为空).
- yt-dlp 的转发型提取器(如 facebook.com/flx/warn)会把请求交给 ``u=`` 参数指向的
  任意地址, 所以入口检查会扫描解码后 URL 里出现的每一个主机名, 解析完成后还要校验
  视频真实页面地址, 都不能落在被排除的域名上.
- 剩余风险: 服务端 302 跳转和提取器内部发起的二次请求仍会发出, 入口检查管不到.
  若在意内网探测风险, 请在容器/宿主机网络层限制出口, 这是部署层的事, 插件管不到.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Iterable
from functools import lru_cache
from typing import Any
from urllib.parse import unquote, urlparse

from ..youtube.extractor import (
    YoutubeExtractor,
    YoutubeParseError,
    YoutubeResult,
    _normalize_url,
)

# 已有专用解析器的平台, 通用解析永不接管(防止重复处理, 也避免绕过各自的启用开关)
DEDICATED_DOMAINS: tuple[str, ...] = (
    # B站
    "bilibili.com",
    "b23.tv",
    "bili2233.cn",
    # 抖音
    "douyin.com",
    "iesdouyin.com",
    # 小红书
    "xiaohongshu.com",
    "xhslink.com",
    "xhslink.cn",
    # 微博
    "weibo.com",
    "weibo.cn",
    "t.cn",
    # X/Twitter
    "x.com",
    "twitter.com",
    # YouTube
    "youtube.com",
    "youtu.be",
)

# 对任意 http(s) 链接做粗匹配, 是否认识交给 yt-dlp 提取器判断
GENERIC_URL_PATTERN = r"(?s).*https?://"

_URL_RE = re.compile(
    r"https?://[^\s'\"<>　-〿一-鿿＀-￯]+",
    re.IGNORECASE,
)

_LOCAL_HOSTS = ("localhost", "localhost.localdomain")

# allowed_extractors 里的负向正则: 排除专用平台自己的提取器名, 防止
# facebook.com/flx/warn/?u=youtube.com/... 这类转发链接走进专用平台解析器
_EXCLUDED_EXTRACTORS = "-(?i:.*(youtube|bili|douyin|twitter|weibo|xiaohongshu|xhs).*)"

# URL 解码后其中每一个 http(s):// 的主机名(转发链接的目标常藏在 u=/url= 等参数里)
_INNER_HOST_RE = re.compile(r"https?://([^/?&#\s'\"<>:]+)", re.IGNORECASE)


def host_in_domains(host: str, domains: Iterable[str]) -> bool:
    """host 等于某域名或是其子域名(``vm.tiktok.com`` 属于 ``tiktok.com``)."""
    host = (host or "").lower().rstrip(".")
    return any(host == d or host.endswith(f".{d}") for d in domains)


def _normalize_domains(values: Iterable[Any]) -> tuple[str, ...]:
    domains: list[str] = []
    for value in values or []:
        text = str(value).strip().lower()
        text = re.sub("^https?://", "", text).split("/")[0].lstrip("*.")
        if not text or text in domains:
            continue
        domains.append(text)
    return tuple(domains)


def _is_local_or_ip_host(host: str) -> bool:
    """本机名与 IP 字面量一律拒绝.

    ``inet_aton`` 与实际连接行为一致, 能认出 ``127.1`` / 十进制 ``2130706433`` /
    十六进制等 ``ipaddress`` 不认识的 IP 变体写法; IPv6 字面量交给 ``ipaddress``.
    """
    host = (host or "").lower().rstrip(".").strip("[]").split("%", 1)[0]
    if not host or host in _LOCAL_HOSTS or host.endswith(".local"):
        return True
    try:
        socket.inet_aton(host)
        return True
    except OSError:
        pass
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


@lru_cache(maxsize=1)
def _dedicated_extractor_classes() -> tuple:
    """yt-dlp 的全部站点专用提取器(排除 generic). 首次调用约数百毫秒, 之后走缓存."""
    try:
        from yt_dlp.extractor import gen_extractor_classes
    except ImportError:
        return ()
    return tuple(
        ie
        for ie in gen_extractor_classes()
        if ie.IE_NAME != "generic" and ie.ie_key() != "Generic"
    )


@lru_cache(maxsize=2048)
def has_dedicated_extractor(url: str) -> bool:
    """是否存在 yt-dlp 专用提取器认识这个链接(只做本地正则匹配, 不发网络请求)."""
    return any(ie.suitable(url) for ie in _dedicated_extractor_classes())


def _precompile_extractor_patterns() -> None:
    """触发每个提取器的 ``_VALID_URL_RE`` 惰性编译.

    不预热的话, 第一条陌生链接要现编译约 1750 条正则, 卡约 0.4 秒.
    ``_match_valid_url`` 会把编译结果挂到类属性上, 一次预热永久生效.
    """
    for ie in _dedicated_extractor_classes():
        try:
            ie._match_valid_url("https://warm.up/")
        except Exception:
            continue


class GenericExtractor(YoutubeExtractor):
    """复用 YoutubeExtractor 的 yt-dlp 下载/取消/清理逻辑, 只替换 URL 校验, 选项和结果校验."""

    label = "通用解析"

    def __init__(self, blocked_domains: Iterable[Any] = (), **kwargs):
        super().__init__(**kwargs)
        self.set_blocked_domains(blocked_domains)

    def set_blocked_domains(self, values: Iterable[Any]) -> None:
        user_blocked = _normalize_domains(values)
        self.excluded_domains: tuple[str, ...] = tuple(
            dict.fromkeys([*DEDICATED_DOMAINS, *user_blocked])
        )

    @staticmethod
    def warm_up() -> None:
        """预加载提取器列表并预编译匹配正则, 避免第一条消息在事件循环里卡顿. 可放线程里调用."""
        _dedicated_extractor_classes()
        _precompile_extractor_patterns()

    def is_excluded_host(self, host: str) -> bool:
        return host_in_domains(host, self.excluded_domains)

    def _host_rejected(self, host: str) -> bool:
        return _is_local_or_ip_host(host) or self.is_excluded_host(host)

    def accepts(self, url: str) -> bool:
        normalized = _normalize_url(url)
        try:
            host = urlparse(normalized).hostname or ""
        except ValueError:
            return False
        if self._host_rejected(host):
            return False
        # 转发型链接把真实目标藏在参数里(如 flx/warn/?u=http://127.0.0.1/...),
        # 解码后 URL 里出现的每一个主机名都要过同样的校验
        if any(
            self._host_rejected(inner)
            for inner in _INNER_HOST_RE.findall(unquote(normalized))
        ):
            return False
        return has_dedicated_extractor(normalized)

    def extract_links(self, text: str) -> list[str]:
        """按消息顺序返回去重后 yt-dlp 认识的链接."""
        links: list[str] = []
        for match in _URL_RE.finditer(text or ""):
            url = _normalize_url(match.group(0))
            if url not in links and self.accepts(url):
                links.append(url)
        return links

    def _validate_url(self, url: str) -> str:
        normalized = _normalize_url(url)
        if not self.accepts(normalized):
            raise YoutubeParseError(f"不支持的链接(无适配的提取器或已被排除): {url}")
        return normalized

    def _base_options(
        self, cookie_file: str | None, *, player_client: str | None = None
    ) -> dict[str, Any]:
        # player_client 是 YouTube 专用参数, 通用站点无意义, 固定传 None
        options = super()._base_options(cookie_file, player_client=None)
        options["allowed_extractors"] = ["default", "-generic", _EXCLUDED_EXTRACTORS]
        # 双保险: 即便误入播放列表也最多取 1 个条目
        # (注意不要设 max_downloads: 下载成功后 yt-dlp 会因此抛 MaxDownloadsReached)
        options["playlist_items"] = "1"
        return options

    @staticmethod
    def _build_format_selector(
        *, max_height: int, video_codec: str, ffmpeg_available: bool
    ) -> str:
        """先保证能解析, 再尽量贴近用户设置的分辨率上限(分辨率优先, 容器偏好其次).

        - ``bv*[height<=?H]+ba/b[height<=?H]``: 不超限里取最高档; ``<=?``
          让高度未知的格式也能放行(很多站点没有 height 字段).
        - ``wv*+ba/wv*``: 全部超限时取最小一档, 即超限里最接近上限的;
          ``wv*`` 保证含视频, 不会误选纯音频格式.
        例(上限 720P): 360/720/1080 选 720; 只有 1080 选 1080; 只有 360 选 360.
        """
        height = f"[height<=?{max_height}]" if max_height > 0 else ""
        if ffmpeg_available:
            return f"bv*{height}+ba/b{height}/wv*+ba/wv*"
        return f"b{height}/wv*"

    def _result_from_info(self, info: dict[str, Any], source_url: str) -> YoutubeResult:
        if info.get("_type") in ("playlist", "multi_video"):
            raise YoutubeParseError("不是单个视频(主页/播放列表/标签/搜索)")
        if info.get("is_live") or info.get("live_status") in (
            "is_live",
            "is_upcoming",
        ):
            raise YoutubeParseError("直播/预告内容不支持")
        formats = info.get("formats") or []
        if formats and all(f.get("vcodec") == "none" for f in formats):
            raise YoutubeParseError("仅音频内容不支持")
        # 单格式提取器(如 Vocaroo)没有 formats 列表, 顶层 vcodec 也要检查
        if not formats and info.get("vcodec") == "none":
            raise YoutubeParseError("仅音频内容不支持")
        webpage_url = str(info.get("webpage_url") or "")
        try:
            host = urlparse(webpage_url).hostname or ""
        except ValueError:
            host = ""
        if host and self.is_excluded_host(host):
            raise YoutubeParseError(f"解析结果指向被排除的站点: {host}")
        return super()._result_from_info(info, source_url)
