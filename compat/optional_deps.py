from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
import types
from enum import Enum
from typing import Any, Union, get_args, get_origin


class MissingOptionalDependencyError(RuntimeError):
    pass


def _missing(package: str):
    def _raise(*args: Any, **kwargs: Any):
        raise MissingOptionalDependencyError(
            f"link_resolver optional dependency '{package}' is not installed"
        )

    return _raise


class _AsyncContextManagerStub:
    def __init__(self, package: str, *args: Any, **kwargs: Any) -> None:
        self._package = package

    async def __aenter__(self):
        raise MissingOptionalDependencyError(
            f"link_resolver optional dependency '{self._package}' is not installed"
        )

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _HttpxResponse:
    status_code = 599
    headers: dict[str, str] = {}
    content: bytes = b""
    url = ""


class _BilibiliVideoQuality(Enum):
    _240P = 240
    _360P = 360
    _480P = 480
    _720P = 720
    _720P_60 = 721
    _1080P = 1080
    _1080P_60 = 1081
    _1080P_PLUS = 1082
    HDR = 1090
    DOLBY = 1091
    _4K = 4000
    _8K = 8000


class _BilibiliVideoCodecs(Enum):
    AVC = "AVC"
    HEVC = "HEVC"
    AV1 = "AV1"


class _BilibiliPlaceholder:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._args = args
        self._kwargs = kwargs

    def __getattr__(self, name: str):
        return _missing("bilibili-api-python")


def ensure_optional_dependency_stubs() -> None:
    _ensure_httpx_stub()
    _ensure_aiohttp_stub()
    _ensure_msgspec_stub()
    _ensure_pillow_stub()
    _ensure_bilibili_stub()


def _has_real_module(name: str) -> bool:
    if name in sys.modules:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _ensure_httpx_stub() -> None:
    if _has_real_module("httpx"):
        return
    module = types.ModuleType("httpx")
    module.AsyncClient = lambda *args, **kwargs: _AsyncContextManagerStub("httpx", *args, **kwargs)
    module.Response = _HttpxResponse
    module.ReadTimeout = MissingOptionalDependencyError
    module.ConnectError = MissingOptionalDependencyError
    sys.modules["httpx"] = module


def _ensure_aiohttp_stub() -> None:
    if _has_real_module("aiohttp"):
        return
    module = types.ModuleType("aiohttp")
    module.ClientSession = lambda *args, **kwargs: _AsyncContextManagerStub("aiohttp", *args, **kwargs)
    module.ClientTimeout = lambda *args, **kwargs: types.SimpleNamespace(args=args, kwargs=kwargs)
    module.ClientError = MissingOptionalDependencyError
    sys.modules["aiohttp"] = module


def _decode_msgspec_fallback(raw: Any, target_type: Any = None, **kwargs: Any) -> Any:
    if target_type is None:
        target_type = kwargs.get("type")
    if target_type is None:
        raise TypeError("_decode_msgspec_fallback requires target_type or type=")
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    parsed = json.loads(raw)
    return _coerce_msgspec_value(parsed, target_type)



def _coerce_msgspec_value(value: Any, target_type: Any) -> Any:
    if target_type in {Any, object}:
        return value
    if value is None:
        return None

    origin = get_origin(target_type)
    if origin in {list, tuple, set}:
        inner_type = get_args(target_type)[0] if get_args(target_type) else Any
        return [_coerce_msgspec_value(item, inner_type) for item in list(value)]
    if origin is dict:
        value_type = get_args(target_type)[1] if len(get_args(target_type)) >= 2 else Any
        return {
            key: _coerce_msgspec_value(item, value_type)
            for key, item in dict(value).items()
        }
    if origin in {Union, types.UnionType}:
        last_error: Exception | None = None
        for member in get_args(target_type):
            if member is type(None):
                if value is None:
                    return None
                continue
            try:
                return _coerce_msgspec_value(value, member)
            except Exception as exc:  # pragma: no cover - 调试兜底
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        return value

    if isinstance(target_type, type):
        if issubclass(target_type, Enum):
            return target_type(value)
        if target_type in {str, int, float, bool}:
            return target_type(value)
        if _looks_like_msgspec_struct(target_type):
            return _instantiate_msgspec_struct(target_type, value)

    return value



def _looks_like_msgspec_struct(target_type: type) -> bool:
    return bool(getattr(target_type, "__annotations__", {}))



