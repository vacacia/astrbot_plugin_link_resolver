# region 导入
"""B站卡片渲染器

使用通用渲染器 + B站主题实现。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from PIL import Image

from ..common.card_renderer import (
    CardData,
    UniversalCardRenderer,
    find_default_font,
    get_theme_for_platform,
)

# endregion


# region 渲染器
class BilibiliCardRenderer:
    """B站卡片渲染器

    特性:
    - 圆角卡片设计
    - B站粉色主题
    - 播放量/弹幕/点赞统计徽章
    - 自动明暗主题切换（19:00-08:00 暗色）
    """

    def __init__(self, font_path: Path | None = None):
        """初始化渲染器

        Args:
            font_path: 自定义字体路径，None 则自动查找
        """
        self.font_path = font_path or find_default_font()

        if not self.font_path:
            from astrbot.api import logger

            logger.warning("⚠️ B站渲染器未找到中文字体，预览图可能出现乱码")

    def render(
        self,
        *,
        title: str | None,
        author: str | None,
        cover_path: Path | None = None,
        views: str | None = None,
        danmaku: str | None = None,
        likes: str | None = None,
    ) -> Image.Image:
        """渲染B站视频卡片

        Args:
            title: 视频标题
            author: UP主名称
            cover_path: 封面图路径
            views: 播放量（如 "12.3万"）
            danmaku: 弹幕数（如 "5678"）
            likes: 点赞数（如 "9.8万"）

        Returns:
            渲染完成的 PIL Image
        """
        # 获取当前时间对应的主题
        theme = get_theme_for_platform("bilibili")

        # 创建通用渲染器
        renderer = UniversalCardRenderer(theme, self.font_path)

        # 构建统计徽章
        stats: dict[str, str] = {}
        if views:
            stats["👁"] = views
        if danmaku:
            stats["💬"] = danmaku
        if likes:
            stats["👍"] = likes

        # 构建数据
        data = CardData(
            title=title,
            author=author,
            text=None,
            image_paths=[],
            cover_path=cover_path,
            is_video=True,
            stats=stats if stats else None,
        )

        return renderer.render(data)


# endregion


# region 导出
__all__ = ["BilibiliCardRenderer"]
# endregion
