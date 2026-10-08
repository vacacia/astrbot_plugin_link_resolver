"""验证 YouTube 链接识别及 yt-dlp 下载边界."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from youtube import (
    YoutubeDownloadError,
    YoutubeExtractor,
    YoutubeParseError,
    extract_youtube_links,
    is_youtube_403_error,
)


class FakeYoutubeDL:
    options: dict = {}

    def __init__(self, options):
        type(self).options = options

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def extract_info(self, url, download=False):
        info = {
            "id": "RHXOhUKP5Y0",
            "title": "Demo",
            "uploader": "Example",
            "duration": 12,
            "webpage_url": url,
        }
        if download:
            path = Path(
                self.options["outtmpl"]
                .replace("%(id)s", info["id"])
                .replace("%(ext)s", "mp4")
            )
            path.write_bytes(b"video")
        return info

    def prepare_filename(self, info):
        return (
            self.options["outtmpl"]
            .replace("%(id)s", info["id"])
            .replace("%(ext)s", "mp4")
        )


class TestYoutubeExtractor(unittest.TestCase):
    def test_link_matching_accepts_youtube_variants_and_excludes_other_hosts(self):
        links = extract_youtube_links(
            "https://youtu.be/RHXOhUKP5Y0?t=1 "
            "youtube.com/shorts/abc https://youtube.com.example/video"
        )
        self.assertEqual(
            links,
            [
                "https://youtu.be/RHXOhUKP5Y0?t=1",
                "https://youtube.com/shorts/abc",
            ],
        )

    def test_rejects_non_youtube_link(self):
        with self.assertRaises(YoutubeParseError):
            YoutubeExtractor._validate_url("https://example.com/video")

    def test_youtube_403_detection_is_specific_to_stream_denials(self):
        self.assertTrue(
            is_youtube_403_error(
                "ERROR: unable to download video data: HTTP Error 403: Forbidden"
            )
        )
        self.assertFalse(is_youtube_403_error("HTTP Error 429: Too Many Requests"))

    def test_compatibility_client_is_forwarded_to_yt_dlp(self):
        options = YoutubeExtractor()._base_options(
            None,
            player_client="web_embedded",
        )
        self.assertEqual(
            options["extractor_args"],
            {
                "youtube": {
                    "player_client": ["web_embedded"],
                }
            },
        )

    def test_netscape_cookie_file_is_forwarded_to_yt_dlp(self):
        with tempfile.TemporaryDirectory() as directory:
            cookie_file = Path(directory) / "cookies.txt"
            cookie_file.write_text(
                "# Netscape HTTP Cookie File\n"
                ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\texample\n",
                encoding="utf-8",
            )
            options = YoutubeExtractor()._base_options(str(cookie_file))
            self.assertEqual(options["cookiefile"], str(cookie_file))

    def test_download_uses_limit_and_returns_media_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "youtube.extractor._get_yt_dlp_class", return_value=FakeYoutubeDL
            ):
                result, output = asyncio.run(
                    YoutubeExtractor().download(
                        "https://youtu.be/RHXOhUKP5Y0",
                        Path(directory),
                        "unit",
                        max_height=720,
                        max_bytes=1024,
                    )
                )
        self.assertEqual(result.video_id, "RHXOhUKP5Y0")
        self.assertEqual(output.suffix, ".mp4")
        self.assertEqual(FakeYoutubeDL.options["max_filesize"], 1024)
        self.assertIn("format", FakeYoutubeDL.options)

    def test_h264_selector_is_the_default_preference(self):
        selector = YoutubeExtractor._build_format_selector(
            max_height=720, video_codec="h264", ffmpeg_available=True
        )
        self.assertTrue(selector.startswith("bv*[vcodec^=avc1][ext=mp4][height<=720]"))
        self.assertIn("+ba[ext=m4a]", selector)

    def test_8k_height_limit_is_forwarded_to_the_format_selector(self):
        selector = YoutubeExtractor._build_format_selector(
            max_height=4320, video_codec="h264", ffmpeg_available=True
        )
        self.assertIn("[height<=4320]", selector)

    def test_av1_selector_can_be_selected(self):
        selector = YoutubeExtractor._build_format_selector(
            max_height=720, video_codec="av1", ffmpeg_available=False
        )
        self.assertTrue(selector.startswith("b[vcodec^=av01][ext=mp4][height<=720]"))
        self.assertNotIn("+ba[ext=m4a]", selector)

    def test_failed_download_removes_partial_streams(self):
        class FailedYoutubeDL(FakeYoutubeDL):
            def extract_info(self, url, download=False):
                Path(
                    self.options["outtmpl"]
                    .replace("%(id)s", "partial")
                    .replace("%(ext)s", "mp4.part")
                ).write_bytes(b"partial")
                raise RuntimeError("HTTP Error 403: Forbidden")

        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "youtube.extractor._get_yt_dlp_class", return_value=FailedYoutubeDL
            ):
                with self.assertRaises(YoutubeDownloadError):
                    asyncio.run(
                        YoutubeExtractor().download(
                            "https://youtu.be/RHXOhUKP5Y0", Path(directory), "failed"
                        )
                    )
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_cancelled_download_waits_for_cleanup(self):
        started = threading.Event()

        class SlowYoutubeDL(FakeYoutubeDL):
            def extract_info(self, url, download=False):
                Path(
                    self.options["outtmpl"]
                    .replace("%(id)s", "partial")
                    .replace("%(ext)s", "mp4.part")
                ).write_bytes(b"partial")
                started.set()
                while True:
                    self.options["progress_hooks"][0]({})
                    time.sleep(0.005)

        async def cancel_download(directory):
            task = asyncio.create_task(
                YoutubeExtractor().download(
                    "https://youtu.be/RHXOhUKP5Y0", Path(directory), "cancelled"
                )
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "youtube.extractor._get_yt_dlp_class", return_value=SlowYoutubeDL
            ):
                asyncio.run(cancel_download(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_without_ffmpeg_selects_video_with_audio(self):
        try:
            from yt_dlp import YoutubeDL
        except ImportError:
            self.skipTest("需要 yt-dlp 验证真实格式选择器")
        formats = [
            {
                "format_id": "combined",
                "url": "https://example.com/combined",
                "ext": "mp4",
                "height": 360,
                "vcodec": "avc1",
                "acodec": "mp4a",
            },
            {
                "format_id": "silent",
                "url": "https://example.com/silent",
                "ext": "mp4",
                "height": 720,
                "vcodec": "avc1",
                "acodec": "none",
            },
        ]
        selector = YoutubeExtractor._build_format_selector(
            max_height=720, video_codec="h264", ffmpeg_available=False
        )
        with YoutubeDL({"quiet": True, "format": selector}) as ydl:
            info = ydl.process_ie_result(
                {"id": "sample", "title": "sample", "formats": formats}, download=False
            )
        self.assertEqual(info["format_id"], "combined")


if __name__ == "__main__":
    unittest.main(verbosity=2)
