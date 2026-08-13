import sys
import types
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

try:
    import astrbot.api  # noqa: F401
except ModuleNotFoundError:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = types.SimpleNamespace()
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

from astrbot_plugin_link_resolver.core.douyin import DouyinExtractor
from astrbot_plugin_link_resolver.core.douyin.guest_api import (
    DouyinGuestAPI,
    GuestSession,
)


@pytest.mark.asyncio
async def test_guest_api_refreshes_session_after_empty_response(monkeypatch):
    api = DouyinGuestAPI()
    sessions = iter([GuestSession("old", "old-fp"), GuestSession("new", "new-fp")])
    created = []

    async def fake_create_session():
        session = next(sessions)
        created.append(session)
        return session

    async def fake_build_endpoint(_aweme_id):
        return "https://example.test/detail"

    responses = iter(
        [
            httpx.Response(200, content=b""),
            httpx.Response(200, json={"aweme_detail": {"aweme_id": "123"}}),
        ]
    )

    async def fake_get(self, endpoint):
        return next(responses)

    monkeypatch.setattr(api, "_create_session", fake_create_session)
    monkeypatch.setattr(api, "_build_endpoint", fake_build_endpoint)
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)

    detail = await api.fetch_detail("123")

    assert detail["aweme_id"] == "123"
    assert [session.ttwid for session in created] == ["old", "new"]


def test_guest_cookie_contains_only_guest_identifiers():
    session = GuestSession("guest-ttwid", "verify_guest")
    assert session.cookie == "ttwid=guest-ttwid; s_v_web_id=verify_guest;"


def test_video_url_prefers_douyin_play_endpoint():
    urls = [
        "https://v26-web.douyinvod.com/video/first",
        "https://v11-weba.douyinvod.com/video/second",
        "https://www.douyin.com/aweme/v1/play/?video_id=stable",
    ]

    assert DouyinExtractor._pick_video_url(urls) == urls[2]


def test_video_url_preserves_all_candidates_in_fallback_order():
    urls = [
        "https://v26-web.douyinvod.com/video/first",
        "https://www.douyin.com/aweme/v1/playwm/?video_id=stable",
        "https://v11-weba.douyinvod.com/video/second",
    ]

    assert DouyinExtractor._order_video_urls(urls) == [
        "https://www.douyin.com/aweme/v1/play/?video_id=stable",
        urls[0],
        urls[2],
    ]
