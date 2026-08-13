"""Signed Douyin detail API fallback using an automatically created guest session."""

from __future__ import annotations

import asyncio
import random
import string
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .abogus import ABogus, BrowserFingerprintGenerator
from .errors import DouyinParseError

DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
)


@dataclass(slots=True)
class GuestSession:
    ttwid: str
    s_v_web_id: str

    @property
    def cookie(self) -> str:
        return f"ttwid={self.ttwid}; s_v_web_id={self.s_v_web_id};"


class DouyinGuestAPI:
    """Fetch public works without requiring a logged-in Douyin account."""

    DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self._session: GuestSession | None = None
        self._session_lock = asyncio.Lock()

    async def _create_session(self) -> GuestSession:
        payload = (
            '{"region":"cn","aid":1768,"needFid":false,'
            '"service":"www.ixigua.com","migrate_info":{"ticket":"",'
            '"source":"node"},"cbUrlProtocol":"https","union":true}'
        )
        headers = {
            "User-Agent": DESKTOP_USER_AGENT,
            "Content-Type": "application/json; charset=utf-8",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                "https://ttwid.bytedance.com/ttwid/union/register/",
                content=payload,
                headers=headers,
            )
        response.raise_for_status()
        ttwid = response.cookies.get("ttwid")
        alphabet = string.ascii_letters + string.digits
        s_v_web_id = f"verify_{int(time.time() * 1000)}_" + "".join(
            random.choices(alphabet, k=36)
        )
        if not ttwid or not s_v_web_id:
            raise DouyinParseError("failed to create Douyin guest session")
        return GuestSession(ttwid=ttwid, s_v_web_id=s_v_web_id)

    async def _get_session(self, *, refresh: bool = False) -> GuestSession:
        async with self._session_lock:
            if refresh or self._session is None:
                self._session = await self._create_session()
            return self._session

    async def _build_endpoint(self, aweme_id: str) -> str:
        token_alphabet = string.ascii_letters + string.digits + "-_"
        ms_token = "".join(random.choices(token_alphabet, k=184))
        params: dict[str, Any] = {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "pc_client_type": 1,
            "publish_video_strategy_type": 2,
            "pc_libra_divert": "Windows",
            "version_code": "290100",
            "version_name": "29.1.0",
            "cookie_enabled": "true",
            "screen_width": 1920,
            "screen_height": 1080,
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Edge",
            "browser_version": "130.0.0.0",
            "browser_online": "true",
            "engine_name": "Blink",
            "engine_version": "130.0.0.0",
            "os_name": "Windows",
            "os_version": "10",
            "cpu_core_num": 12,
            "device_memory": 8,
            "platform": "PC",
            "downlink": 10,
            "effective_type": "4g",
            "round_trip_time": 100,
            "msToken": ms_token,
            "aweme_id": aweme_id,
        }
        param_str = "&".join(f"{key}={value}" for key, value in params.items())
        fingerprint = BrowserFingerprintGenerator.generate_fingerprint("Edge")
        signature = ABogus(
            fp=fingerprint, user_agent=DESKTOP_USER_AGENT
        ).generate_abogus(param_str, "")[1]
        return f"{self.DETAIL_URL}?{param_str}&a_bogus={signature}"

    async def fetch_detail(self, aweme_id: str) -> dict[str, Any]:
        """Return ``aweme_detail``, refreshing guest state once on an empty reply."""

        last_error = "empty response"
        last_exception: httpx.HTTPError | None = None
        for refresh in (False, True):
            try:
                session = await self._get_session(refresh=refresh)
                endpoint = await self._build_endpoint(aweme_id)
                headers = {
                    "User-Agent": DESKTOP_USER_AGENT,
                    "Referer": "https://www.douyin.com/",
                    "Cookie": session.cookie,
                }
                async with httpx.AsyncClient(
                    timeout=self.timeout, headers=headers
                ) as client:
                    response = await client.get(endpoint)
            except httpx.HTTPError as exc:
                last_exception = exc
                last_error = f"network error: {exc}"
                continue
            if response.status_code != 200:
                last_error = f"status {response.status_code}"
                continue
            if not response.content:
                last_error = "HTTP 200 with empty body"
                continue
            try:
                payload = response.json()
            except ValueError as exc:
                last_error = f"invalid JSON: {exc}"
                continue
            detail = payload.get("aweme_detail") if isinstance(payload, dict) else None
            if isinstance(detail, dict) and detail:
                return detail
            status = payload.get("status_code") if isinstance(payload, dict) else None
            last_error = f"missing aweme_detail (status_code={status})"

        error = DouyinParseError(f"signed guest detail failed: {last_error}")
        if last_exception:
            raise error from last_exception
        raise error
