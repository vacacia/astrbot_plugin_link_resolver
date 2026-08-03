from __future__ import annotations

import sys
import types

from .astrbot import api as compat_api
from .astrbot.api import event as compat_event
from .astrbot.api import message_components as compat_components
from .astrbot.api import star as compat_star


def install_astrbot_compat() -> None:
    astrbot_pkg = sys.modules.get("astrbot")
    if astrbot_pkg is None or getattr(astrbot_pkg, "__name__", "") != "astrbot":
        astrbot_pkg = types.ModuleType("astrbot")
        astrbot_pkg.__path__ = []  # type: ignore[attr-defined]
        sys.modules["astrbot"] = astrbot_pkg
    setattr(astrbot_pkg, "api", compat_api)
    sys.modules["astrbot.api"] = compat_api
    sys.modules["astrbot.api.event"] = compat_event
    sys.modules["astrbot.api.message_components"] = compat_components
    sys.modules["astrbot.api.star"] = compat_star
