# region 导入
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFilter

# endregion


# region 圆角与形状
def create_rounded_rectangle(
    width: int,
    height: int,
    radius: int,
    color: tuple[int, int, int],
) -> Image.Image:
    """创建圆角矩形图像（RGBA）"""
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        [(0, 0), (width - 1, height - 1)],
        radius=radius,
        fill=(*color, 255),
    )
    return image


def add_rounded_corners(img: Image.Image, radius: int) -> Image.Image:
    """为图片添加圆角

    Returns:
        带圆角的 RGB 图像（白色背景填充）
    """
    mask = Image.new("L", img.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        [(0, 0), (img.width - 1, img.height - 1)],
        radius=radius,
        fill=255,
    )

    output = Image.new("RGBA", img.size, (255, 255, 255, 0))
    output.paste(img, (0, 0))
    output.putalpha(mask)

    # 白色背景
    final = Image.new("RGB", img.size, (255, 255, 255))
    final.paste(output, (0, 0), output)
    return final


# endregion


# region 阴影效果
def add_shadow(
    card: Image.Image,
    shadow_color: tuple[int, int, int, int] = (0, 0, 0, 40),
    shadow_offset: int = 8,
    shadow_blur: int = 20,
    corner_radius: int = 24,
    bg_color: tuple[int, int, int] = (255, 255, 255),
) -> Image.Image:
    """为卡片添加柔和阴影效果

    Args:
        card: 输入卡片图像（RGBA）
        shadow_color: 阴影颜色（RGBA）
        shadow_offset: 阴影偏移量
        shadow_blur: 阴影模糊半径
        corner_radius: 阴影圆角半径
        bg_color: 画布背景色

    Returns:
        带阴影的 RGB 图像
    """
    canvas_width = card.width + shadow_offset * 2 + shadow_blur * 2
    canvas_height = card.height + shadow_offset * 2 + shadow_blur * 2

    # 创建阴影层（使用背景色填充）
    shadow = Image.new("RGBA", (canvas_width, canvas_height), (*bg_color, 255))

    # 创建阴影形状
    shadow_shape = Image.new("RGBA", card.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_shape)
    shadow_draw.rounded_rectangle(
        [(0, 0), (card.width - 1, card.height - 1)],
        radius=corner_radius,
        fill=shadow_color,
    )

    # 放置阴影（带偏移）
    shadow_x = shadow_blur + shadow_offset
    shadow_y = shadow_blur + shadow_offset
    shadow.paste(shadow_shape, (shadow_x, shadow_y), shadow_shape)

    # 模糊阴影
    shadow = shadow.filter(ImageFilter.GaussianBlur(shadow_blur))

    # 将卡片放置在阴影上方
    card_x = shadow_blur
    card_y = shadow_blur
    shadow.paste(card, (card_x, card_y), card)

    return shadow.convert("RGB")


# endregion


# region 渐变效果
def create_gradient_bar(
    width: int,
    height: int,
    color: tuple[int, int, int],
    direction: str = "down",
) -> Image.Image:
    """创建渐变色条

    Args:
        width: 宽度
        height: 高度
        color: 起始颜色（RGB）
        direction: 渐变方向 "down"（从上往下淡出） 或 "up"（从下往上淡出）

    Returns:
        RGBA 图像，从不透明渐变到透明
    """
    gradient = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    for y in range(height):
        if direction == "down":
            alpha = int(255 * (1 - y / height))
        else:
            alpha = int(255 * (y / height))

        for x in range(width):
            gradient.putpixel((x, y), (*color, alpha))

    return gradient


def create_horizontal_gradient(
    width: int,
    height: int,
    left_color: tuple[int, int, int],
    right_color: tuple[int, int, int],
) -> Image.Image:
    """创建水平渐变背景

    Args:
        width: 宽度
        height: 高度
        left_color: 左侧颜色
        right_color: 右侧颜色

    Returns:
        RGB 渐变图像
    """
    gradient = Image.new("RGB", (width, height))

    for x in range(width):
        ratio = x / max(width - 1, 1)
        r = int(left_color[0] * (1 - ratio) + right_color[0] * ratio)
        g = int(left_color[1] * (1 - ratio) + right_color[1] * ratio)
        b = int(left_color[2] * (1 - ratio) + right_color[2] * ratio)
        for y in range(height):
            gradient.putpixel((x, y), (r, g, b))

    return gradient


