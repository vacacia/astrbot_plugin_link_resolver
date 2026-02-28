# region 插件路径管理器（延迟初始化）
"""
使用 StarTools.get_data_dir() 获取数据存储目录。
在首次访问时延迟初始化，因为 StarTools 需要在 AstrBot 上下文初始化后才能使用。
"""
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

PLUGIN_NAME = "astrbot_plugin_link_resolver"

# 路径缓存
_data_dir: Path | None = None
_initialized: bool = False


def _get_data_dir() -> Path:
    """获取插件数据目录（延迟初始化）"""
    global _data_dir, _initialized
    if _data_dir is not None:
        return _data_dir
    
    try:
        from astrbot.api.star import StarTools
        _data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        _initialized = True
    except Exception:
        # 回退到插件目录（开发/测试环境）
        _data_dir = Path(__file__).resolve().parents[2] / "data"
        _data_dir.mkdir(parents=True, exist_ok=True)
    
    return _data_dir


def _ensure_dir(path: Path) -> Path:
    """确保目录存在"""
    path.mkdir(parents=True, exist_ok=True)
    return path


# region 路径获取函数
def get_cache_path() -> Path:
    """获取缓存根目录"""
    return _ensure_dir(_get_data_dir() / "cache")


def get_cookies_path() -> Path:
    """获取 Cookies 目录"""
    return _ensure_dir(_get_data_dir() / "cookies")


def get_bili_cookies_file() -> Path:
    """获取 B站 Cookies 文件路径"""
    return get_cookies_path() / "bili_cookies.txt"


# Bilibili 路径
def get_bilibili_cache() -> Path:
    return _ensure_dir(get_cache_path() / "bilibili")


def get_bilibili_video_path() -> Path:
    return _ensure_dir(get_bilibili_cache() / "videos")


def get_bilibili_thumb_path() -> Path:
    return _ensure_dir(get_bilibili_cache() / "thumbnails")


def get_bilibili_card_path() -> Path:
    return _ensure_dir(get_bilibili_cache() / "cards")


# Douyin 路径
def get_douyin_cache() -> Path:
    return _ensure_dir(get_cache_path() / "douyin")


def get_douyin_video_path() -> Path:
    return _ensure_dir(get_douyin_cache() / "videos")


def get_douyin_image_path() -> Path:
    return _ensure_dir(get_douyin_cache() / "images")


def get_douyin_card_path() -> Path:
    return _ensure_dir(get_douyin_cache() / "cards")


# Xiaohongshu 路径
def get_xhs_cache() -> Path:
    return _ensure_dir(get_cache_path() / "xiaohongshu")


def get_xhs_video_path() -> Path:
    return _ensure_dir(get_xhs_cache() / "videos")


def get_xhs_image_path() -> Path:
    return _ensure_dir(get_xhs_cache() / "images")


def get_xhs_card_path() -> Path:
    return _ensure_dir(get_xhs_cache() / "cards")
# endregion
# endregion

