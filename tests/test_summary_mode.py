# ruff: noqa: E402
"""Tests for per-platform summary_mode behavior.

Run inside AstrBot container:
    cd /AstrBot
    python /AstrBot/data/plugins/astrbot_plugin_link_resolver/tests/test_summary_mode.py -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

for candidate in Path(__file__).resolve().parents:
    if (candidate / "data" / "plugins").exists():
        root_path = str(candidate)
        if root_path not in sys.path:
            sys.path.insert(0, root_path)
        break

from astrbot.api.message_components import Image, Plain
from data.plugins.astrbot_plugin_link_resolver.core.bilibili.handler import (
    BilibiliMixin,
    VideoRef,
)
from data.plugins.astrbot_plugin_link_resolver.core.douyin import DouyinResult
from data.plugins.astrbot_plugin_link_resolver.core.douyin.handler import DouyinMixin
from data.plugins.astrbot_plugin_link_resolver.core.xiaohongshu import XiaohongshuResult
from data.plugins.astrbot_plugin_link_resolver.core.xiaohongshu.extractor import (
    XiaohongshuExtractor,
)
from data.plugins.astrbot_plugin_link_resolver.core.xiaohongshu.handler import (
    XiaohongshuMixin,
)
from data.plugins.astrbot_plugin_link_resolver.main import LinkResolver


class DummyEvent:
    def __init__(self):
        self.sent = []
        self.result = None

    async def send(self, chain):
        self.sent.append(chain)

    def chain_result(self, chain):
        return chain

    def plain_result(self, text: str):
        return text

    def set_result(self, result):
        self.result = result


class TestSummaryModeConfig(unittest.TestCase):
    def test_read_summary_mode_defaults_to_text_and_validates_value(self):
        plugin = LinkResolver.__new__(LinkResolver)
        plugin.config = {
            "bili_settings": {},
            "douyin_settings": {"summary_mode": "渲染卡片"},
            "xhs_settings": {"summary_mode": "非法值"},
        }
        plugin._get_config_value = LinkResolver._get_config_value.__get__(
            plugin, LinkResolver
        )

        self.assertEqual(
            LinkResolver._read_summary_mode(plugin, "bili_settings.summary_mode"),
            "文字摘要",
        )
        self.assertEqual(
            LinkResolver._read_summary_mode(plugin, "douyin_settings.summary_mode"),
            "渲染卡片",
        )
        self.assertEqual(
            LinkResolver._read_summary_mode(plugin, "xhs_settings.summary_mode"),
            "文字摘要",
        )

    def test_conf_schema_exposes_summary_mode_for_bili_douyin_and_xhs(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        for platform in ("bili_settings", "douyin_settings", "xhs_settings"):
            item = schema[platform]["items"].get("summary_mode")
            self.assertIsNotNone(item, platform)
            self.assertEqual(item["default"], "文字摘要")
            self.assertEqual(item["options"], ["文字摘要", "渲染卡片"])


class TestSummaryModeHandlers(unittest.IsolatedAsyncioTestCase):
    async def test_douyin_video_download_falls_back_to_next_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "video.mp4"
            plugin = SimpleNamespace(
                max_video_size_mb=200,
                _build_douyin_path=lambda *_args, **_kwargs: output_path,
                _estimate_total_size_mb=AsyncMock(return_value=None),
                _download_stream=AsyncMock(
                    side_effect=[RuntimeError("第一个地址不可用"), 1024]
                ),
            )
            plugin._download_douyin_video = DouyinMixin._download_douyin_video.__get__(
                plugin, DouyinMixin
            )

            result = await plugin._download_douyin_video(
                ["https://example.com/first", "https://example.com/second"],
                "request",
            )

            self.assertEqual(result, output_path)
            self.assertEqual(
                [call.args[0] for call in plugin._download_stream.await_args_list],
                ["https://example.com/first", "https://example.com/second"],
            )

    async def test_douyin_video_download_falls_back_when_high_quality_is_too_large(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "video.mp4"
            plugin = SimpleNamespace(
                max_video_size_mb=200,
                _build_douyin_path=lambda *_args, **_kwargs: output_path,
                _estimate_total_size_mb=AsyncMock(side_effect=[300.0, 100.0]),
                _download_stream=AsyncMock(return_value=1024),
            )
            plugin._download_douyin_video = DouyinMixin._download_douyin_video.__get__(
                plugin, DouyinMixin
            )

            result = await plugin._download_douyin_video(
                ["https://example.com/4k", "https://example.com/1080p"],
                "request",
            )

            self.assertEqual(result, output_path)
            self.assertEqual(
                plugin._download_stream.await_args.args[0],
                "https://example.com/1080p",
            )

    async def test_douyin_image_download_falls_back_to_next_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "image.jpg"
            plugin = SimpleNamespace(
                _build_douyin_path=lambda *_args, **_kwargs: output_path,
                _download_stream=AsyncMock(
                    side_effect=[RuntimeError("第一个地址不可用"), 1024]
                ),
            )
            plugin._download_douyin_image = DouyinMixin._download_douyin_image.__get__(
                plugin, DouyinMixin
            )

            result = await plugin._download_douyin_image(
                ["https://example.com/first.jpg", "https://example.com/second.jpg"],
                "request",
            )

            self.assertEqual(result, output_path)
            self.assertEqual(
                [call.args[0] for call in plugin._download_stream.await_args_list],
                [
                    "https://example.com/first.jpg",
                    "https://example.com/second.jpg",
                ],
            )

    async def test_xhs_extracts_live_photo_video(self):
        extractor = XiaohongshuExtractor()

        result = extractor._build_result_from_note(
            {
                "type": "normal",
                "imageList": [
                    {
                        "urlDefault": "https://example.com/image.jpg",
                        "stream": {
                            "h264": [{"masterUrl": "https://example.com/live.mp4"}]
                        },
                    }
                ],
            },
            "https://www.xiaohongshu.com/explore/demo",
        )

        self.assertEqual(result.image_urls, ["https://example.com/image.jpg"])
        self.assertEqual(result.live_photo_urls, ["https://example.com/live.mp4"])

    async def test_xhs_preserves_stream_and_image_backup_urls(self):
        extractor = XiaohongshuExtractor()

        result = extractor._build_result_from_note(
            {
                "type": "normal",
                "imageList": [
                    {
                        "urlDefault": "https://example.com/default.jpg!style",
                        "url": "https://example.com/fallback.jpg",
                        "stream": {
                            "h264": [
                                {
                                    "masterUrl": "https://example.com/live.mp4",
                                    "backupUrls": [
                                        "https://backup.example.com/live.mp4"
                                    ],
                                }
                            ]
                        },
                    }
                ],
            },
            "https://www.xiaohongshu.com/explore/demo",
        )

        self.assertEqual(
            result.image_url_candidates,
            [["https://example.com/default.jpg", "https://example.com/fallback.jpg"]],
        )
        self.assertEqual(
            result.live_photo_url_candidates,
            [
                [
                    "https://example.com/live.mp4",
                    "https://backup.example.com/live.mp4",
                ]
            ],
        )

    async def test_xhs_preserves_video_backup_urls_by_codec_priority(self):
        extractor = XiaohongshuExtractor()

        result = extractor._build_result_from_note(
            {
                "type": "video",
                "video": {
                    "media": {
                        "stream": {
                            "h264": [{"masterUrl": "https://example.com/h264.mp4"}],
                            "h265": [
                                {
                                    "masterUrl": "https://example.com/h265.mp4",
                                    "backupUrl": "https://backup.example.com/h265.mp4",
                                }
                            ],
                        }
                    }
                },
            },
            "https://www.xiaohongshu.com/explore/demo",
        )

        self.assertEqual(
            result.video_urls,
            [
                "https://example.com/h265.mp4",
                "https://backup.example.com/h265.mp4",
                "https://example.com/h264.mp4",
            ],
        )
        self.assertEqual(result.video_url, result.video_urls[0])

    async def test_xhs_video_download_falls_back_to_next_candidate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "video.mp4"
            plugin = SimpleNamespace(
                max_video_size_mb=200,
                _build_xhs_path=lambda *_args, **_kwargs: output_path,
                _xhs_download_headers=lambda *_args: {},
                _estimate_total_size_mb=AsyncMock(return_value=None),
                _download_stream=AsyncMock(
                    side_effect=[RuntimeError("第一个地址不可用"), 1024]
                ),
            )
            plugin._download_xhs_video = XiaohongshuMixin._download_xhs_video.__get__(
                plugin, XiaohongshuMixin
            )

            result = await plugin._download_xhs_video(
                ["https://example.com/first.mp4", "https://example.com/second.mp4"],
                "request",
            )

            self.assertEqual(result, output_path)
            self.assertEqual(
                [call.args[0] for call in plugin._download_stream.await_args_list],
                [
                    "https://example.com/first.mp4",
                    "https://example.com/second.mp4",
                ],
            )

    async def test_xhs_video_download_checks_next_candidate_after_size_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "video.mp4"
            plugin = SimpleNamespace(
                max_video_size_mb=200,
                retry_count=1,
                _build_xhs_path=lambda *_args, **_kwargs: output_path,
                _xhs_download_headers=lambda *_args: {},
                _estimate_total_size_mb=AsyncMock(side_effect=[300.0, 100.0]),
                _download_stream=AsyncMock(return_value=1024),
            )
            plugin._download_xhs_video = XiaohongshuMixin._download_xhs_video.__get__(
                plugin, XiaohongshuMixin
            )

            result = await plugin._download_xhs_video(
                ["https://example.com/h265.mp4", "https://example.com/h264.mp4"],
                "request",
            )

            self.assertEqual(result, output_path)
            self.assertEqual(
                plugin._download_stream.await_args.args[0],
                "https://example.com/h264.mp4",
            )

    async def test_xhs_file_ids_stay_aligned_when_an_image_has_no_url(self):
        extractor = XiaohongshuExtractor()

        result = extractor._build_result_from_note(
            {
                "type": "normal",
                "imageList": [
                    {"fileId": "missing-url"},
                    {
                        "fileId": "valid-image",
                        "urlDefault": "https://example.com/image.jpg",
                    },
                ],
            },
            "https://www.xiaohongshu.com/explore/demo",
        )

        self.assertEqual(result.image_urls, ["https://example.com/image.jpg"])
        self.assertEqual(result.file_ids, ["valid-image"])

    async def test_build_xhs_summary_strips_topic_marker_inside_hashtags_only(self):
        plugin = SimpleNamespace()
        plugin._build_xhs_summary = XiaohongshuMixin._build_xhs_summary.__get__(
            plugin, XiaohongshuMixin
        )

        summary = plugin._build_xhs_summary(
            XiaohongshuResult(
                title="师徒三人",
                author="芙辛高照",
                text="#芙莉莲[话题]# 普通[话题]文本 #菲伦[话题]#",
                image_urls=["https://example.com/1.jpg"],
                file_ids=[],
                video_url=None,
                cover_url=None,
                source_url="https://www.xiaohongshu.com/explore/demo",
                note_id="demo",
            ),
            image_count=1,
            is_video=False,
        )

        self.assertIn("正文：#芙莉莲# 普通[话题]文本 #菲伦#", summary)

    async def test_process_douyin_image_post_uses_plain_summary_node(self):
        event = DummyEvent()
        original_link = "https://v.douyin.com/demo123/"
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "img.jpg"
            image_path.write_bytes(b"image")

            plugin = SimpleNamespace(
                douyin_enabled=True,
                douyin_summary_mode="文字摘要",
                douyin_render_card=False,
                douyin_merge_send=False,
                douyin_max_media=99,
                retry_count=0,
                max_video_size_mb=200,
                douyin_extractor=SimpleNamespace(
                    parse=AsyncMock(
                        return_value=DouyinResult(
                            title="图文标题",
                            author="作者甲",
                            author_avatar=None,
                            duration=0,
                            video_url=None,
                            cover_url=None,
                            image_urls=["https://example.com/1.jpg"],
                            dynamic_urls=[],
                            source_url="https://www.douyin.com/note/123456789",
                            likes=12000,
                            comments=34,
                            item_id="123",
                        )
                    )
                ),
                _refresh_config=lambda: None,
                _send_reaction_emoji=AsyncMock(),
                _download_douyin_image=AsyncMock(return_value=image_path),
                _download_douyin_video=AsyncMock(),
                _render_douyin_card=AsyncMock(),
                _prepare_component_for_merge_send=AsyncMock(
                    side_effect=lambda component: component
                ),
                _get_merge_sender_uin=lambda event: "10001",
                cleanup_files=AsyncMock(),
            )
            plugin._format_count = DouyinMixin._format_count.__get__(
                plugin, DouyinMixin
            )
            plugin._build_douyin_summary = DouyinMixin._build_douyin_summary.__get__(
                plugin, DouyinMixin
            )

            await DouyinMixin._process_douyin(plugin, event, original_link)

        plugin._render_douyin_card.assert_not_awaited()
        self.assertEqual(len(event.sent), 1)
        nodes = event.sent[0].chain[0]
        first_component = nodes.nodes[0].content[0]
        self.assertIsInstance(first_component, Plain)
        self.assertIn("🎵 抖音", first_component.text)
        self.assertIn("作者：作者甲", first_component.text)
        self.assertIn("标题：图文标题", first_component.text)
        self.assertIn("点赞：1.2万", first_component.text)
        self.assertIn("评论：34", first_component.text)
        self.assertIn("媒体：图片 1 张", first_component.text)
        self.assertIn(f"链接：{original_link}", first_component.text)
        self.assertNotIn("https://www.douyin.com/note/123456789", first_component.text)

    async def test_process_xhs_force_unmerge_sends_summary_before_images(self):
        event = DummyEvent()
        original_link = (
            "https://www.xiaohongshu.com/discovery/item/abc123"
            "?app_platform=android&xsec_token=demo&share_channel=qq"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "xhs.jpg"
            image_path.write_bytes(b"x" * 2 * 1024 * 1024)

            plugin = SimpleNamespace(
                xhs_enabled=True,
                xhs_summary_mode="文字摘要",
                xhs_render_card=False,
                xhs_merge_send=False,
                xhs_max_media=99,
                xhs_concurrent_download=False,
                xhs_auto_unmerge_threshold_mb=1,
                xhs_qq_image_size_limit_mb=0,
                retry_count=0,
                max_video_size_mb=200,
                xhs_extractor=SimpleNamespace(
                    parse=AsyncMock(
                        return_value=XiaohongshuResult(
                            title="小红书标题",
                            author="作者乙",
                            text="完整正文内容",
                            image_urls=["https://example.com/xhs.jpg"],
                            file_ids=[],
                            video_url=None,
                            cover_url=None,
                            source_url="https://www.xiaohongshu.com/explore/abc123?xsec_token=demo",
                            note_id="abc123",
                        )
                    )
                ),
                _refresh_config=lambda: None,
                _send_reaction_emoji=AsyncMock(),
                _download_xhs_image=AsyncMock(return_value=image_path),
                _download_xhs_video=AsyncMock(),
                _render_xhs_card=AsyncMock(),
                _prepare_component_for_merge_send=AsyncMock(
                    side_effect=lambda component: component
                ),
                cleanup_files=AsyncMock(),
            )
            plugin._build_xhs_summary = XiaohongshuMixin._build_xhs_summary.__get__(
                plugin, XiaohongshuMixin
            )

            results = []
            async for result in XiaohongshuMixin._process_xhs(
                plugin, event, original_link
            ):
                results.append(result)

        plugin._render_xhs_card.assert_not_awaited()
        self.assertEqual(len(results), 2)
        self.assertIsInstance(results[0][0], Plain)
        self.assertIn("🍠 小红书", results[0][0].text)
        self.assertIn("正文：完整正文内容", results[0][0].text)
        self.assertIn("媒体：图片 1 张", results[0][0].text)
        self.assertIn(
            "链接：https://www.xiaohongshu.com/discovery/item/abc123",
            results[0][0].text,
        )
        self.assertNotIn("app_platform=android", results[0][0].text)
        self.assertNotIn("share_channel=qq", results[0][0].text)
        self.assertIsInstance(results[1][0], Image)

    async def test_process_bili_single_page_merge_send_uses_plain_summary_node(self):
        event = DummyEvent()
        with tempfile.TemporaryDirectory() as tmpdir:
            video_path = Path(tmpdir) / "demo.mp4"
            video_path.write_bytes(b"video")

            plugin = SimpleNamespace(
                bili_enabled=True,
                bili_summary_mode="文字摘要",
                bili_render_card=False,
                bili_merge_send=True,
                enable_multi_page=True,
                multi_page_max=3,
                bili_max_duration_seconds=300,
                quality_label="1080P高帧率",
                retry_count=0,
                error_notify_mode="静默",
                _refresh_config=lambda: None,
                _send_reaction_emoji=AsyncMock(),
                _load_cookies=lambda: None,
                _build_credential=lambda cookies: object(),
                _check_cookie_status=AsyncMock(
                    return_value=SimpleNamespace(
                        is_login=False,
                        is_vip=False,
                        vip_type=0,
                        message="游客",
                    )
                ),
                _get_video_info=AsyncMock(
                    return_value={
                        "bvid": "BV1xx411c7mD",
                        "title": "B站标题",
                        "owner": {"name": "UP主甲"},
                        "duration": 125,
                        "stat": {
                            "view": 12345,
                            "like": 678,
                            "coin": 90,
                            "share": 12,
                            "reply": 34,
                        },
                        "pic": "https://example.com/cover.jpg",
                        "pages": [{"part": "P1", "duration": 125}],
                    }
                ),
                _download_video=AsyncMock(return_value=(video_path, "1080P")),
                _assert_video_file_ready=lambda path, *_: path.stat().st_size,
                _render_bili_card=AsyncMock(),
                _prepare_component_for_merge_send=AsyncMock(
                    side_effect=lambda component: component
                ),
                _get_merge_sender_uin=lambda event: "10001",
                cleanup_files=AsyncMock(),
            )
            plugin._build_bili_summary = BilibiliMixin._build_bili_summary.__get__(
                plugin, BilibiliMixin
            )
            plugin._format_count = BilibiliMixin._format_count.__get__(
                plugin, BilibiliMixin
            )

            with patch(
                "data.plugins.astrbot_plugin_link_resolver.core.bilibili.handler.video.Video",
                return_value=object(),
            ):
                await BilibiliMixin._process_bili_video(
                    plugin,
                    event,
                    VideoRef(
                        bvid="BV1xx411c7mD",
                        avid=None,
                        page_index=0,
                        source_url="https://www.bilibili.com/video/BV1xx411c7mD",
                    ),
                )

        plugin._render_bili_card.assert_not_awaited()
        self.assertEqual(len(event.sent), 1)
        nodes = event.sent[0].chain[0]
        first_component = nodes.nodes[0].content[0]
        self.assertIsInstance(first_component, Plain)
        self.assertIn("🎬 B站", first_component.text)
        self.assertIn("标题：B站标题", first_component.text)
        self.assertIn("UP主：UP主甲", first_component.text)
        self.assertIn("播放：1.2万", first_component.text)
        self.assertIn("点赞：678", first_component.text)
        self.assertIn("投币：90", first_component.text)
        self.assertIn("分享：12", first_component.text)
        self.assertIn("评论：34", first_component.text)
        self.assertIn("画质：1080P", first_component.text)
        self.assertIn(
            "链接：https://www.bilibili.com/video/BV1xx411c7mD", first_component.text
        )
        self.assertNotIn("链接：https://b23.tv/", first_component.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