# endregion


# region 毛玻璃效果
def add_frosted_glass(
    image: Image.Image,
    blur_radius: int = 30,
    overlay_color: tuple[int, int, int] = (255, 255, 255),
    overlay_alpha: int = 180,
) -> Image.Image:
    """添加毛玻璃效果

    Args:
        image: 输入图像
        blur_radius: 模糊半径
        overlay_color: 覆盖层颜色
        overlay_alpha: 覆盖层透明度 (0-255)

    Returns:
        带毛玻璃效果的 RGBA 图像
    """
    blurred = image.filter(ImageFilter.GaussianBlur(blur_radius))
    blurred_rgba = blurred.convert("RGBA")

    overlay = Image.new("RGBA", image.size, (*overlay_color, overlay_alpha))

    return Image.alpha_composite(blurred_rgba, overlay)


# endregion


# region 播放图标
def draw_play_icon(
    image: Image.Image,
    x: int,
    y: int,
    size: int,
    icon_scale: float = 0.18,
) -> None:
    """在图像上绘制播放图标

    Args:
        image: 目标图像（将被原地修改）
        x: 图片区域左上角 x 坐标
        y: 图片区域左上角 y 坐标
        size: 图片区域尺寸
        icon_scale: 图标相对于图片的缩放比例
    """
    radius = int(size * icon_scale)
    center_x = size // 2
    center_y = size // 2

    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # 半透明圆形背景
    draw.ellipse(
        (center_x - radius, center_y - radius, center_x + radius, center_y + radius),
        fill=(0, 0, 0, 120),
    )

    # 播放三角形
    triangle = [
        (center_x - radius // 3, center_y - radius // 2),
        (center_x - radius // 3, center_y + radius // 2),
        (center_x + radius // 2, center_y),
    ]
    draw.polygon(triangle, fill=(255, 255, 255, 220))

    image.paste(overlay, (x, y), overlay)


# endregion


# region 统计徽章
def draw_stat_badges(
    draw: ImageDraw.ImageDraw,
    y: int,
    stats: dict[str, str],
    font,
    x_start: int,
    color: tuple[int, int, int],
    gap: int = 24,
) -> int:
    """绘制统计徽章（播放量/点赞/评论等）

    Args:
        draw: ImageDraw 对象
        y: 绘制位置 y 坐标
        stats: 统计数据 {"👁": "12.3万", "💬": "5678", "👍": "9.8万"}
        font: 字体
        x_start: 起始 x 坐标
        color: 文字颜色
        gap: 项目间距

    Returns:
        绘制后的 x 坐标位置
    """
    x = x_start
    for icon, value in stats.items():
        text = f"{icon} {value}"
        draw.text((x, y), text, fill=color, font=font)
        x += int(font.getlength(text)) + gap
    return x


# endregion


# region 图片裁剪
def crop_to_square(img: Image.Image) -> Image.Image:
    """居中裁剪为正方形"""
    width, height = img.size
    if width == height:
        return img
    if width > height:
        left = (width - height) // 2
        return img.crop((left, 0, left + height, height))
    top = (height - width) // 2
    return img.crop((0, top, width, top + width))


def fit_image(
    img: Image.Image,
    max_width: int,
    max_height: int,
) -> Image.Image:
    """等比例缩放图片以适应最大尺寸"""
    if img.width <= max_width and img.height <= max_height:
        return img

    ratio = min(max_width / img.width, max_height / img.height)
    new_size = (int(img.width * ratio), int(img.height * ratio))
    return img.resize(new_size, Image.Resampling.LANCZOS)


# endregion


# region 导出
__all__ = [
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
]
# endregion
