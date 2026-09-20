"""使用自动创建的匿名访客身份请求抖音详情接口."""

from __future__ import annotations

import asyncio
import random
import string
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from .abogus import ABogus, BrowserFingerprintGenerator
from .errors import DouyinParseError
from .websign import encode_pairs, sign

DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
)


@dataclass(slots=True)
class GuestSession:
    ttwid: str
    uifid: str

    @property
    def cookie(self) -> str:
        return f"ttwid={self.ttwid}; UIFID_TEMP={self.uifid};"


@dataclass(frozen=True, slots=True)
class GuestRequest:
    endpoint: str
    headers: dict[str, str]


class DouyinGuestAPI:
    """无需登录抖音账号即可读取公开作品."""

    DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout
        self._session: GuestSession | None = None
        self._session_lock = asyncio.Lock()

    async def _create_session(self) -> GuestSession:
        headers = {
            "User-Agent": DESKTOP_USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers=headers,
        ) as client:
            response = await client.get("https://live.douyin.com/")
            response.raise_for_status()
            cookies = {
                cookie.name: cookie.value
                for cookie in client.cookies.jar
                if cookie.value
            }
        ttwid = cookies.get("ttwid")
        uifid = cookies.get("UIFID_TEMP")
        if not ttwid or not uifid:
            raise DouyinParseError(
                "failed to create Douyin guest identity: missing ttwid or UIFID_TEMP"
            )
        return GuestSession(ttwid=ttwid, uifid=uifid)

    async def _get_session(self, *, refresh: bool = False) -> GuestSession:
        async with self._session_lock:
            if refresh or self._session is None:
                self._session = await self._create_session()
            return self._session

    @staticmethod
    def _referer_for(aweme_id: str, source_url: str | None) -> str:
        if source_url:
            try:
                hostname = (urlparse(source_url).hostname or "").lower()
            except ValueError:
                hostname = ""
            if hostname == "douyin.com" or hostname.endswith(".douyin.com"):
                return source_url
        return f"https://www.douyin.com/video/{aweme_id}"

    async def _build_request(
        self,
        aweme_id: str,
        session: GuestSession,
        source_url: str | None,
    ) -> GuestRequest:
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
            "uifid": session.uifid,
        }
        param_pairs = [(key, str(value)) for key, value in params.items()]
        param_str = encode_pairs(param_pairs)
        fingerprint = BrowserFingerprintGenerator.generate_fingerprint("Edge")
        signature = ABogus(
            fp=fingerprint, user_agent=DESKTOP_USER_AGENT
        ).generate_abogus(param_str, "")[1]
        query_with_abogus = encode_pairs(
            [
                *param_pairs,
                ("a_bogus", signature),
            ]
        )
        signed = sign(query_with_abogus, session.uifid)
        return GuestRequest(
            endpoint=f"{self.DETAIL_URL}?{signed.query}",
            headers={
                "Referer": self._referer_for(aweme_id, source_url),
                "uifid": session.uifid,
                "x-secsdk-web-signature": signed.signature,
                "x-secsdk-web-expire": signed.timestamp,
            },
        )

    async def fetch_detail(
        self, aweme_id: str, source_url: str | None = None
    ) -> dict[str, Any]:
        """返回 ``aweme_detail``, 失败时刷新一次匿名身份."""

        last_error = "empty response"
        last_exception: httpx.HTTPError | None = None
        for refresh in (False, True):
            try:
                session = await self._get_session(refresh=refresh)
                request = await self._build_request(aweme_id, session, source_url)
                headers = {
                    "User-Agent": DESKTOP_USER_AGENT,
                    "Cookie": session.cookie,
                    **request.headers,
                }
                async with httpx.AsyncClient(
                    timeout=self.timeout, headers=headers
                ) as client:
                    response = await client.get(request.endpoint)
            except httpx.HTTPError as exc:
                last_exception = exc
                last_error = f"network error: {exc}"
                continue
            if response.status_code != 200:
                response_text = response.text.strip().replace("\n", " ")[:160]
                last_error = f"status {response.status_code}"
                if response_text:
                    last_error += f" ({response_text})"
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
