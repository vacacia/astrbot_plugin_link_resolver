# region 插件路径
from pathlib import Path

PLUGIN_NAME = "astrbot_plugin_myparser"

# 使用 __file__ 获取正确的插件根目录，避免相对路径问题
PLUGIN_PATH = Path(__file__).resolve().parents[2]  # core/common/paths.py -> 上两级

# 缓存根目录
CACHE_PATH = PLUGIN_PATH / "cache"

# Cookies 目录
COOKIES_PATH = PLUGIN_PATH / "cookies"
BILI_COOKIES_FILE = COOKIES_PATH / "bili_cookies.txt"
XHS_COOKIES_FILE = COOKIES_PATH / "xhs_cookies.txt"

# 各平台缓存目录
BILIBILI_CACHE = CACHE_PATH / "bilibili"
BILIBILI_VIDEO_PATH = BILIBILI_CACHE / "videos"
BILIBILI_THUMB_PATH = BILIBILI_CACHE / "thumbnails"
BILIBILI_CARD_PATH = BILIBILI_CACHE / "cards"

DOUYIN_CACHE = CACHE_PATH / "douyin"
DOUYIN_VIDEO_PATH = DOUYIN_CACHE / "videos"
DOUYIN_IMAGE_PATH = DOUYIN_CACHE / "images"
DOUYIN_CARD_PATH = DOUYIN_CACHE / "cards"

XHS_CACHE = CACHE_PATH / "xiaohongshu"
XHS_VIDEO_PATH = XHS_CACHE / "videos"
XHS_IMAGE_PATH = XHS_CACHE / "images"
XHS_CARD_PATH = XHS_CACHE / "cards"

# 创建目录
for path in [
    COOKIES_PATH,
    BILIBILI_VIDEO_PATH, BILIBILI_THUMB_PATH, BILIBILI_CARD_PATH,
    DOUYIN_VIDEO_PATH, DOUYIN_IMAGE_PATH, DOUYIN_CARD_PATH,
    XHS_VIDEO_PATH, XHS_IMAGE_PATH, XHS_CARD_PATH,
]:
    path.mkdir(parents=True, exist_ok=True)
# endregion
