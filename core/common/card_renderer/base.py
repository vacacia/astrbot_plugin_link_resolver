# region 导入
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw

from .components import (
    add_rounded_corners,
    add_shadow,
    create_gradient_bar,
    create_rounded_rectangle,
    crop_to_square,
    draw_play_icon,
    draw_stat_badges,
    fit_image,
)
from .themes import CardTheme
from .utils import find_default_font, get_line_height, get_text_width, load_font, wrap_text

# endregion


# region 数据结构
@dataclass(slots=True)
class ImageGrid:
    """图片网格布局信息"""

    images: list[Image.Image]
    cols: int
    rows: int
    tile_size: int
    height: int


@dataclass
class CardData:
    """统一卡片数据结构"""

    title: str | None = None
    author: str | None = None
    text: str | None = None
    image_paths: list[Path] = field(default_factory=list)
    cover_path: Path | None = None
    is_video: bool = False

    # 扩展字段：统计数据
    stats: dict[str, str] | None = None  # {"👁": "12.3万", "💬": "5678"}


# endregion


# region 通用渲染器
class UniversalCardRenderer:
    """通用卡片渲染器

    支持多平台主题，自动布局，现代化视觉效果。

    特性:
    - 圆角卡片设计
    - 柔和阴影效果
    - 渐变色标题栏
    - 图片网格布局
    - 视频播放图标
    - 统计数据徽章
    - 明暗主题自动切换
    """

    # 尺寸常量
    CARD_WIDTH = 900
    PADDING = 32
    SECTION_GAP = 18
    GRID_GAP = 12
    CORNER_RADIUS = 24

    # 图片限制
    MAX_IMAGES = 9
    MAX_IMAGE_HEIGHT = 900
    IMAGE_CORNER_RADIUS = 12

    # 渐变条高度
    GRADIENT_HEIGHT = 0

    def __init__(self, theme: CardTheme, font_path: Path | None = None):
        """初始化渲染器

        Args:
            theme: 卡片主题配置
            font_path: 自定义字体路径，None 则自动查找
        """
        self.theme = theme
        self.font_path = font_path or find_default_font()

        if not self.font_path:
            try:
                from astrbot.api import logger

                logger.warning("⚠️ 未找到中文字体，预览图可能出现乱码")
            except ImportError:
                pass

        # 加载字体
        self.title_font = load_font(self.font_path, 32)
        self.text_font = load_font(self.font_path, 24)
        self.meta_font = load_font(self.font_path, 20)
        self.stats_font = load_font(self.font_path, 18)

    def render(self, data: CardData) -> Image.Image:
        """渲染卡片

        Args:
            data: 卡片数据

        Returns:
            渲染完成的 PIL Image
        """
        content_width = self.CARD_WIDTH - self.PADDING * 2

        # 文本换行
        title_lines = wrap_text(data.title or "", self.title_font, content_width)
        text_lines = wrap_text(data.text or "", self.text_font, content_width)

        # 准备图片
        images = list(data.image_paths) if data.image_paths else []
        if not images and data.cover_path:
            images = [data.cover_path]
        grid = self._prepare_images(images, content_width)

        # 计算各部分高度
        gradient_height = self.GRADIENT_HEIGHT
        meta_height = get_line_height(self.meta_font)
        title_height = len(title_lines) * get_line_height(self.title_font) if title_lines else 0
        text_height = len(text_lines) * get_line_height(self.text_font) if text_lines else 0
        grid_height = grid.height if grid else 0
        stats_height = get_line_height(self.stats_font) if data.stats else 0

        # 计算卡片总高度
        card_height = gradient_height + self.PADDING + meta_height
        if title_lines:
            card_height += self.SECTION_GAP + title_height
        if text_lines:
            card_height += self.SECTION_GAP + text_height
        if grid:
            card_height += self.SECTION_GAP + grid_height
        if data.stats:
            card_height += self.SECTION_GAP + stats_height
        card_height += self.PADDING

        # 创建卡片主体
        card = create_rounded_rectangle(
            self.CARD_WIDTH,
            card_height,
            self.CORNER_RADIUS,
            self.theme.bg_color,
        )

        # 绘制渐变色顶部条
        # self._draw_gradient_bar(card) 

        draw = ImageDraw.Draw(card)

        # 绘制内容
        y = gradient_height + self.PADDING
        self._draw_meta(draw, y, data.author)
        y += meta_height

        if title_lines:
            y += self.SECTION_GAP
            y = self._draw_lines(draw, (self.PADDING, y), title_lines, self.title_font, self.theme.text_color)

        if text_lines:
            y += self.SECTION_GAP
            y = self._draw_lines(draw, (self.PADDING, y), text_lines, self.text_font, self.theme.text_color)

        if grid:
            y += self.SECTION_GAP
            self._draw_grid(card, grid, y, is_video=data.is_video)
            y += grid_height

        if data.stats:
            y += self.SECTION_GAP
            draw_stat_badges(
                draw,
                y,
                data.stats,
                self.stats_font,
                self.PADDING,
                self.theme.meta_color,
            )

        # 添加阴影
        # 根据主题选择背景色
        canvas_bg = (40, 40, 40) if self.theme.is_dark else (245, 245, 245)
        final_image = add_shadow(
            card,
            shadow_color=self.theme.shadow_color,
            shadow_offset=self.theme.shadow_offset,
            shadow_blur=self.theme.shadow_blur,
            corner_radius=self.CORNER_RADIUS,
            bg_color=canvas_bg,
        )

        return final_image

    def _draw_gradient_bar(self, card: Image.Image) -> None:
        """绘制顶部渐变色条"""
        if self.GRADIENT_HEIGHT <= 0:
            return
        gradient = create_gradient_bar(
            self.CARD_WIDTH,
            self.GRADIENT_HEIGHT,
            self.theme.accent_color,
            direction="down",
        )
        # 需要圆角遮罩，只显示顶部圆角区域
        # 简化实现：直接粘贴（圆角效果由卡片本身保证）
        card.paste(gradient, (0, 0), gradient)

    def _draw_meta(self, draw: ImageDraw.ImageDraw, y: int, author: str | None) -> None:
        """绘制元信息（平台标识 + 作者）"""
        label = self.theme.name
        draw.text((self.PADDING, y), label, fill=self.theme.accent_color, font=self.meta_font)

        if author:
            label_width = get_text_width(self.meta_font, label)
            draw.text(
                (self.PADDING + label_width + 12, y),
                f"· {author}",
                fill=self.theme.meta_color,
                font=self.meta_font,
            )

    def _draw_lines(
        self,
        draw: ImageDraw.ImageDraw,
        pos: tuple[int, int],
        lines: list[str],
        font,
        fill: tuple[int, int, int],
    ) -> int:
        """绘制多行文本，返回结束 y 坐标"""
        x, y = pos
        line_height = get_line_height(font)
        for line in lines:
            draw.text((x, y), line, fill=fill, font=font)
            y += line_height
        return y

    def _prepare_images(self, paths: list[Path], content_width: int) -> ImageGrid | None:
        """准备图片网格"""
        if not paths:
            return None

        images: list[Image.Image] = []
        for path in paths[: self.MAX_IMAGES]:
            try:
                with Image.open(path) as img:
                    images.append(img.convert("RGB"))
            except Exception:
                continue

        if not images:
            return None

        count = len(images)

        # 单图特殊处理
        if count == 1:
            img = images[0]
            max_height = min(self.MAX_IMAGE_HEIGHT, content_width)
            img = fit_image(img, content_width, max_height)
            img = add_rounded_corners(img, self.IMAGE_CORNER_RADIUS)
            return ImageGrid(
                images=[img],
                cols=1,
                rows=1,
                tile_size=img.width,
                height=img.height,
            )

        # 多图网格
        cols = 2 if count in (2, 4) else 3
        rows = (count + cols - 1) // cols
        tile_size = (content_width - (cols + 1) * self.GRID_GAP) // cols

        processed: list[Image.Image] = []
        for img in images:
            img = crop_to_square(img)
            img = img.resize((tile_size, tile_size), Image.Resampling.LANCZOS)
            img = add_rounded_corners(img, self.IMAGE_CORNER_RADIUS)
            processed.append(img)

        height = rows * tile_size + (rows + 1) * self.GRID_GAP
        return ImageGrid(
            images=processed,
            cols=cols,
            rows=rows,
            tile_size=tile_size,
            height=height,
        )

    def _draw_grid(self, base: Image.Image, grid: ImageGrid, y: int, is_video: bool) -> None:
        """绘制图片网格"""
        x_start = self.PADDING

        # 单图
        if grid.cols == 1 and grid.rows == 1:
            img = grid.images[0]
            base.paste(img, (x_start, y))
            if is_video:
                draw_play_icon(base, x_start, y, img.width, img.height)
            return

        # 多图网格
        img_index = 0
        for row in range(grid.rows):
            for col in range(grid.cols):
                if img_index >= len(grid.images):
                    break
                img = grid.images[img_index]
                x = x_start + self.GRID_GAP + col * (grid.tile_size + self.GRID_GAP)
                y_pos = y + self.GRID_GAP + row * (grid.tile_size + self.GRID_GAP)
                base.paste(img, (x, y_pos))
                if is_video and img_index == 0:
                    draw_play_icon(base, x, y_pos, grid.tile_size, grid.tile_size)
                img_index += 1


# endregion


# region 导出
__all__ = [
    "CardData",
    "ImageGrid",
    "UniversalCardRenderer",
]
# endregion
