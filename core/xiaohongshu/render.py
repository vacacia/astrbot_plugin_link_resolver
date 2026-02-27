# region 导入
"""小红书卡片渲染器

使用通用渲染器 + 小红书主题实现。
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
class XiaohongshuCardRenderer:
    """小红书卡片渲染器

    保持与原有接口兼容，内部使用通用渲染器实现。

    特性:
    - 圆角卡片设计
    - 阴影效果
    - 现代配色方案
    - 完整文字显示（无截断）
    - 图片网格布局
    - 视频播放图标
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

            logger.warning("⚠️ 小红书渲染器未找到中文字体，预览图可能出现乱码")

    def render(
        self,
        *,
        title: str | None,
        author: str | None,
        text: str | None,
        image_paths: Iterable[Path] | None = None,
        cover_path: Path | None = None,
        is_video: bool = False,
    ) -> Image.Image:
        """渲染小红书卡片

        Args:
            title: 标题
            author: 作者
            text: 正文内容
            image_paths: 图片路径列表
            cover_path: 封面图路径（视频笔记）
            is_video: 是否为视频笔记

        Returns:
            渲染完成的 PIL Image
        """
        # 获取当前时间对应的主题
        theme = get_theme_for_platform("xiaohongshu")

        # 创建通用渲染器
        renderer = UniversalCardRenderer(theme, self.font_path)

        # 构建数据
        data = CardData(
            title=title,
            author=author,
            text=text,
            image_paths=list(image_paths) if image_paths else [],
            cover_path=cover_path,
            is_video=is_video,
        )

        return renderer.render(data)


# endregion


# region 导出
__all__ = ["XiaohongshuCardRenderer", "find_default_font"]
# endregion
