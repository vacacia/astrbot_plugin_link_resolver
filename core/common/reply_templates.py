"""超限回复模板。

时长超限和体积超限原先把句子写在各个 handler 里。这里收成同一套渲染：
配置给出模板，调用点只提供时长或体积数字。默认句与改配置之前一致。
模板留空表示只记日志、不向会话发送。
"""

from __future__ import annotations

DEFAULT_DURATION_LIMIT_REPLY = "视频太长了你自己看去"
DEFAULT_SIZE_LIMIT_REPLY = "我没流量了, 看不了那么大的视频"


class _KeepUnknown(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def ceil_minutes(seconds: float) -> int:
    return max(1, int(-(-(float(seconds)) // 60)))


def floor_minutes(seconds: float) -> int:
    return max(1, int(float(seconds)) // 60)


def render_reply_template(
    template: str | None,
    fallback: str,
    **values: object,
) -> str | None:
    """渲染回复。None 用 fallback；去掉空白后为空则不发送。"""
    if template is None:
        text = fallback
    else:
        text = str(template)
        if not text.strip():
            return None
    try:
        return text.format_map(_KeepUnknown(values))
    except (IndexError, ValueError):
        return text


def duration_limit_reply(
    owner: object,
    duration_seconds: float,
    limit_seconds: float,
) -> str | None:
    return render_reply_template(
        getattr(owner, "duration_limit_reply", None),
        DEFAULT_DURATION_LIMIT_REPLY,
        duration_seconds=int(float(duration_seconds)),
        limit_seconds=int(float(limit_seconds)),
        video_minutes=ceil_minutes(duration_seconds),
        limit_minutes=floor_minutes(limit_seconds),
    )


def size_limit_reply(owner: object) -> str | None:
    limit_mb = int(getattr(owner, "max_video_size_mb", 0) or 0)
    return render_reply_template(
        getattr(owner, "size_limit_reply", None),
        DEFAULT_SIZE_LIMIT_REPLY,
        limit_mb=limit_mb,
    )
