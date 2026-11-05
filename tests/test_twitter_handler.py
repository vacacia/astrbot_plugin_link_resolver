# ruff: noqa: E402
"""Integration-style tests for Twitter/X handler entrypoints.

Run inside AstrBot container:
    cd /AstrBot
    python /AstrBot/data/plugins/astrbot_plugin_link_resolver/tests/test_twitter_handler.py -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

for candidate in Path(__file__).resolve().parents:
    if (candidate / "data" / "plugins").exists():
        root_path = str(candidate)
        if root_path not in sys.path:
            sys.path.insert(0, root_path)
        break

from astrbot.api.message_components import Plain, Video
from data.plugins.astrbot_plugin_link_resolver.core.generic import GenericExtractor
from data.plugins.astrbot_plugin_link_resolver.core.twitter import TwitterResult
from data.plugins.astrbot_plugin_link_resolver.core.twitter.handler import (
    TWITTER_TEXT_CHUNK_SIZE,
    TwitterMixin,
    split_twitter_text,
)
from data.plugins.astrbot_plugin_link_resolver.main import LinkResolver


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


class TestTwitterHandler(unittest.IsolatedAsyncioTestCase):
    def test_conf_schema_exposes_x_platform_and_twitter_settings(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertIn("X", schema["enable_platforms"]["options"])
        self.assertIn("X", schema["enable_platforms"]["default"])
        self.assertEqual(
            schema["twitter_settings"]["items"]["max_media"]["default"], 99
        )
        self.assertFalse(schema["twitter_settings"]["items"]["merge_send"]["default"])

    async def test_process_twitter_image_post_merges_with_plain_summary(self):
        event = DummyEvent()
        with tempfile.TemporaryDirectory() as tmpdir:
            image_a = Path(tmpdir) / "a.jpg"
            image_b = Path(tmpdir) / "b.jpg"
            image_a.write_bytes(b"a")
            image_b.write_bytes(b"b")

            plugin = SimpleNamespace(
                twitter_enabled=True,
                twitter_merge_send=False,
                twitter_max_media=99,
                retry_count=0,
                max_video_size_mb=200,
                twitter_extractor=SimpleNamespace(
                    parse=AsyncMock(
                        return_value=TwitterResult(
                            text="图片正文",
                            author="Alice(@alice)",
                            created_at="2025-04-01",
                            image_urls=[
                                "https://pbs.twimg.com/media/a.jpg",
                                "https://pbs.twimg.com/media/b.jpg",
                            ],
                            video_urls=[],
                            source_url="https://x.com/alice/status/1234567890123456789",
                            tweet_id="1234567890123456789",
                        )
                    )
                ),
                _refresh_config=lambda: None,
                _send_reaction_emoji=AsyncMock(),
                _download_twitter_image=AsyncMock(side_effect=[image_a, image_b]),
                _download_twitter_video=AsyncMock(),
                _prepare_component_for_merge_send=AsyncMock(
                    side_effect=lambda component: component
                ),
                _get_merge_sender_uin=lambda event: "10001",
                cleanup_files=AsyncMock(),
            )
            plugin._build_twitter_info = TwitterMixin._build_twitter_info.__get__(
                plugin, TwitterMixin
            )

            await TwitterMixin._process_twitter(
                plugin, event, "https://x.com/alice/status/1234567890123456789"
            )

        self.assertEqual(len(event.sent), 1)
        nodes = event.sent[0].chain[0]
        self.assertEqual(len(nodes.nodes), 4)
        info_component = nodes.nodes[0].content[0]
        self.assertIsInstance(info_component, Plain)
        self.assertIn("X @Alice(@alice)", info_component.text)
        self.assertNotIn("图片正文", info_component.text)
        self.assertEqual(nodes.nodes[1].content[0].text, "图片正文")

    async def test_process_twitter_single_video_non_merged_sends_only_video(self):
        event = DummyEvent()
        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = Path(tmpdir) / "demo.mp4"
            video_path.write_bytes(b"video")

            plugin = SimpleNamespace(
                twitter_enabled=True,
                twitter_merge_send=False,
                twitter_max_media=99,
                retry_count=0,
                max_video_size_mb=200,
                twitter_extractor=SimpleNamespace(
                    parse=AsyncMock(
                        return_value=TwitterResult(
                            text="视频正文",
                            author="Bob(@bob)",
                            created_at="2025-05-02",
                            image_urls=[],
                            video_urls=[
                                "https://video.twimg.com/ext_tw_video/demo.mp4"
                            ],
                            source_url="https://x.com/bob/status/1234567890123456789",
                            tweet_id="1234567890123456789",
                        )
                    )
                ),
                _refresh_config=lambda: None,
                _send_reaction_emoji=AsyncMock(),
                _download_twitter_video=AsyncMock(return_value=video_path),
                _download_twitter_image=AsyncMock(),
                _prepare_component_for_merge_send=AsyncMock(),
                _get_merge_sender_uin=lambda event: "10001",
                cleanup_files=AsyncMock(),
            )
            plugin._build_twitter_info = TwitterMixin._build_twitter_info.__get__(
                plugin, TwitterMixin
            )

            await TwitterMixin._process_twitter(
                plugin, event, "https://x.com/bob/status/1234567890123456789"
            )

        plugin._prepare_component_for_merge_send.assert_not_awaited()
        self.assertEqual(len(event.sent), 1)
        chain = event.sent[0].chain
        self.assertEqual(len(chain), 1)
        self.assertIsInstance(chain[0], Video)

    async def test_process_twitter_cleans_download_when_send_fails(self):
        event = DummyEvent()
        event.send = AsyncMock(side_effect=RuntimeError("send failed"))
        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = Path(tmpdir) / "demo.mp4"
            video_path.write_bytes(b"video")
            plugin = SimpleNamespace(
                twitter_enabled=True,
                twitter_merge_send=False,
                twitter_max_media=99,
                retry_count=0,
                max_video_size_mb=200,
                twitter_extractor=SimpleNamespace(
                    parse=AsyncMock(
                        return_value=TwitterResult(
                            text="视频正文",
                            author="Bob(@bob)",
                            created_at="2025-05-02",
                            image_urls=[],
                            video_urls=["https://video.twimg.com/demo.mp4"],
                            source_url="https://x.com/bob/status/1234567890123456789",
                            tweet_id="1234567890123456789",
                        )
                    )
                ),
                _refresh_config=lambda: None,
                _send_reaction_emoji=AsyncMock(),
                _download_twitter_video=AsyncMock(return_value=video_path),
                _download_twitter_image=AsyncMock(),
                _prepare_component_for_merge_send=AsyncMock(),
                _get_merge_sender_uin=lambda event: "10001",
                cleanup_files=AsyncMock(),
            )
            plugin._build_twitter_info = TwitterMixin._build_twitter_info.__get__(
                plugin, TwitterMixin
            )

            with self.assertRaisesRegex(RuntimeError, "send failed"):
                await TwitterMixin._process_twitter(
                    plugin, event, "https://x.com/bob/status/1234567890123456789"
                )

        plugin.cleanup_files.assert_awaited_once_with([video_path], [])

    async def test_process_twitter_single_video_merged_sends_summary_plus_video(self):
        event = DummyEvent()
        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = Path(tmpdir) / "demo.mp4"
            video_path.write_bytes(b"video")

            plugin = SimpleNamespace(
                twitter_enabled=True,
                twitter_merge_send=True,
                twitter_max_media=99,
                retry_count=0,
                max_video_size_mb=200,
                twitter_extractor=SimpleNamespace(
                    parse=AsyncMock(
                        return_value=TwitterResult(
                            text="视频正文",
                            author="Bob(@bob)",
                            created_at="2025-05-02",
                            image_urls=[],
                            video_urls=[
                                "https://video.twimg.com/ext_tw_video/demo.mp4"
                            ],
                            source_url="https://x.com/bob/status/1234567890123456789",
                            tweet_id="1234567890123456789",
                        )
                    )
                ),
                _refresh_config=lambda: None,
                _send_reaction_emoji=AsyncMock(),
                _download_twitter_video=AsyncMock(return_value=video_path),
                _download_twitter_image=AsyncMock(),
                _prepare_component_for_merge_send=AsyncMock(
                    side_effect=lambda component: component
                ),
                _get_merge_sender_uin=lambda event: "10001",
                cleanup_files=AsyncMock(),
            )
            plugin._build_twitter_info = TwitterMixin._build_twitter_info.__get__(
                plugin, TwitterMixin
            )

            await TwitterMixin._process_twitter(
                plugin, event, "https://x.com/bob/status/1234567890123456789"
            )

        self.assertEqual(len(event.sent), 1)
        nodes = event.sent[0].chain[0]
        self.assertEqual(len(nodes.nodes), 3)
        self.assertIsInstance(nodes.nodes[0].content[0], Plain)
        self.assertEqual(nodes.nodes[1].content[0].text, "视频正文")
        plugin._prepare_component_for_merge_send.assert_awaited_once()

    async def test_process_twitter_mixed_media_forces_merge_even_when_disabled(self):
        event = DummyEvent()
        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = Path(tmpdir) / "demo.mp4"
            image_path = Path(tmpdir) / "demo.jpg"
            video_path.write_bytes(b"video")
            image_path.write_bytes(b"image")

            plugin = SimpleNamespace(
                twitter_enabled=True,
                twitter_merge_send=False,
                twitter_max_media=99,
                retry_count=0,
                max_video_size_mb=200,
                twitter_extractor=SimpleNamespace(
                    parse=AsyncMock(
                        return_value=TwitterResult(
                            text="混合正文",
                            author="Carol(@carol)",
                            created_at="2025-06-07",
                            image_urls=["https://pbs.twimg.com/media/a.jpg"],
                            video_urls=["https://video.twimg.com/ext_tw_video/a.mp4"],
                            source_url="https://x.com/carol/status/1234567890123456789",
                            tweet_id="1234567890123456789",
                        )
                    )
                ),
                _refresh_config=lambda: None,
                _send_reaction_emoji=AsyncMock(),
                _download_twitter_video=AsyncMock(return_value=video_path),
                _download_twitter_image=AsyncMock(return_value=image_path),
                _prepare_component_for_merge_send=AsyncMock(
                    side_effect=lambda component: component
                ),
                _get_merge_sender_uin=lambda event: "10001",
                cleanup_files=AsyncMock(),
            )
            plugin._build_twitter_info = TwitterMixin._build_twitter_info.__get__(
                plugin, TwitterMixin
            )

            await TwitterMixin._process_twitter(
                plugin, event, "https://x.com/carol/status/1234567890123456789"
            )

        self.assertEqual(len(event.sent), 1)
        nodes = event.sent[0].chain[0]
        self.assertEqual(len(nodes.nodes), 4)
        self.assertIsInstance(nodes.nodes[0].content[0], Plain)
        self.assertEqual(nodes.nodes[1].content[0].text, "混合正文")
        self.assertEqual(plugin._prepare_component_for_merge_send.await_count, 2)

    def _make_text_only_plugin(self, text: str | None, merge_send: bool = False):
        plugin = SimpleNamespace(
            twitter_enabled=True,
            twitter_merge_send=merge_send,
            twitter_max_media=99,
            retry_count=0,
            max_video_size_mb=200,
            twitter_extractor=SimpleNamespace(
                parse=AsyncMock(
                    return_value=TwitterResult(
                        text=text,
                        author="Eve(@eve)",
                        created_at="2025-08-09",
                        image_urls=[],
                        video_urls=[],
                        source_url="https://x.com/eve/status/1234567890123456789",
                        tweet_id="1234567890123456789",
                    )
                )
            ),
            _refresh_config=lambda: None,
            _send_reaction_emoji=AsyncMock(),
            _download_twitter_image=AsyncMock(),
            _download_twitter_video=AsyncMock(),
            _prepare_component_for_merge_send=AsyncMock(),
            _get_merge_sender_uin=lambda event: "10001",
            cleanup_files=AsyncMock(),
        )
        plugin._build_twitter_info = TwitterMixin._build_twitter_info.__get__(
            plugin, TwitterMixin
        )
        return plugin

    async def test_process_twitter_text_only_merges_info_then_full_text(self):
        event = DummyEvent()
        text = "\n\n".join(f"第{i}段" + "字" * 400 for i in range(8))
        plugin = self._make_text_only_plugin(text, merge_send=False)

        await TwitterMixin._process_twitter(
            plugin, event, "https://x.com/eve/status/1234567890123456789"
        )

        self.assertEqual(len(event.sent), 1)
        nodes = event.sent[0].chain[0].nodes
        texts = [node.content[0].text for node in nodes]
        self.assertIn("X @Eve(@eve)", texts[0])
        self.assertIn("链接: https://x.com/eve/status/1234567890123456789", texts[0])
        body = texts[1:]
        self.assertGreater(len(body), 1)
        self.assertTrue(all(len(t) <= TWITTER_TEXT_CHUNK_SIZE for t in body))
        self.assertEqual("".join(body).replace("\n", ""), text.replace("\n", ""))
        plugin._download_twitter_image.assert_not_awaited()
        plugin._download_twitter_video.assert_not_awaited()
        plugin._prepare_component_for_merge_send.assert_not_awaited()
        plugin.cleanup_files.assert_not_awaited()

    async def test_process_twitter_empty_tweet_sends_nothing(self):
        event = DummyEvent()
        plugin = self._make_text_only_plugin(None)

        await TwitterMixin._process_twitter(
            plugin, event, "https://x.com/eve/status/1234567890123456789"
        )

        self.assertEqual(event.sent, [])

    def test_split_twitter_text_keeps_every_char_and_respects_limit(self):
        self.assertEqual(split_twitter_text(""), [])
        self.assertEqual(split_twitter_text("  短文  "), ["短文"])

        hard = "字" * 2500
        chunks = split_twitter_text(hard, 1000)
        self.assertEqual([len(c) for c in chunks], [1000, 1000, 500])

        mixed = ("甲" * 600 + "\n\n") + ("乙" * 600 + "\n") + ("丙" * 600)
        chunks = split_twitter_text(mixed, 1000)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))
        self.assertEqual(chunks[0], "甲" * 600)
        self.assertEqual("".join(chunks), mixed.replace("\n", ""))

    async def test_handle_json_card_dispatches_twitter_link(self):
        event = DummyEvent(
            components=[
                {
                    "type": "json",
                    "data": {
                        "meta": {
                            "detail_1": {
                                "url": "https://x.com/demo/status/1234567890123456789"
                            }
                        }
                    },
                }
            ]
        )
        plugin = LinkResolver.__new__(LinkResolver)
        plugin.generic_extractor = GenericExtractor()
        plugin.generic_enabled = True
        plugin.bili_enabled = False
        plugin.douyin_enabled = False
        plugin.xhs_enabled = False
        plugin.weibo_enabled = False
        plugin.twitter_enabled = True
        plugin.group_filter_mode = "黑名单"
        plugin.group_filter_list = []
        plugin._register_parse_task = lambda *args, **kwargs: None
        plugin._is_bot_muted = AsyncMock(return_value=False)
        plugin._process_twitter = AsyncMock()
        plugin.extract_links_from_json = LinkResolver.extract_links_from_json.__get__(
            plugin, LinkResolver
        )
        plugin._coerce_json_payload = LinkResolver._coerce_json_payload.__get__(
            plugin, LinkResolver
        )

        async for _ in LinkResolver.handle_json_card(plugin, event):
            pass

        plugin._process_twitter.assert_awaited_once_with(
            event, "https://x.com/demo/status/1234567890123456789", is_from_card=True
        )
        self.assertTrue(event._llm)


if __name__ == "__main__":
    unittest.main(verbosity=2)
