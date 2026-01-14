"""小红书内容提取器 - 基于 astrbot_plugin_parser 参考实现重写"""

import json
import re
from dataclasses import dataclass
from http import cookiejar
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger

# Cookie 文件路径（从 common 模块导入）
from ..common import get_xhs_cookies_file


# region 常量
XHS_SHORT_LINK_PATTERN = r"(?:https?://)?(?:www\.)?xhslink\.com/[A-Za-z0-9._?%&+=/#@-]+"
XHS_MESSAGE_PATTERN = (
    r"(?s).*(?:"
    + XHS_SHORT_LINK_PATTERN
    + r"|(?:https?://)?(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/[0-9a-zA-Z]+)"
)

# 通用 headers
_COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/55.0.2883.87 UBrowser/6.2.4098.3 Safari/537.36"
    )
}

# headers (用于短链接重定向)
XHS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/16.6 Mobile/15E148 Safari/604.1 Edg/132.0.0.0"
    ),
    "origin": "https://www.xiaohongshu.com",
    "x-requested-with": "XMLHttpRequest",
    "sec-fetch-site": "same-origin",
    "sec-fetch-mode": "cors",
    "sec-fetch-dest": "empty",
}

# explore 页面 headers
_EXPLORE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/55.0.2883.87 UBrowser/6.2.4098.3 Safari/537.36"
    ),
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
}



_SHORT_RE = re.compile(XHS_SHORT_LINK_PATTERN, re.IGNORECASE)
_NOTE_ID_RE = re.compile(r"/(?:explore|discovery/item)/(?P<id>[0-9a-zA-Z]+)", re.IGNORECASE)
_LONG_RE = re.compile(
    r"(?:https?://)?(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/[0-9a-zA-Z]+[A-Za-z0-9._%?&+=/#@-]*",
    re.IGNORECASE
)
# endregion


def extract_xhs_links(text: str) -> list[str]:
    """从文本中提取所有小红书链接"""
    links = []
    # 短链接
    for match in _SHORT_RE.finditer(text):
        url = match.group(0)
        if not url.startswith("http"):
            url = "https://" + url
        links.append(url)
    # 长链接
    for match in _LONG_RE.finditer(text):
        url = match.group(0)
        if not url.startswith("http"):
            url = "https://" + url
        if url not in links:
            links.append(url)
    return links


# region 数据类
@dataclass(slots=True)
class XiaohongshuResult:
    title: str | None
    author: str | None
    text: str | None
    image_urls: list[str]
    file_ids: list[str]  # 用于尝试下载原图
    video_url: str | None
    cover_url: str | None
    source_url: str
    note_id: str | None = None


class XiaohongshuParseError(RuntimeError):
    pass
# endregion


def load_xhs_cookies() -> dict[str, str]:
    """加载小红书 cookies（支持 JSON 格式或 Netscape cookies.txt 格式）"""
    cookies_file = get_xhs_cookies_file()
    if not cookies_file.exists():
        return {}
    try:
        raw = cookies_file.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        # 尝试 JSON 格式
        if raw.lstrip().startswith("{"):
            data = json.loads(raw)
            if isinstance(data, dict):
                cookies = {str(k): str(v) for k, v in data.items()}
                logger.info("🍪 小红书 cookies 加载成功 (JSON): %d 个", len(cookies))
                return cookies
    except Exception:
        pass

    # 尝试 Netscape cookies.txt 格式
    try:
        jar = cookiejar.MozillaCookieJar()
        jar.load(str(get_xhs_cookies_file()), ignore_discard=True, ignore_expires=True)
        cookies = {cookie.name: cookie.value for cookie in jar}
        logger.info("🍪 小红书 cookies 加载成功 (Netscape): %d 个", len(cookies))
        return cookies
    except Exception as exc:
        logger.warning("🍪 小红书 cookies 加载失败: %s", str(exc))
        return {}


