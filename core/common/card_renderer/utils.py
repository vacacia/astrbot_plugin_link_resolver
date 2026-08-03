# region 导入
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..font_manager import (
    get_managed_font_paths,
    get_user_font_paths,
    managed_fonts_enabled,
)

# endregion


# region 字体查找
def find_default_font() -> Path | None:
    """查找可用的中文字体。"""
    plugin_root = Path(__file__).resolve().parents[4]
    user_fonts = get_user_font_paths()
    managed_fonts = get_managed_font_paths()

    candidates: list[Path] = []
    if user_fonts.primary:
        candidates.append(user_fonts.primary)
    if managed_fonts_enabled() and managed_fonts.primary:
        candidates.append(managed_fonts.primary)

    parser_resources = (
        plugin_root
        / "astrbot_plugin_parser"
        / "core"
        / "resources"
        / "HYSongYunLangHeiW-1.ttf"
    )
    candidates.append(parser_resources)

    candidates.extend(
        Path(font)
        for font in (
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
        )
    )

    for candidate in candidates:
        if candidate.exists() and _font_path_loadable(candidate, 24):
            return candidate
    return None


def find_emoji_font() -> Path | None:
    """查找可用于 Emoji fallback 的字体。"""
    user_fonts = get_user_font_paths()
    managed_fonts = get_managed_font_paths()
    candidates: list[Path] = []
    if user_fonts.emoji:
        candidates.append(user_fonts.emoji)
    if managed_fonts_enabled() and managed_fonts.emoji:
        candidates.append(managed_fonts.emoji)

    candidates.extend(
        Path(font)
        for font in (
            "/usr/share/fonts/truetype/openmoji/OpenMoji-black-glyf.ttf",
            "/usr/share/fonts/truetype/noto/Noto-COLRv1.ttf",
            "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        )
    )

    for candidate in candidates:
        if candidate.exists() and _emoji_font_path_renders(candidate, 24):
            return candidate
    return None


# endregion


# region 字体加载
def load_font(
    font_path: Path | None, size: int
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """加载字体，如果路径不存在则使用默认字体。"""
    font = load_optional_font(font_path, size)
    if font is not None:
        return font
    return ImageFont.load_default()


def load_optional_font(
    font_path: Path | None, size: int
) -> ImageFont.FreeTypeFont | None:
    """尝试加载字体，失败时返回 None。"""
    if not font_path or not font_path.exists():
        return None
    try:
        return ImageFont.truetype(str(font_path), size)
    except Exception:
        return None


# endregion


# region 文本工具
def get_line_height(
    font: ImageFont.ImageFont,
    emoji_font: ImageFont.ImageFont | None = None,
) -> int:
    """获取行高。"""
    heights = [_get_font_metrics_height(font)]
    if emoji_font is not None:
        heights.append(_get_font_metrics_height(emoji_font))
    return max(heights)


def get_text_width(
    font: ImageFont.ImageFont,
    text: str,
    emoji_font: ImageFont.ImageFont | None = None,
) -> int:
    """获取文本宽度。"""
    cursor = 0.0
    for cluster in _iter_text_clusters(text):
        active_font = _choose_font_for_cluster(cluster, font, emoji_font)
        cursor += float(active_font.getlength(cluster))
    return int(round(cursor))


def wrap_text(
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    emoji_font: ImageFont.ImageFont | None = None,
) -> list[str]:
    """按字体链宽度逐 cluster 自动换行。"""
    if not text:
        return []

    lines: list[str] = []
    for raw in text.splitlines():
        current = ""
        for cluster in _iter_text_clusters(raw):
            candidate = current + cluster
            if current and get_text_width(font, candidate, emoji_font) > max_width:
                lines.append(current)
                current = cluster
            else:
                current = candidate
        if current:
            lines.append(current)
    return lines


def draw_text_with_fallback(
    draw: ImageDraw.ImageDraw,
    pos: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
    emoji_font: ImageFont.ImageFont | None = None,
) -> int:
    """使用主字体 + Emoji fallback 绘制一行文本。"""
    cursor = float(pos[0])
    y = pos[1]

    run_text = ""
    run_font = font
    run_is_emoji = False

    def flush() -> None:
        nonlocal cursor, run_text, run_font, run_is_emoji
        if not run_text:
            return
        kwargs = {
            "fill": fill,
            "font": run_font,
        }
        if run_is_emoji:
            kwargs["embedded_color"] = True
        draw.text((int(round(cursor)), y), run_text, **kwargs)
        cursor += float(run_font.getlength(run_text))
        run_text = ""

    for cluster in _iter_text_clusters(text):
        active_is_emoji = bool(emoji_font and _looks_like_emoji(cluster))
        active_font = emoji_font if active_is_emoji else font
        if run_text and (
            active_font is not run_font or active_is_emoji != run_is_emoji
        ):
            flush()
        run_font = active_font
        run_is_emoji = active_is_emoji
        run_text += cluster

    flush()
    return int(round(cursor))


def _get_font_metrics_height(font: ImageFont.ImageFont) -> int:
    try:
        ascent, descent = font.getmetrics()
        return ascent + descent
    except Exception:
        bbox = font.getbbox("Ag")
        return bbox[3] - bbox[1] if bbox else 0


def _font_path_loadable(path: Path, size: int) -> bool:
    return load_optional_font(path, size) is not None


def _emoji_font_path_renders(path: Path, size: int) -> bool:
    font = load_optional_font(path, size)
    if font is None:
        return False
    return _font_renders_probe(font, embedded_color=True) or _font_renders_probe(
        font, embedded_color=False
    )


def _font_renders_probe(font: ImageFont.ImageFont, embedded_color: bool) -> bool:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    kwargs = {"font": font}
    if embedded_color:
        kwargs["embedded_color"] = True
    else:
        kwargs["fill"] = (0, 0, 0, 255)

    try:
        draw.text((0, 0), "😀", **kwargs)
    except Exception:
        return False
    return image.getbbox() is not None


def _choose_font_for_cluster(
    cluster: str,
    font: ImageFont.ImageFont,
    emoji_font: ImageFont.ImageFont | None,
) -> ImageFont.ImageFont:
    if emoji_font and _looks_like_emoji(cluster):
        return emoji_font
    return font


def _iter_text_clusters(text: str):
    i = 0
    while i < len(text):
        cluster = text[i]
        i += 1

        if _is_regional_indicator(cluster):
            if i < len(text) and _is_regional_indicator(text[i]):
                cluster += text[i]
                i += 1
            yield cluster
            continue

        while i < len(text):
            current = text[i]
            if (
                _is_variation_selector(current)
                or _is_skin_tone_modifier(current)
                or current == "\u20e3"
            ):
                cluster += current
                i += 1
                continue
            if current == "\u200d":
                cluster += current
                i += 1
                if i < len(text):
                    cluster += text[i]
                    i += 1
                continue
            break

        yield cluster


def _looks_like_emoji(cluster: str) -> bool:
    return any(
        _is_emoji_base(char)
        or _is_skin_tone_modifier(char)
        or _is_regional_indicator(char)
        or char in ("\u200d", "\u20e3", "\ufe0f")
        for char in cluster
    )


def _is_variation_selector(char: str) -> bool:
    codepoint = ord(char)
    return 0xFE00 <= codepoint <= 0xFE0F or 0xE0100 <= codepoint <= 0xE01EF


def _is_skin_tone_modifier(char: str) -> bool:
    codepoint = ord(char)
    return 0x1F3FB <= codepoint <= 0x1F3FF


def _is_regional_indicator(char: str) -> bool:
    codepoint = ord(char)
    return 0x1F1E6 <= codepoint <= 0x1F1FF


def _is_emoji_base(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x1F300 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x26FF
        or 0x2700 <= codepoint <= 0x27BF
        or codepoint
        in {
            0x00A9,
            0x00AE,
            0x203C,
            0x2049,
            0x2122,
            0x2139,
            0x3030,
            0x303D,
            0x3297,
            0x3299,
        }
    )


# endregion


# region 导出
__all__ = [
    "draw_text_with_fallback",
    "find_default_font",
    "find_emoji_font",
    "get_line_height",
    "get_text_width",
    "load_font",
    "load_optional_font",
    "wrap_text",
]
# endregion
