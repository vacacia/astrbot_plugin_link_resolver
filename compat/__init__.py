from __future__ import annotations

from .bootstrap import install_astrbot_compat
from .optional_deps import ensure_optional_dependency_stubs
from .runtime_adapter import CompatEvent, DeliverySink
from .runtime_state import get_runtime_state, set_runtime_state

__all__ = [
    "CompatEvent",
    "DeliverySink",
    "ensure_optional_dependency_stubs",
    "get_runtime_state",
    "install_astrbot_compat",
    "set_runtime_state",
]
