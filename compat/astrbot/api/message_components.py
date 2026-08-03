from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .event import MessageChain


@dataclass
class BaseMessageComponent:
    type: str

    def toDict(self) -> dict[str, Any]:
        return self.to_dict_sync()

    async def to_dict(self) -> dict[str, Any]:
        return self.to_dict_sync()

    def to_dict_sync(self) -> dict[str, Any]:
        raise NotImplementedError


@dataclass
class Plain(BaseMessageComponent):
    text: str = ""

    def __init__(self, text: str, **_: Any) -> None:
        super().__init__(type="text")
        self.text = text

    def to_dict_sync(self) -> dict[str, Any]:
        return {"type": "text", "data": {"text": self.text}}


Text = Plain


@dataclass
class _FileLikeComponent(BaseMessageComponent):
    file: str = ""
    name: str | None = None
    url: str = ""
    path: str = ""

    async def register_to_file_service(self) -> str:
        file_ref = str(self.file or self.url or self.path or "").strip()
        if file_ref.startswith(("http://", "https://", "file://", "base64://", "data:")):
            return file_ref
        return Path(file_ref).expanduser().resolve(strict=False).as_uri()


@dataclass
class Image(_FileLikeComponent):
    def __init__(self, file: str = "", **kwargs: Any) -> None:
        super().__init__(type="image", file=file, path=file, **kwargs)

    @classmethod
    def fromFileSystem(cls, path: str) -> "Image":
        return cls(file=path)

    def to_dict_sync(self) -> dict[str, Any]:
        return {"type": "image", "data": {"file": self.file or self.path}}


@dataclass
class Video(_FileLikeComponent):
    cover: str = ""
    c: int = 2

    def __init__(self, file: str = "", cover: str = "", c: int = 2, **kwargs: Any) -> None:
        super().__init__(type="video", file=file, path=file, **kwargs)
        self.cover = cover
        self.c = c

    @classmethod
    def fromFileSystem(cls, path: str) -> "Video":
        return cls(file=path)

    @classmethod
    def fromURL(cls, url: str, cover: str = "", c: int = 2) -> "Video":
        return cls(file=url, url=url, cover=cover, c=c)

    def to_dict_sync(self) -> dict[str, Any]:
        data: dict[str, Any] = {"file": self.file or self.url or self.path}
        if self.cover:
            data["cover"] = self.cover
        if self.c:
            data["c"] = self.c
        return {"type": "video", "data": data}


@dataclass
class File(_FileLikeComponent):
    def __init__(self, file: str = "", name: str | None = None, **kwargs: Any) -> None:
        super().__init__(type="file", file=file, path=file, name=name, **kwargs)
        self.name = name

    def to_dict_sync(self) -> dict[str, Any]:
        data = {"file": self.file or self.path}
        if self.name:
            data["name"] = self.name
        return {"type": "file", "data": data}


@dataclass
class Json(BaseMessageComponent):
    data: dict[str, Any] = field(default_factory=dict)

    def __init__(self, data: str | dict[str, Any], **_: Any) -> None:
        super().__init__(type="json")
        if isinstance(data, str):
            data = json.loads(data)
        self.data = dict(data)

    def to_dict_sync(self) -> dict[str, Any]:
        return {"type": "json", "data": self.data}


@dataclass
class Node(BaseMessageComponent):
    uin: str = ""
    content: list[Any] = field(default_factory=list)
    name: str = ""

    def __init__(self, uin: str, content: list[Any], name: str = "", **_: Any) -> None:
        super().__init__(type="node")
        self.uin = str(uin)
        self.content = list(content)
        self.name = name

    async def to_dict(self) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        for component in self.content:
            if hasattr(component, "to_dict"):
                content.append(await component.to_dict())
            elif hasattr(component, "toDict"):
                content.append(component.toDict())
            else:
                raise TypeError(f"unsupported node content: {component!r}")
        return {
            "type": "node",
            "data": {
                "user_id": self.uin,
                "nickname": self.name or self.uin,
                "content": content,
            },
        }

    def to_dict_sync(self) -> dict[str, Any]:
        return {
            "type": "node",
            "data": {
                "user_id": self.uin,
                "nickname": self.name or self.uin,
                "content": [
                    component.toDict() if hasattr(component, "toDict") else component
                    for component in self.content
                ],
            },
        }


@dataclass
class Nodes(BaseMessageComponent):
    nodes: list[Node] = field(default_factory=list)

    def __init__(self, nodes: list[Node], **_: Any) -> None:
        super().__init__(type="nodes")
        self.nodes = list(nodes)

    async def to_dict(self) -> dict[str, Any]:
        return {"messages": [await node.to_dict() for node in self.nodes]}

    def to_dict_sync(self) -> dict[str, Any]:
        return {"messages": [node.toDict() for node in self.nodes]}
