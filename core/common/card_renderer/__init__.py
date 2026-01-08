# region 导入
"""通用卡片渲染器模块

提供多平台卡片渲染能力，支持小红书、抖音、B站。
自动根据时间切换明暗主题。
"""
from .base import CardData, ImageGrid, UniversalCardRenderer
from .components import (
    add_frosted_glass,
    add_rounded_corners,
    add_shadow,
    create_gradient_bar,
    create_horizontal_gradient,
    create_rounded_rectangle,
    crop_to_square,
    draw_play_icon,
    draw_stat_badges,
    fit_image,
)
from .themes import (
    BILIBILI_DARK,
    BILIBILI_LIGHT,
    DOUYIN_DARK,
    DOUYIN_LIGHT,
    XIAOHONGSHU_DARK,
    XIAOHONGSHU_LIGHT,
    CardTheme,
    PlatformName,
    get_theme_for_platform,
    is_dark_mode_time,
)
from .utils import find_default_font, get_line_height, get_text_width, load_font, wrap_text

# endregion


# region 导出
__all__ = [
    # 核心类
    "UniversalCardRenderer",
    "CardData",
    "ImageGrid",
    # 主题
    "CardTheme",
    "PlatformName",
    "get_theme_for_platform",
    "is_dark_mode_time",
    "XIAOHONGSHU_LIGHT",
    "XIAOHONGSHU_DARK",
    "DOUYIN_LIGHT",
    "DOUYIN_DARK",
    "BILIBILI_LIGHT",
    "BILIBILI_DARK",
    # 组件
    "create_rounded_rectangle",
    "add_rounded_corners",
    "add_shadow",
    "create_gradient_bar",
    "create_horizontal_gradient",
    "add_frosted_glass",
    "draw_play_icon",
    "draw_stat_badges",
    "crop_to_square",
    "fit_image",
    # 工具
    "find_default_font",
    "load_font",
    "get_line_height",
    "get_text_width",
    "wrap_text",
]
# endregion
