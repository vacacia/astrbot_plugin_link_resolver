from __future__ import annotations

from typing import Any, Callable


AstrMessageEvent = object


class MessageChain:
    def __init__(self, chain: list[Any]) -> None:
        self.chain = list(chain)


class filter:  # noqa: N801
    @staticmethod
    def regex(_pattern: str, priority: int = 100) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        _ = priority

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            return func

        return decorator
