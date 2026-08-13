import json
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
from astrbot_plugin_link_resolver.core.douyin.errors import DouyinParseError
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


@pytest.mark.asyncio
async def test_guest_api_wraps_network_error_for_share_page_fallback(monkeypatch):
    api = DouyinGuestAPI()
    attempts = []

    async def fail_get_session(*, refresh=False):
        attempts.append(refresh)
        raise httpx.ConnectError("guest API unavailable")

    monkeypatch.setattr(api, "_get_session", fail_get_session)

    with pytest.raises(DouyinParseError) as captured:
        await api.fetch_detail("123")

    assert attempts == [False, True]
    assert isinstance(captured.value.__cause__, httpx.ConnectError)
    assert "network error" in str(captured.value)


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


def test_video_url_selects_highest_resolution_then_keeps_lower_quality_fallbacks():
    video = {
        "bit_rate": [
            {
                "bit_rate": 800_000,
                "play_addr": {
                    "width": 720,
                    "height": 1280,
                    "url_list": ["https://example.com/720p"],
                },
            },
            {"bit_rate": None, "play_addr": None},
            {
                "bit_rate": 2_000_000,
                "play_addr": {
                    "width": 1080,
                    "height": 1920,
                    "url_list": ["https://example.com/1080p"],
                },
            },
            {
                "bit_rate": 1_500_000,
                "play_addr": {
                    "width": 2160,
                    "height": 3840,
                    "url_list": ["https://example.com/4k"],
                },
            },
        ]
    }

    urls = DouyinExtractor._select_highest_quality_video_urls(
        video,
        {"url_list": ["https://example.com/default"]},
    )

    assert urls == [
        "https://example.com/4k",
        "https://example.com/1080p",
        "https://example.com/720p",
        "https://example.com/default",
    ]
    assert DouyinExtractor._find_selected_video_quality(video, urls[0]) == {
        "width": 2160,
        "height": 3840,
        "bit_rate": 1_500_000,
        "codec": "未知",
        "gear_name": None,
        "candidate_count": 1,
    }


@pytest.mark.asyncio
async def test_share_live_photo_does_not_expose_background_audio_as_video(monkeypatch):
    extractor = DouyinExtractor()
    router_data = {
        "loaderData": {
            "note_(id)/page": {
                "videoInfoRes": {
                    "item_list": [
                        {
                            "aweme_id": "123",
                            "create_time": 0,
                            "author": {"nickname": "作者"},
                            "desc": "动图",
                            "video": {
                                "play_addr": {
                                    "url_list": ["https://example.com/music.mp3"]
                                },
                                "cover": {"url_list": []},
                                "duration": 10,
                            },
                            "images": [
                                {
                                    "url_list": [
                                        "https://example.com/image.jpg",
                                        "https://backup.example.com/image.jpg",
                                    ],
                                    "video": {
                                        "play_addr": {
                                            "url_list": [
                                                "https://example.com/live.mp4",
                                                "https://backup.example.com/live.mp4",
                                            ]
                                        },
                                        "cover": {"url_list": []},
                                        "duration": 1,
                                    },
                                }
                            ],
                        }
                    ]
                }
            }
        }
    }
    html = f"<script>window._ROUTER_DATA = {json.dumps(router_data)}</script>"

    async def fake_fetch_html(*_args, **_kwargs):
        return httpx.Response(200, text=html)

    monkeypatch.setattr(extractor, "_fetch_html", fake_fetch_html)

    result = await extractor.parse_video(
        "https://www.iesdouyin.com/share/note/123",
        "https://www.douyin.com/note/123",
    )

    assert result.video_url is None
    assert result.video_urls == []
    assert result.image_url_candidates == [
        [
            "https://example.com/image.jpg",
            "https://backup.example.com/image.jpg",
        ]
    ]
    assert result.dynamic_url_candidates == [
        [
            "https://example.com/live.mp4",
            "https://backup.example.com/live.mp4",
        ]
    ]


@pytest.mark.asyncio
async def test_normal_video_prefers_iteminfo_quality_ladder(monkeypatch):
    extractor = DouyinExtractor()
    expected = object()
    calls = []

    async def fake_parse_iteminfo(video_id, source_url):
        calls.append((video_id, source_url))
        return expected

    async def fail_parse_video(*_args):
        raise AssertionError("详情 API 成功时不应退回分享页")

    monkeypatch.setattr(extractor, "parse_iteminfo", fake_parse_iteminfo)
    monkeypatch.setattr(extractor, "parse_video", fail_parse_video)

    result = await extractor.parse("https://www.douyin.com/video/123456")

    assert result is expected
    assert calls == [("123456", "https://www.douyin.com/video/123456")]


@pytest.mark.asyncio
async def test_normal_video_falls_back_to_share_page_after_guest_network_error(
    monkeypatch,
):
    extractor = DouyinExtractor()
    expected = object()
    share_urls = []

    async def fail_iteminfo(*_args):
        raise DouyinParseError("signed guest detail failed: network error")

    async def fake_parse_video(url, _source_url):
        share_urls.append(url)
        return expected

    monkeypatch.setattr(extractor, "parse_iteminfo", fail_iteminfo)
    monkeypatch.setattr(extractor, "parse_video", fake_parse_video)

    result = await extractor.parse("https://www.douyin.com/video/123456")

    assert result is expected
    assert share_urls == ["https://m.douyin.com/share/video/123456"]
