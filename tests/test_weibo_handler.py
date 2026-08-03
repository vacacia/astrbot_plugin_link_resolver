# ruff: noqa: E402
"""Integration-style tests for Weibo handler entrypoints.

Run from the AcaBot repo root:
    .venv/bin/python -m pytest extensions/plugins/link_resolver/tests/test_weibo_handler.py -q
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.api.message_components import Video
from plugins.link_resolver.core.weibo import WeiboResult
from plugins.link_resolver.core.weibo.handler import WeiboMixin
from plugins.link_resolver.main import LinkResolver


class DummyEvent:
    def __init__(self, message_str: str = "", components: list | None = None):
        self.message_str = message_str
        self.message_obj = SimpleNamespace(message=components or [], raw_message=None)
        self.bot = None
        self.sent = []
        self._llm = False

    def get_sender_id(self):
        return "10001"

    def get_self_id(self):
        return "20002"

    def get_group_id(self):
        return "30003"

    def should_call_llm(self, value: bool):
        self._llm = value

    async def send(self, chain):
        self.sent.append(chain)


class TestWeiboHandler(unittest.IsolatedAsyncioTestCase):
    async def test_handle_weibo_dispatches_first_link(self):
        event = DummyEvent(
            "先看这个 https://weibo.com/1234567890/AbCdEfGhI 再看这个 https://weibo.com/1234567890/QwErTyUiO"
        )
        plugin = SimpleNamespace(
            weibo_enabled=True,
            _is_self_message=lambda event: False,
            _is_bot_muted=AsyncMock(return_value=False),
            _process_weibo=AsyncMock(),
        )

        await WeiboMixin.handle_weibo(plugin, event)

        plugin._process_weibo.assert_awaited_once_with(
            event, "https://weibo.com/1234567890/AbCdEfGhI", is_from_card=False
        )
        self.assertTrue(event._llm)

    async def test_handle_json_card_dispatches_weibo_link(self):
        event = DummyEvent(
            components=[
                {
                    "type": "json",
                    "data": {
                        "meta": {
                            "detail_1": {
                                "url": "https://weibo.com/1234567890/AbCdEfGhI"
                            }
                        }
                    },
                }
            ]
        )
        plugin = LinkResolver.__new__(LinkResolver)
        plugin.bili_enabled = False
        plugin.douyin_enabled = False
        plugin.xhs_enabled = False
        plugin.weibo_enabled = True
        plugin._register_parse_task = lambda *args, **kwargs: None
        plugin._is_bot_muted = AsyncMock(return_value=False)
        plugin._process_weibo = AsyncMock()
        plugin.extract_links_from_json = LinkResolver.extract_links_from_json.__get__(
            plugin, LinkResolver
        )
        plugin._coerce_json_payload = LinkResolver._coerce_json_payload.__get__(
            plugin, LinkResolver
        )

        async for _ in LinkResolver.handle_json_card(plugin, event):
            pass

        plugin._process_weibo.assert_awaited_once_with(
            event, "https://weibo.com/1234567890/AbCdEfGhI", is_from_card=True
        )
        self.assertTrue(event._llm)

    async def test_process_weibo_merged_video_uses_callback_preparation(self):
        event = DummyEvent()
        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = Path(tmpdir) / "demo.mp4"
            video_path.write_bytes(b"video")

            plugin = SimpleNamespace(
                weibo_enabled=True,
                weibo_merge_send=True,
                weibo_max_media=99,
                retry_count=0,
                max_video_size_mb=200,
                weibo_extractor=SimpleNamespace(
                    parse=AsyncMock(
                        return_value=WeiboResult(
                            title="微博视频",
                            author="博主丙",
                            text="视频正文",
                            image_urls=[],
                            video_url="https://media.example.com/demo.mp4",
                            cover_url=None,
                            source_url="https://weibo.com/1234567890/AbCdEfGhI",
                        )
                    )
                ),
                _refresh_config=lambda: None,
                _send_reaction_emoji=AsyncMock(),
                _download_weibo_video=AsyncMock(return_value=video_path),
                _download_weibo_image=AsyncMock(),
                _prepare_component_for_merge_send=AsyncMock(
                    side_effect=lambda component: component
                ),
                _get_merge_sender_uin=lambda event: "10001",
                cleanup_files=AsyncMock(),
            )
            plugin._build_weibo_summary = WeiboMixin._build_weibo_summary.__get__(
                plugin, WeiboMixin
            )

            await WeiboMixin._process_weibo(
                plugin, event, "https://weibo.com/1234567890/AbCdEfGhI"
            )

        plugin._prepare_component_for_merge_send.assert_awaited_once()
        plugin.cleanup_files.assert_awaited_once()
        self.assertEqual(len(event.sent), 1)
        nodes = event.sent[0].chain[0]
        self.assertEqual(len(nodes.nodes), 2)

    async def test_process_weibo_non_merged_video_sends_only_video(self):
        event = DummyEvent()
        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = Path(tmpdir) / "demo.mp4"
            video_path.write_bytes(b"video")

            plugin = SimpleNamespace(
                weibo_enabled=True,
                weibo_merge_send=False,
                weibo_max_media=99,
                retry_count=0,
                max_video_size_mb=200,
                weibo_extractor=SimpleNamespace(
                    parse=AsyncMock(
                        return_value=WeiboResult(
                            title="微博视频",
                            author="博主丁",
                            text="视频正文",
                            image_urls=[],
                            video_url="https://media.example.com/demo.mp4",
                            cover_url=None,
                            source_url="https://weibo.com/1234567890/AbCdEfGhI",
                        )
                    )
                ),
                _refresh_config=lambda: None,
                _send_reaction_emoji=AsyncMock(),
                _download_weibo_video=AsyncMock(return_value=video_path),
                _download_weibo_image=AsyncMock(),
                _prepare_component_for_merge_send=AsyncMock(),
                _get_merge_sender_uin=lambda event: "10001",
                cleanup_files=AsyncMock(),
            )
            plugin._build_weibo_summary = WeiboMixin._build_weibo_summary.__get__(
                plugin, WeiboMixin
            )

            await WeiboMixin._process_weibo(
                plugin, event, "https://weibo.com/1234567890/AbCdEfGhI"
            )

        plugin._prepare_component_for_merge_send.assert_not_awaited()
        plugin.cleanup_files.assert_awaited_once()
        self.assertEqual(len(event.sent), 1)
        chain = event.sent[0].chain
        self.assertEqual(len(chain), 1)
        self.assertIsInstance(chain[0], Video)


if __name__ == "__main__":
    unittest.main(verbosity=2)