class XiaohongshuExtractor:
    """小红书内容提取器"""

    def __init__(self, timeout: float = 15.0, cookies: dict[str, str] | None = None):
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.cookies = cookies or load_xhs_cookies()

    async def parse(self, text_or_url: str) -> XiaohongshuResult:
        """解析小红书链接"""
        url = text_or_url.strip()
        if not url.startswith("http"):
            url = "https://" + url

        # 短链接需要先获取重定向目标
        if _SHORT_RE.search(url):
            url = await self._get_redirect_url(url)
            logger.debug("XHS short url resolved: %s", url)

        # 提取 note_id
        match = _NOTE_ID_RE.search(url)
        if not match:
            raise XiaohongshuParseError(f"无法从URL提取笔记ID: {url}")
        note_id = match.group("id")
        logger.debug("XHS note id: %s", note_id)

        # 构建 explore URL（比 discovery/item 更稳定）
        # 保留原始查询参数
        query_string = ""
        if "?" in url:
            query_string = "?" + url.split("?", 1)[1]
        explore_url = f"https://www.xiaohongshu.com/explore/{note_id}{query_string}"

        # 尝试 explore 页面解析
        try:
            return await self._parse_explore(explore_url, note_id)
        except XiaohongshuParseError:
            logger.debug("XHS explore parse failed, trying discovery")

        # 回退到 discovery 解析
        if "/discovery/item/" in url:
            return await self._parse_discovery(url)

        raise XiaohongshuParseError("无法解析小红书内容")

    async def _get_redirect_url(self, url: str) -> str:
        """获取短链接重定向目标（单次重定向）"""
        async with aiohttp.ClientSession(timeout=self.timeout, cookies=self.cookies) as session:
            async with session.get(url, headers=XHS_HEADERS, allow_redirects=False) as resp:
                if resp.status >= 400:
                    raise XiaohongshuParseError(f"短链接请求失败: {resp.status}")
                location = resp.headers.get("Location", url)
                return location

    async def _parse_explore(self, url: str, note_id: str) -> XiaohongshuResult:
        """解析 explore 页面"""
        async with aiohttp.ClientSession(timeout=self.timeout, cookies=self.cookies) as session:
            async with session.get(url, headers=_EXPLORE_HEADERS) as resp:
                logger.debug("XHS explore url: %s, status: %s", resp.url, resp.status)
                if resp.status != 200:
                    raise XiaohongshuParseError(f"页面请求失败: {resp.status}")
                html = await resp.text()

        json_obj = self._extract_initial_state(html)

        # 路径: ["note"]["noteDetailMap"][note_id]["note"]
        note_data = (
            json_obj.get("note", {})
            .get("noteDetailMap", {})
            .get(note_id, {})
            .get("note", {})
        )
        if not note_data:
            raise XiaohongshuParseError("无法找到笔记详情数据")

        return self._build_result_from_note(note_data, url)

    async def _parse_discovery(self, url: str) -> XiaohongshuResult:
        """解析 discovery 页面"""
        async with aiohttp.ClientSession(timeout=self.timeout, cookies=self.cookies) as session:
            async with session.get(url, headers=XHS_HEADERS, allow_redirects=True) as resp:
                logger.debug("XHS discovery url: %s, status: %s", resp.url, resp.status)
                if resp.status != 200:
                    raise XiaohongshuParseError(f"页面请求失败: {resp.status}")
                html = await resp.text()

        json_obj = self._extract_initial_state(html)

        note_data_wrapper = json_obj.get("noteData")
        if not note_data_wrapper:
            raise XiaohongshuParseError("无法找到 noteData")

        preload_data = note_data_wrapper.get("normalNotePreloadData", {})
        note_data = note_data_wrapper.get("data", {}).get("noteData", {})
        if not note_data:
            raise XiaohongshuParseError("无法找到 noteData.data.noteData")

        return self._build_result_from_note(note_data, url, preload_data)

    def _extract_initial_state(self, html: str) -> dict[str, Any]:
        """从HTML中提取 __INITIAL_STATE__ JSON"""
        pattern = r"window\.__INITIAL_STATE__=(.*?)</script>"
        match = re.search(pattern, html)
        if not match:
            raise XiaohongshuParseError("小红书分享链接失效或内容已删除")

        json_str = match.group(1).replace("undefined", "null")
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            raise XiaohongshuParseError(f"JSON解析失败: {e}") from e

    def _build_result_from_note(
        self,
        note: dict[str, Any],
        source_url: str,
        preload: dict[str, Any] | None = None,
    ) -> XiaohongshuResult:
        """从笔记数据构建结果"""
        # 标题和描述
        title = note.get("title") or note.get("shareTitle") or ""
        desc = note.get("desc") or note.get("shareDesc") or ""

        # 作者
        user = note.get("user") or note.get("author") or {}
        author = user.get("nickname") or user.get("nickName") or user.get("name")

        # 图片列表 - 获取 URL 和 fileId
        image_list = note.get("imageList") or []
        image_urls = []
        file_ids = []
        for i, img in enumerate(image_list):
            if isinstance(img, dict):
                # 记录详细的图片数据以便排查旧版笔记
                logger.debug(f"XHS Image[{i}] full data: {img}")
                
                # 获取图片 URL
                img_url = self._get_original_image_url(img)
                if img_url:
                    image_urls.append(img_url)
                # 获取 fileId 用于尝试原图
                file_id = self._get_file_id_from_image(img)
                file_ids.append(file_id)  # 可能是 None，保持索引对应
        
        logger.debug(f"XHS Extracted file_ids: {file_ids}")

        # 视频
        video_url = self._extract_video_url(note)

        # 封面（视频的第一帧或第一张图片）
        cover_url = None
        if preload:
            # preload 中的图片通常是无水印的
            preload_images = preload.get("imagesList") or []
            if preload_images and isinstance(preload_images[0], dict):
                cover_url = (
                    preload_images[0].get("urlSizeLarge")
                    or preload_images[0].get("url")
                )
        if not cover_url and image_urls:
            cover_url = image_urls[0]

        note_type = note.get("type", "")
        note_id = note.get("noteId") or note.get("id")

        logger.info(
            "XHS 解析完成: type=%s, images=%d, video=%s",
            note_type, len(image_urls), "有" if video_url else "无"
        )

        return XiaohongshuResult(
            title=title if title else None,
            author=author,
            text=desc if desc else None,
            image_urls=image_urls,
            file_ids=file_ids,
            video_url=video_url,
            cover_url=cover_url,
            source_url=source_url,
            note_id=note_id,
        )

    def _get_original_image_url(self, img: dict[str, Any]) -> str | None:
        """从图片对象获取最佳图片 URL
        
        优先级：
        1. urlDefault (高清，通常可用)
        2. url (普通)
        如果有 cookies，会在 handler 层尝试 ci.xiaohongshu.com 原图
        """
        # 优先使用 urlDefault（高清预览，通常可用）
        url = img.get("urlDefault") or img.get("url")
        if not url:
            return None
        
        # 清理样式后缀以获得更高清的图片
        if "!" in url:
            url = url.split("!", 1)[0]
        
        return url

    def _get_file_id_from_image(self, img: dict[str, Any]) -> str | None:
        """从图片对象提取 fileId，用于构建原图 URL"""
        # 1. 尝试从字段获取
        file_id = img.get("fileId") or img.get("file_id") or img.get("traceId")
        
        # 2. 如果字段没有，从 urlDefault / url / urlPre 中提取
        if not file_id:
            for key in ("urlDefault", "url", "urlPre"):
                url = img.get(key)
                if isinstance(url, str) and url:
                    extracted = self._extract_file_id_from_url(url)
                    if extracted:
                        file_id = extracted
                        # logger.debug("XHS 从 URL %s 提取到 fileId: %s", key, file_id)
                        break
        
        if file_id and "!" in file_id:
            file_id = file_id.split("!", 1)[0]
            
        return file_id

    @staticmethod
    def _extract_file_id_from_url(url: str) -> str | None:
        """从 URL 中提取 fileId"""
        if not url:
            return None
        # 常见格式：
        # 1. .../spectrum/1040g0k...
        # 2. .../notes_pre_post/1040g0k...
        # 3. .../1040g0k... (直接文件名)
        
        # 匹配 spectrum 后的 id
        if "spectrum/" in url:
            match = re.search(r"spectrum/([a-zA-Z0-9]+)", url)
            if match:
                return match.group(1)
                
        # 匹配直接的文件名（通常是20位以上的字母数字组合）
        # 排除 !style, ? 等后缀
        base_name = url.split("?")[0].split("!")[0]
        base_name = base_name.split("/")[-1]
        
        if len(base_name) > 20 and re.match(r"^[a-zA-Z0-9]+$", base_name):
            return base_name
            
        return None

    def _extract_video_url(self, note: dict[str, Any]) -> str | None:
        """提取视频URL"""
        if note.get("type") != "video":
            return None

        video = note.get("video")
        if not isinstance(video, dict):
            return None

        media = video.get("media")
        if not isinstance(media, dict):
            return None

        stream = media.get("stream")
        if not isinstance(stream, dict):
            return None

        # h265 无水印优先，其次 h264
        for codec in ("h265", "h264", "av1", "h266"):
            codec_streams = stream.get(codec)
            if isinstance(codec_streams, list) and codec_streams:
                master_url = codec_streams[0].get("masterUrl")
                if master_url:
                    logger.debug("XHS video codec: %s", codec)
                    return master_url

        return None


# 导出
__all__ = [
    "XHS_HEADERS",
    "XHS_MESSAGE_PATTERN",
    "XHS_SHORT_LINK_PATTERN",
    "XiaohongshuExtractor",
    "XiaohongshuParseError",
    "XiaohongshuResult",
    "extract_xhs_links",
    "load_xhs_cookies",
]
