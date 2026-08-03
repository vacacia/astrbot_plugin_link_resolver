from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class CompatRuntimeState:
    data_dir: Path
    gateway: Any
    plugin_config: dict[str, Any]


_state: CompatRuntimeState | None = None


def set_runtime_state(*, data_dir: Path, gateway: Any, plugin_config: dict[str, Any]) -> CompatRuntimeState:
    global _state
    _state = CompatRuntimeState(
        data_dir=data_dir,
        gateway=gateway,
        plugin_config=dict(plugin_config),
    )
    return _state


def get_runtime_state() -> CompatRuntimeState:
    if _state is None:
        raise RuntimeError("link_resolver compat runtime state is not initialized")
    return _state
