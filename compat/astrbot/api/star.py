from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ...runtime_state import get_runtime_state


class Context:
    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = dict(config or {})

    def get_config(self) -> dict[str, Any]:
        return dict(self._config)


class Star:
    def __init__(self, context: Context | None = None) -> None:
        self.context = context


class StarTools:
    @staticmethod
    def get_data_dir(_plugin_name: str) -> Path:
        return get_runtime_state().data_dir


def register(*_args: Any, **_kwargs: Any) -> Callable[[type], type]:
    def decorator(cls: type) -> type:
        return cls

    return decorator
