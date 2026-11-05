# ruff: noqa: E402
"""验证通用站点(yt-dlp 兜底): 提取器匹配, 禁用 generic 提取器, 重试策略和发送清理."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

for candidate in Path(__file__).resolve().parents:
    if (candidate / "data" / "plugins").exists():
        sys.path.insert(0, str(candidate))
        break

from data.plugins.astrbot_plugin_link_resolver.core.generic import GenericExtractor
from data.plugins.astrbot_plugin_link_resolver.core.generic.handler import (
    GenericMixin,
)
from data.plugins.astrbot_plugin_link_resolver.core.registry import classify_links
from data.plugins.astrbot_plugin_link_resolver.core.youtube import (
    YoutubeDownloadError,
    YoutubeParseError,
    YoutubeResult,
)


class TestGenericExtractor(unittest.TestCase):
    def test_accepts_links_known_by_ytdlp_extractors(self):
        extractor = GenericExtractor()
        self.assertTrue(extractor.accepts("https://vm.tiktok.com/ZMabc/"))
        self.assertTrue(extractor.accepts("https://www.instagram.com/reel/abc/"))
        self.assertTrue(extractor.accepts("https://www.nicovideo.jp/watch/sm9"))

    def test_rejects_unknown_sites_ip_and_localhost(self):
        extractor = GenericExtractor()
        self.assertFalse(extractor.accepts("https://example.com/news/article"))
        self.assertFalse(extractor.accepts("http://192.168.1.1/admin"))
        self.assertFalse(extractor.accepts("http://[::1]:8080/x"))
        self.assertFalse(extractor.accepts("http://localhost/x"))
        self.assertFalse(extractor.accepts("https://tiktok.com.evil.com/x"))

    def test_rejects_numeric_ip_variants(self):
        """127.1 / 十进制 / 十六进制等 ipaddress 不认识的 IP 写法也要拒绝."""
        extractor = GenericExtractor()
        for url in (
            "http://127.1/x",
            "http://127.0.0.1:6185/x",
            "http://2130706433/x",
            "http://0x7f.0.0.1/x",
            "http://0177.0.0.1/x",
        ):
            self.assertFalse(extractor.accepts(url), url)

    def test_forwarding_link_with_nested_target_is_rejected(self):
        """转发型链接(如 facebook flx/warn/?u=...)的目标主机也要过入口校验."""
        extractor = GenericExtractor()
        # u= 指向内网地址
        self.assertFalse(
            extractor.accepts(
                "https://www.facebook.com/flx/warn/"
                "?u=http%3A%2F%2F127.0.0.1%3A6185%2FMediasite%2FPlay%2Fx"
            )
        )
        # u= 指向被排除的专用平台
        self.assertFalse(
            extractor.accepts(
                "https://www.facebook.com/flx/warn/"
                "?u=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3D1"
            )
        )

    def test_dedicated_platforms_never_taken_over(self):
        extractor = GenericExtractor()
        for url in (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://www.bilibili.com/video/BV1xx411c7mD",
            "https://x.com/a/status/1",
            "https://www.douyin.com/video/7123456789012345678",
        ):
            self.assertFalse(extractor.accepts(url), url)

    def test_user_blocked_domains_take_effect(self):
        extractor = GenericExtractor(blocked_domains=["https://*.TikTok.com/path", ""])
        self.assertFalse(extractor.accepts("https://vm.tiktok.com/ZMabc/"))
        self.assertTrue(extractor.accepts("https://www.instagram.com/reel/abc/"))
        extractor.set_blocked_domains([])
        self.assertTrue(extractor.accepts("https://vm.tiktok.com/ZMabc/"))

    def test_extract_links_stops_at_cjk_and_dedupes(self):
        extractor = GenericExtractor()
        links = extractor.extract_links(
            "看这个https://vm.tiktok.com/ZMabc/太好笑了 https://vm.tiktok.com/ZMabc/ "
            "https://example.com/x"
        )
        self.assertEqual(links, ["https://vm.tiktok.com/ZMabc/"])

    def test_options_disable_generic_extractor(self):
        options = GenericExtractor()._base_options(None, player_client="web_embedded")
        self.assertEqual(options["allowed_extractors"][:2], ["default", "-generic"])
        self.assertNotIn("extractor_args", options)

    def test_dedicated_platform_extractors_are_not_loaded(self):
        from yt_dlp import YoutubeDL

        options = GenericExtractor()._base_options(None)
        with YoutubeDL({"quiet": True, **options}) as ydl:
            names = [ie.IE_NAME.lower() for ie in ydl._ies.values()]
        self.assertNotIn("generic", names)
        for keyword in ("youtube", "bili", "douyin", "twitter", "weibo", "xhs"):
            self.assertFalse([n for n in names if keyword in n], keyword)
        self.assertIn("tiktok", names)
        self.assertIn("instagram", names)

    def test_playlist_live_and_audio_only_are_rejected(self):
        extractor = GenericExtractor()
        for info in (
            {"id": "u", "_type": "playlist"},
            {"id": "l", "is_live": True},
            {"id": "a", "formats": [{"vcodec": "none"}, {"vcodec": "none"}]},
            # 单格式提取器没有 formats 列表, 顶层 vcodec="none" 也是仅音频
            {"id": "s", "vcodec": "none"},
        ):
            with self.assertRaises(YoutubeParseError):
                extractor._result_from_info(info, "https://x")
        ok = extractor._result_from_info(
            {"id": "v", "title": "t", "formats": [{"vcodec": "none"}, {}]},
            "https://x",
        )
        self.assertEqual(ok.video_id, "v")

    def test_redirect_to_excluded_platform_is_rejected(self):
        extractor = GenericExtractor()
        with self.assertRaises(YoutubeParseError):
            extractor._result_from_info(
                {"id": "yt", "webpage_url": "https://www.youtube.com/watch?v=1"},
                "https://www.facebook.com/flx/warn/?u=https%3A%2F%2Fwww.youtube.com",
            )

    def test_validate_url_rejects_unsupported(self):
        with self.assertRaises(YoutubeParseError):
            GenericExtractor()._validate_url("https://example.com/v")

    def test_options_limit_playlist_to_one_item(self):
        options = GenericExtractor()._base_options(None)
        self.assertEqual(options["playlist_items"], "1")
        self.assertNotIn("max_downloads", options)

    def test_warm_up_precompiles_extractor_regex(self):
        """预热后提取器的匹配正则应已编译挂到类上, 首个链接不用再等几百毫秒."""
        from data.plugins.astrbot_plugin_link_resolver.core.generic.extractor import (
            _dedicated_extractor_classes,
        )

        GenericExtractor.warm_up()
        classes = _dedicated_extractor_classes()
        self.assertGreater(len(classes), 1000)
        compiled = sum("_VALID_URL_RE" in ie.__dict__ for ie in classes)
        self.assertGreater(compiled, len(classes) // 2)

    def test_classify_uses_filter_for_generic(self):
        extractor = GenericExtractor()
        grouped = classify_links(
            ["https://vm.tiktok.com/ZMabc/", "https://example.com/x"],
            link_filters={"generic": extractor.accepts},
        )
        self.assertEqual(grouped, {"generic": ["https://vm.tiktok.com/ZMabc/"]})


class TestFormatSelector(unittest.TestCase):
    """用 yt-dlp 真实的选择器逻辑验证: 先保证能解析, 再尽量贴近分辨率上限."""

    @staticmethod
    def pick(heights, max_height=720, ffmpeg=True):
        """heights 里每项是高度或 (高度, 容器) 元组, 返回选中的 format_id."""
        from yt_dlp import YoutubeDL

        formats = []
        for item_spec in heights:
            height, ext = (
                item_spec if isinstance(item_spec, tuple) else (item_spec, "mp4")
            )
            item = {
                "format_id": f"{ext}{height}",
                "ext": ext,
                "vcodec": "avc1",
                "acodec": "mp4a",
                "protocol": "https",
                "url": f"http://x/{ext}{height}",
            }
            if height:
                item["height"] = height
            formats.append(item)
        selector = GenericExtractor._build_format_selector(
            max_height=max_height, video_codec="h264", ffmpeg_available=ffmpeg
        )
        chosen = list(
            YoutubeDL({"quiet": True}).build_format_selector(selector)(
                {"formats": formats, "incomplete_formats": False}
            )
        )
        return chosen[0]["format_id"] if chosen else None

    def test_prefers_highest_not_above_limit(self):
        self.assertEqual(self.pick([360, 720, 1080]), "mp4720")
        self.assertEqual(self.pick([360, 1080]), "mp4360")

    def test_resolution_beats_container(self):
        """分辨率优先于容器: 720 webm 胜过 360 mp4."""
        self.assertEqual(self.pick([(360, "mp4"), (720, "webm")]), "webm720")
        self.assertEqual(
            self.pick([(360, "mp4"), (720, "webm"), (1080, "mp4")]), "webm720"
        )

    def test_falls_back_to_closest_above_limit_instead_of_failing(self):
        self.assertEqual(self.pick([1080]), "mp41080")
        self.assertEqual(self.pick([1080, 2160]), "mp41080")
        self.assertEqual(self.pick([2160]), "mp42160")

    def test_lower_than_limit_and_unknown_height_still_parse(self):
        self.assertEqual(self.pick([360]), "mp4360")
        self.assertEqual(self.pick([0]), "mp40")

    def test_without_ffmpeg_and_unlimited(self):
        self.assertEqual(self.pick([360, 720, 1080], ffmpeg=False), "mp4720")
        self.assertEqual(self.pick([360, 720, 1080], max_height=0), "mp41080")


class TestGenericHandler(unittest.IsolatedAsyncioTestCase):
    def make_handler(self, directory, retry_count=2):
        path = Path(directory) / "video.mp4"
        path.write_bytes(b"video")
        result = YoutubeResult("sample", "示例", "作者", 12, "https://vm.tiktok.com/x")
        handler = GenericMixin()
        handler._refresh_config = Mock()
        handler.generic_enabled = True
        handler.generic_max_duration_seconds = 300
        handler.generic_max_height = 720
        handler.generic_merge_send = False
        handler.max_video_size_mb = 250
        handler.retry_count = retry_count
        handler.error_notify_mode = "静默"
        handler.generic_cookies_file = None
        handler._send_reaction_emoji = AsyncMock()
        handler.cleanup_files = AsyncMock()
        handler.generic_extractor = SimpleNamespace(
            inspect=AsyncMock(return_value=result),
            download=AsyncMock(return_value=(result, path)),
        )
        event = SimpleNamespace(
            send=AsyncMock(), set_result=Mock(), plain_result=lambda text: text
        )
        return handler, event, result, path

    async def test_success_sends_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory:
            handler, event, result, path = self.make_handler(directory)
            await handler._process_generic(event, result.source_url)
            event.send.assert_awaited_once()
            handler.cleanup_files.assert_awaited_once_with([path], [])

    async def test_unsupported_url_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            handler, event, result, _ = self.make_handler(directory, retry_count=3)
            handler.generic_extractor.inspect.side_effect = YoutubeDownloadError(
                "通用解析 信息获取失败: Unsupported URL: https://x"
            )
            await handler._process_generic(event, result.source_url)
            self.assertEqual(handler.generic_extractor.inspect.await_count, 1)
            event.send.assert_not_awaited()

    async def test_failure_notifies_according_to_mode(self):
        for mode, expected in (
            ("静默", None),
            ("脱敏", "❌ 通用解析失败"),
            ("报错", "boom"),
        ):
            with tempfile.TemporaryDirectory() as directory:
                handler, event, result, _ = self.make_handler(directory, retry_count=0)
                handler.error_notify_mode = mode
                handler.generic_extractor.inspect.side_effect = YoutubeDownloadError(
                    "boom"
                )
                await handler._process_generic(event, result.source_url)
                if expected is None:
                    event.set_result.assert_not_called()
                else:
                    self.assertIn(expected, event.set_result.call_args.args[0])

    async def test_oversize_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            handler, event, result, _ = self.make_handler(directory, retry_count=3)
            handler.generic_extractor.download.side_effect = YoutubeDownloadError(
                "下载后的视频超过大小限制"
            )
            await handler._process_generic(event, result.source_url)
            self.assertEqual(handler.generic_extractor.download.await_count, 1)
            event.send.assert_not_awaited()

    async def test_oversize_early_abort_is_not_retried(self):
        """yt-dlp 因 Content-Length 超限提前中止(没产出文件)时也不重试."""
        for error in (
            "通用解析 下载失败: File is larger than max-filesize (136 bytes > 100 bytes)",
            "通用解析 下载失败: yt-dlp 未生成可发送的视频文件",
        ):
            with tempfile.TemporaryDirectory() as directory:
                handler, event, result, _ = self.make_handler(directory, retry_count=3)
                handler.generic_extractor.download.side_effect = YoutubeDownloadError(
                    error
                )
                await handler._process_generic(event, result.source_url)
                self.assertEqual(
                    handler.generic_extractor.download.await_count, 1, error
                )

    async def test_transient_error_retries_then_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            handler, event, result, path = self.make_handler(directory)
            handler.generic_extractor.download.side_effect = [
                YoutubeDownloadError("HTTP Error 500"),
                (result, path),
            ]
            await handler._process_generic(event, result.source_url)
            self.assertEqual(handler.generic_extractor.download.await_count, 2)
            event.send.assert_awaited_once()

    async def test_too_long_is_rejected_before_download(self):
        with tempfile.TemporaryDirectory() as directory:
            handler, event, result, _ = self.make_handler(directory)
            handler.generic_max_duration_seconds = 5
            await handler._process_generic(event, result.source_url)
            handler.generic_extractor.download.assert_not_awaited()
            event.set_result.assert_called_once()

    async def test_disabled_does_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            handler, event, result, _ = self.make_handler(directory)
            handler.generic_enabled = False
            await handler._process_generic(event, result.source_url)
            handler.generic_extractor.inspect.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