def _instantiate_msgspec_struct(target_type: type, payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    annotations = dict(getattr(target_type, "__annotations__", {}) or {})
    prepared_values: dict[str, Any] = {}
    for attr_name, attr_type in annotations.items():
        source_name = _resolve_msgspec_field_name(target_type, attr_name)
        if source_name in payload:
            raw_value = payload[source_name]
        elif attr_name in payload:
            raw_value = payload[attr_name]
        else:
            raw_value = _default_msgspec_field_value(target_type, attr_name)
        prepared_values[attr_name] = _coerce_msgspec_value(raw_value, attr_type)

    try:
        return target_type(**prepared_values)
    except Exception:
        instance = object.__new__(target_type)
        for attr_name, value in prepared_values.items():
            setattr(instance, attr_name, value)
        return instance



def _resolve_msgspec_field_name(target_type: type, attr_name: str) -> str:
    struct_fields = tuple(getattr(target_type, "__struct_fields__", ()) or ())
    encode_fields = tuple(getattr(target_type, "__struct_encode_fields__", ()) or ())
    if struct_fields and encode_fields and len(struct_fields) == len(encode_fields):
        mapping = dict(zip(struct_fields, encode_fields, strict=False))
        if attr_name in mapping:
            return str(mapping[attr_name] or attr_name)

    field_info = target_type.__dict__.get(attr_name)
    if isinstance(field_info, dataclasses.Field):
        return str(field_info.metadata.get("msgspec_name", attr_name) or attr_name)
    return attr_name



def _default_msgspec_field_value(target_type: type, attr_name: str) -> Any:
    struct_fields = tuple(getattr(target_type, "__struct_fields__", ()) or ())
    struct_defaults = tuple(getattr(target_type, "__struct_defaults__", ()) or ())
    if struct_fields and struct_defaults and len(struct_fields) == len(struct_defaults):
        defaults = dict(zip(struct_fields, struct_defaults, strict=False))
        if attr_name in defaults:
            default_value = defaults[attr_name]
            if getattr(default_value, "__repr__", lambda: "")() == "<factory>":
                return None
            return default_value

    field_info = target_type.__dict__.get(attr_name)
    if isinstance(field_info, dataclasses.Field):
        if field_info.default_factory is not dataclasses.MISSING:
            return field_info.default_factory()
        if field_info.default is not dataclasses.MISSING:
            return field_info.default
    return None



def _ensure_msgspec_stub() -> None:
    if _has_real_module("msgspec"):
        return

    class Struct:
        def __init_subclass__(cls, *args: Any, **kwargs: Any) -> None:
            return None

    def _field(
        *args: Any,
        default: Any = dataclasses.MISSING,
        default_factory: Any = dataclasses.MISSING,
        **kwargs: Any,
    ):
        metadata = {"msgspec_name": kwargs.get("name")}
        if default_factory is not dataclasses.MISSING:
            return dataclasses.field(default_factory=default_factory, metadata=metadata)
        if default is not dataclasses.MISSING:
            return dataclasses.field(default=default, metadata=metadata)
        return dataclasses.field(metadata=metadata)

    module = types.ModuleType("msgspec")
    module.Struct = Struct
    module.field = _field
    module.json = types.SimpleNamespace(decode=_decode_msgspec_fallback)
    sys.modules["msgspec"] = module


def _ensure_pillow_stub() -> None:
    if _has_real_module("PIL"):
        return

    pil_pkg = types.ModuleType("PIL")
    image_mod = types.ModuleType("PIL.Image")
    image_draw_mod = types.ModuleType("PIL.ImageDraw")
    image_font_mod = types.ModuleType("PIL.ImageFont")
    image_filter_mod = types.ModuleType("PIL.ImageFilter")

    class DummyImage:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            _missing("Pillow")()

    image_mod.Image = DummyImage
    image_mod.open = _missing("Pillow")
    image_mod.new = _missing("Pillow")
    image_draw_mod.Draw = _missing("Pillow")
    image_draw_mod.ImageDraw = object
    image_font_mod.truetype = _missing("Pillow")
    image_font_mod.load_default = _missing("Pillow")
    image_font_mod.FreeTypeFont = object
    image_font_mod.ImageFont = object
    image_filter_mod.GaussianBlur = object

    pil_pkg.Image = image_mod
    pil_pkg.ImageDraw = image_draw_mod
    pil_pkg.ImageFont = image_font_mod
    pil_pkg.ImageFilter = image_filter_mod

    sys.modules["PIL"] = pil_pkg
    sys.modules["PIL.Image"] = image_mod
    sys.modules["PIL.ImageDraw"] = image_draw_mod
    sys.modules["PIL.ImageFont"] = image_font_mod
    sys.modules["PIL.ImageFilter"] = image_filter_mod


def _ensure_bilibili_stub() -> None:
    if _has_real_module("bilibili_api"):
        return

    bilibili_api_mod = types.ModuleType("bilibili_api")
    bilibili_video_mod = types.ModuleType("bilibili_api.video")

    class Credential(_BilibiliPlaceholder):
        pass

    class Video(_BilibiliPlaceholder):
        pass

    class VideoDownloadURLDataDetecter(_BilibiliPlaceholder):
        pass

    class VideoStreamDownloadURL(_BilibiliPlaceholder):
        pass

    class AudioStreamDownloadURL(_BilibiliPlaceholder):
        pass

    bilibili_video_mod.Video = Video
    bilibili_video_mod.VideoQuality = _BilibiliVideoQuality
    bilibili_video_mod.VideoCodecs = _BilibiliVideoCodecs
    bilibili_video_mod.VideoDownloadURLDataDetecter = VideoDownloadURLDataDetecter
    bilibili_video_mod.VideoStreamDownloadURL = VideoStreamDownloadURL
    bilibili_video_mod.AudioStreamDownloadURL = AudioStreamDownloadURL

    bilibili_api_mod.Credential = Credential
    bilibili_api_mod.video = bilibili_video_mod

    sys.modules["bilibili_api"] = bilibili_api_mod
    sys.modules["bilibili_api.video"] = bilibili_video_mod
