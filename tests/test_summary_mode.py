# ruff: noqa: E402
"""Tests for per-platform summary_mode behavior.

Run from the AcaBot repo root:
    .venv/bin/python -m pytest extensions/plugins/link_resolver/tests/test_summary_mode.py -q
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import Image, Plain
from plugins.link_resolver.core.bilibili.handler import (
    BilibiliMixin,
    VideoRef,
)
from plugins.link_resolver.core.douyin import DouyinResult
from plugins.link_resolver.core.douyin.handler import DouyinMixin
from plugins.link_resolver.core.xiaohongshu import XiaohongshuResult
from plugins.link_resolver.core.xiaohongshu.handler import (
    XiaohongshuMixin,
)
from plugins.link_resolver.main import LinkResolver


class DummyEvent:
    def __init__(self, *, tool_invocation: bool = False):
        self.sent = []
        self.result = None
        self._tool_invocation = tool_invocation

    async def send(self, chain):
        self.sent.append(chain)

    def chain_result(self, chain):
        return chain

    def plain_result(self, text: str):
        return text

    def set_result(self, result):
        self.result = result

    def is_tool_invocation(self) -> bool:
        return self._tool_invocation


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


class TestSummaryModeHandlers(unittest.IsolatedAsyncioTestCase):
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

    async def test_process_xhs_over_limit_asks_before_expanding_images(self):
        """自动解析超过体积阈值时只发送确认提示."""

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
        self.assertEqual(
            results,
            ["这篇有 1 张图片, 共 2.00 MB, 全部展开会很大喵, 真的要解析吗?"],
        )
        plugin.cleanup_files.assert_awaited_once_with([image_path], [])

    async def test_process_xhs_tool_invocation_force_unmerges_over_limit(self):
        """工具解析超过体积阈值时继续逐张发送."""

        event = DummyEvent(tool_invocation=True)
        original_link = "https://www.xiaohongshu.com/discovery/item/abc123"
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
                            source_url=original_link,
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

        self.assertEqual(len(results), 2)
        self.assertIsInstance(results[0][0], Plain)
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
                "plugins.link_resolver.core.bilibili.handler.video.Video",
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
