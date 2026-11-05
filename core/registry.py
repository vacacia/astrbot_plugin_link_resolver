"""平台注册表: 所有受支持平台的唯一描述处.

新增平台时, 在 ``PLATFORMS`` 追加一条 ``Platform``, 以下行为会自动覆盖:
- ``enable_platforms`` 开关到 ``<key>_enabled`` 属性的映射
- JSON 卡片里的链接分类与分发顺序
- 插件重载时对旧解析任务的识别与取消
- 启动日志里的启用平台列表

仍需手写的部分(框架限制, 装饰器必须静态声明):
- main.py 中对应的 ``@filter.regex`` 消息处理器
- ``_conf_schema.json`` 里 ``enable_platforms`` 的可选项
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .bilibili import BILI_MESSAGE_PATTERN
from .douyin import DOUYIN_MESSAGE_PATTERN
from .generic import GENERIC_URL_PATTERN
from .twitter import TWITTER_MESSAGE_PATTERN
from .weibo import WEIBO_MESSAGE_PATTERN
from .xiaohongshu import XHS_MESSAGE_PATTERN
from .youtube import YOUTUBE_MESSAGE_PATTERN


# region 数据结构
@dataclass(frozen=True)
class Platform:
    key: str  # 内部标识, 同时决定 ``<key>_enabled`` 属性名
    label: str  # 配置面板 enable_platforms 里显示的名字
    pattern: str  # 识别该平台链接的正则(与 filter.regex 同一份)
    handle_name: str  # 消息处理入口的方法名(用于识别旧任务)
    process_name: str  # 解析主流程的方法名(用于识别旧任务)
    default_enabled: bool = True  # 配置缺失时是否默认启用(通用兜底为 False, 需手动开启)

    @property
    def enabled_attr(self) -> str:
        return f"{self.key}_enabled"


# endregion

# region 注册表
# 顺序即 JSON 卡片的分发优先级: 第一个启用且命中链接的平台胜出.
PLATFORMS: tuple[Platform, ...] = (
    Platform(
        "bili", "B站", BILI_MESSAGE_PATTERN, "handle_bili_video", "_process_bili_video"
    ),
    Platform(
        "douyin", "抖音", DOUYIN_MESSAGE_PATTERN, "handle_douyin", "_process_douyin"
    ),
    Platform("xhs", "小红书", XHS_MESSAGE_PATTERN, "handle_xhs", "_process_xhs"),
    Platform("weibo", "微博", WEIBO_MESSAGE_PATTERN, "handle_weibo", "_process_weibo"),
    Platform(
        "twitter", "X", TWITTER_MESSAGE_PATTERN, "handle_twitter", "_process_twitter"
    ),
    Platform(
        "youtube",
        "YouTube",
        YOUTUBE_MESSAGE_PATTERN,
        "handle_youtube",
        "_process_youtube",
    ),
    # 通用兜底放最后: 它只接管白名单域名, 且永不接管上面已有专用解析器的平台
    Platform(
        "generic",
        "通用站点",
        GENERIC_URL_PATTERN,
        "handle_generic",
        "_process_generic",
        default_enabled=False,
    ),
)

PLATFORM_LABELS: tuple[str, ...] = tuple(p.label for p in PLATFORMS)
PLATFORM_DEFAULT_LABELS: tuple[str, ...] = tuple(
    p.label for p in PLATFORMS if p.default_enabled
)
PLATFORM_BY_KEY: dict[str, Platform] = {p.key: p for p in PLATFORMS}

# 协程 __qualname__ 中出现这些片段即视为解析任务(插件重载时用于取消旧任务).
TASK_QUALNAME_TOKENS: tuple[str, ...] = tuple(
    name for p in PLATFORMS for name in (p.handle_name, p.process_name)
)
# endregion


# region 工具
def classify_links(
    links: list[str],
    link_filters: Mapping[str, Callable[[str], bool]] | None = None,
) -> dict[str, list[str]]:
    """把链接按平台分组, 只保留命中的平台, 组内保持输入顺序.

    link_filters: 平台 key -> 额外校验函数(如通用站点的域名白名单, 它的 pattern 过宽).
    """
    link_filters = link_filters or {}
    grouped: dict[str, list[str]] = {}
    for platform in PLATFORMS:
        extra = link_filters.get(platform.key)
        matched = [
            link
            for link in links
            if re.search(platform.pattern, link) and (extra is None or extra(link))
        ]
        if matched:
            grouped[platform.key] = matched
    return grouped


# endregion
