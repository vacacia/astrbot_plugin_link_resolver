# ruff: noqa: E402
import json

import httpx
import pytest

from plugins.link_resolver.core.douyin import DouyinExtractor
from plugins.link_resolver.core.douyin.errors import DouyinParseError
from plugins.link_resolver.core.douyin.guest_api import (
    DouyinGuestAPI,
    GuestRequest,
    GuestSession,
)
from plugins.link_resolver.core.douyin.websign import sign


def test_websign_matches_known_sdk_fixture():
    signed = sign(
        "aweme_id=123&uifid=guest&a_bogus=A%2BB%2F%3D",
        "guest",
        timestamp=1_700_000_000,
    )

    assert signed.timestamp == "1700000000"
    assert signed.signature == "d6ffd103bb2de29134a9e15755c2d5c4"
    assert signed.query == (
        "aweme_id=123&uifid=guest&a_bogus=A%2BB%2F%3D"
        "&timestamp=1700000000"
        "&x-secsdk-web-signature=d6ffd103bb2de29134a9e15755c2d5c4"
    )


@pytest.mark.asyncio
async def test_guest_api_bootstraps_server_issued_anonymous_identity(monkeypatch):
    requested_urls = []

    class FakeClient:
        def __init__(self, **_kwargs):
            self.cookies = httpx.Cookies()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url):
            requested_urls.append(url)
            self.cookies.set("ttwid", "guest-ttwid", domain=".douyin.com")
            self.cookies.set("UIFID_TEMP", "guest-uifid", domain=".douyin.com")
            return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    session = await DouyinGuestAPI()._create_session()

    assert requested_urls == ["https://live.douyin.com/"]
    assert session == GuestSession(ttwid="guest-ttwid", uifid="guest-uifid")


@pytest.mark.asyncio
async def test_guest_api_refreshes_session_after_empty_response(monkeypatch):
    api = DouyinGuestAPI()
    sessions = iter(
        [GuestSession("old", "old-uifid"), GuestSession("new", "new-uifid")]
    )
    created = []

    async def fake_create_session():
        session = next(sessions)
        created.append(session)
        return session

    async def fake_build_request(_aweme_id, session, _source_url):
        return GuestRequest(
            endpoint="https://example.test/detail",
            headers={"uifid": session.uifid},
        )

    responses = iter(
        [
            httpx.Response(200, content=b""),
            httpx.Response(200, json={"aweme_detail": {"aweme_id": "123"}}),
        ]
    )

    async def fake_get(self, endpoint):
        return next(responses)

    monkeypatch.setattr(api, "_create_session", fake_create_session)
    monkeypatch.setattr(api, "_build_request", fake_build_request)
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
    session = GuestSession("guest-ttwid", "guest-uifid")
    assert session.cookie == "ttwid=guest-ttwid; UIFID_TEMP=guest-uifid;"


@pytest.mark.asyncio
async def test_guest_request_contains_uifid_and_websign(monkeypatch):
    api = DouyinGuestAPI()
    session = GuestSession("guest-ttwid", "guest-uifid")

    monkeypatch.setattr(
        "plugins.link_resolver.core.douyin.websign.time",
        lambda: 1_700_000_000,
    )

    request = await api._build_request(
        "7684636895083644273",
        session,
        "https://www.douyin.com/note/7684636895083644273",
    )

    parsed = httpx.URL(request.endpoint)
    params = dict(parsed.params.multi_items())
    assert params["aweme_id"] == "7684636895083644273"
    assert params["uifid"] == "guest-uifid"
    assert params["timestamp"] == "1700000000"
    assert params["x-secsdk-web-signature"]
    assert request.headers == {
        "Referer": "https://www.douyin.com/note/7684636895083644273",
        "uifid": "guest-uifid",
        "x-secsdk-web-signature": params["x-secsdk-web-signature"],
        "x-secsdk-web-expire": "1700000000",
    }


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


def test_video_url_keeps_all_default_play_address_variants():
    video = {
        "play_addr": {"url_list": ["https://example.com/default"]},
        "play_addr_h264": {"url_list": ["https://example.com/h264"]},
        "play_addr_h265": {"url_list": ["https://example.com/h265"]},
        "play_addr_lowbr": {"url_list": ["https://example.com/low"]},
    }

    urls = DouyinExtractor._select_highest_quality_video_urls(video, video["play_addr"])

    assert urls == [
        "https://example.com/default",
        "https://example.com/h264",
        "https://example.com/h265",
        "https://example.com/low",
    ]


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
