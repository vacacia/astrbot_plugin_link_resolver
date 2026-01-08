# region 导入
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

# endregion


# region 数据结构
@dataclass(slots=True)
class CardTheme:
    """卡片主题配置"""

    name: str  # 平台显示名称
    accent_color: tuple[int, int, int]  # 品牌主题色

    # 背景与文字
    bg_color: tuple[int, int, int] = (255, 255, 255)
    text_color: tuple[int, int, int] = (34, 34, 34)
    meta_color: tuple[int, int, int] = (120, 120, 120)

    # 阴影
    shadow_color: tuple[int, int, int, int] = (0, 0, 0, 38)
    shadow_offset: int = 8
    shadow_blur: int = 20

    # 是否为暗色主题
    is_dark: bool = False


# endregion


# region 平台主题定义
# ================================
# 小红书 Xiaohongshu
# ================================
XIAOHONGSHU_LIGHT = CardTheme(
    name="小红书",
    accent_color=(255, 46, 85),  # #FF2E55
    bg_color=(255, 255, 255),
    text_color=(34, 34, 34),
    meta_color=(120, 120, 120),
    shadow_color=(0, 0, 0, 38),
    is_dark=False,
)

XIAOHONGSHU_DARK = CardTheme(
    name="小红书",
    accent_color=(255, 76, 110),  # 暗色模式下稍微提亮
    bg_color=(30, 30, 30),
    text_color=(240, 240, 240),
    meta_color=(160, 160, 160),
    shadow_color=(0, 0, 0, 80),
    is_dark=True,
)

# ================================
# 抖音 Douyin
# ================================
DOUYIN_LIGHT = CardTheme(
    name="抖音",
    accent_color=(254, 44, 85),  # #FE2C55
    bg_color=(255, 255, 255),
    text_color=(34, 34, 34),
    meta_color=(120, 120, 120),
    shadow_color=(0, 0, 0, 38),
    is_dark=False,
)

DOUYIN_DARK = CardTheme(
    name="抖音",
    accent_color=(254, 74, 110),
    bg_color=(22, 24, 35),  # 抖音暗色背景
    text_color=(240, 240, 240),
    meta_color=(160, 160, 160),
    shadow_color=(0, 0, 0, 80),
    is_dark=True,
)

# ================================
# B站 Bilibili
# ================================
BILIBILI_LIGHT = CardTheme(
    name="哔哩哔哩",
    accent_color=(251, 114, 153),  # #FB7299
    bg_color=(255, 255, 255),
    text_color=(34, 34, 34),
    meta_color=(120, 120, 120),
    shadow_color=(0, 0, 0, 38),
    is_dark=False,
)

BILIBILI_DARK = CardTheme(
    name="哔哩哔哩",
    accent_color=(251, 134, 168),
    bg_color=(24, 25, 28),  # B站暗色背景
    text_color=(240, 240, 240),
    meta_color=(160, 160, 160),
    shadow_color=(0, 0, 0, 80),
    is_dark=True,
)


# endregion


# region 主题选择
PlatformName = Literal["xiaohongshu", "douyin", "bilibili"]

_THEMES: dict[PlatformName, tuple[CardTheme, CardTheme]] = {
    "xiaohongshu": (XIAOHONGSHU_LIGHT, XIAOHONGSHU_DARK),
    "douyin": (DOUYIN_LIGHT, DOUYIN_DARK),
    "bilibili": (BILIBILI_LIGHT, BILIBILI_DARK),
}


def is_dark_mode_time(hour: int | None = None) -> bool:
    """判断当前是否应使用暗色模式

    规则: 19:00 - 次日 08:00 使用暗色模式
    """
    if hour is None:
        hour = datetime.now().hour
    return hour >= 19 or hour < 8


def get_theme_for_platform(
    platform: PlatformName,
    force_dark: bool | None = None,
) -> CardTheme:
    """获取指定平台的主题

    Args:
        platform: 平台名称
        force_dark: 强制指定明暗模式，None 则自动根据时间判断
    """
    light_theme, dark_theme = _THEMES[platform]

    if force_dark is not None:
        return dark_theme if force_dark else light_theme

    return dark_theme if is_dark_mode_time() else light_theme


# endregion


# region 导出
__all__ = [
    "CardTheme",
    "PlatformName",
    "XIAOHONGSHU_LIGHT",
    "XIAOHONGSHU_DARK",
    "DOUYIN_LIGHT",
    "DOUYIN_DARK",
    "BILIBILI_LIGHT",
    "BILIBILI_DARK",
    "is_dark_mode_time",
    "get_theme_for_platform",
]
# endregion
