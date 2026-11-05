# ruff: noqa: E402
"""验证平台注册表, 统一兜底, 下载 4xx 不重试, 抖音空 slides 回退."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx

for candidate in Path(__file__).resolve().parents:
    if (candidate / "data" / "plugins").exists():
        sys.path.insert(0, str(candidate))
        break

from data.plugins.astrbot_plugin_link_resolver.core.douyin import (
    DouyinExtractor,
    DouyinParseError,
)
from data.plugins.astrbot_plugin_link_resolver.core.generic import GenericExtractor
from data.plugins.astrbot_plugin_link_resolver.core.registry import (
    PLATFORMS,
    TASK_QUALNAME_TOKENS,
    classify_links,
)
from data.plugins.astrbot_plugin_link_resolver.main import (
    CARD_HANDLED,
    LinkResolverPlugin,
)


def make_plugin(mode="静默"):
    plugin = LinkResolverPlugin.__new__(LinkResolverPlugin)
    plugin.error_notify_mode = mode
    return plugin


def make_event():
    return SimpleNamespace(set_result=Mock(), plain_result=lambda text: text)


class TestRegistry(unittest.TestCase):
    def test_classify_links_groups_by_platform(self):
        grouped = classify_links(
            [
                "https://b23.tv/abc123",
                "https://v.douyin.com/AbCd/",
                "https://youtu.be/dQw4w9WgXcQ",
                "https://mmocgame.qpic.cn/wechatgame/xx",
            ],
            link_filters={"generic": GenericExtractor().accepts},
        )
        self.assertEqual(set(grouped), {"bili", "douyin", "youtube"})

    def test_registry_covers_task_tokens_and_enabled_attr(self):
        self.assertEqual(len(TASK_QUALNAME_TOKENS), len(PLATFORMS) * 2)
        self.assertIn("handle_bili_video", TASK_QUALNAME_TOKENS)
        self.assertEqual(PLATFORMS[0].enabled_attr, "bili_enabled")

    def test_every_platform_has_method_on_plugin(self):
        for platform in PLATFORMS:
            self.assertTrue(hasattr(LinkResolverPlugin, platform.handle_name))
            self.assertTrue(hasattr(LinkResolverPlugin, platform.process_name))


class TestGuard(unittest.IsolatedAsyncioTestCase):
    async def test_guarded_swallows_send_failure(self):
        plugin, event = make_plugin(), make_event()

        async def boom():
            raise RuntimeError("retcode=1200 Timeout")

        await plugin._guarded("douyin", event, boom())
        event.set_result.assert_not_called()

    async def test_guarded_notifies_by_mode(self):
        event = make_event()

        async def boom():
            raise RuntimeError("secret detail")

        await make_plugin("脱敏")._guarded("douyin", event, boom())
        event.set_result.assert_called_once_with("❌ 抖音 处理失败")
        event = make_event()
        await make_plugin("报错")._guarded("douyin", event, boom())
        self.assertIn("secret detail", event.set_result.call_args.args[0])

    async def test_guarded_propagates_cancel(self):
        async def cancelled():
            raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await make_plugin()._guarded("bili", make_event(), cancelled())

    async def test_guarded_stream_marks_handled_on_error(self):
        async def broken():
            yield "first"
            raise RuntimeError("boom")

        items = [
            item
            async for item in make_plugin()._guarded_stream(
                "xhs", make_event(), broken(), error_result=CARD_HANDLED
            )
        ]
        self.assertEqual(items, ["first", CARD_HANDLED])


class TestDownloadNoRetryOn4xx(unittest.IsolatedAsyncioTestCase):
    async def test_403_is_not_retried(self):
        plugin = make_plugin()
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(403, request=request)

        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        def patched(*args, **kwargs):
            return real_client(*args, transport=transport, **kwargs)

        import data.plugins.astrbot_plugin_link_resolver.main as main_module

        original = main_module.httpx.AsyncClient
        main_module.httpx.AsyncClient = patched
        try:
            with tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(httpx.HTTPStatusError):
                    await plugin._download_stream(
                        "https://example.com/a.mp4",
                        Path(directory) / "a.mp4",
                        cookies=None,
                        max_bytes=None,
                        retries=3,
                    )
        finally:
            main_module.httpx.AsyncClient = original
        self.assertEqual(calls, 1)


class TestDouyinNullSlides(unittest.IsolatedAsyncioTestCase):
    async def test_null_aweme_details_becomes_parse_error(self):
        extractor = DouyinExtractor()
        response = Mock(content=b'{"aweme_details": null}')
        response.raise_for_status = Mock()
        client = AsyncMock()
        client.get.return_value = response
        client.__aenter__.return_value = client
        import data.plugins.astrbot_plugin_link_resolver.core.douyin as douyin_module

        original = douyin_module.httpx.AsyncClient
        douyin_module.httpx.AsyncClient = Mock(return_value=client)
        try:
            with self.assertRaises(DouyinParseError):
                await extractor.parse_slides("123", "https://www.douyin.com/note/123")
        finally:
            douyin_module.httpx.AsyncClient = original


if __name__ == "__main__":
    unittest.main()
