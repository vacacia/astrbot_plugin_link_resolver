# ruff: noqa: E402
"""验证 YouTube 回退重试, 时长限制及发送后的资源清理."""

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

from data.plugins.astrbot_plugin_link_resolver.core.youtube import (
    YoutubeDownloadError,
    YoutubeResult,
)
from data.plugins.astrbot_plugin_link_resolver.core.youtube.handler import YoutubeMixin


class TestYoutubeHandler(unittest.IsolatedAsyncioTestCase):
    def make_handler(self, directory, retry_count=0):
        path = Path(directory) / "video.mp4"
        path.write_bytes(b"video")
        result = YoutubeResult("sample", "示例", "作者", 12, "https://youtu.be/sample")
        handler = YoutubeMixin()
        handler._refresh_config = Mock()
        handler.youtube_enabled = True
        handler.youtube_player_client = "default"
        handler.youtube_max_duration_seconds = 300
        handler.youtube_merge_send = False
        handler.retry_count = retry_count
        handler._send_reaction_emoji = AsyncMock()
        handler._inspect_youtube_video = AsyncMock(return_value=result)
        handler._download_youtube_video = AsyncMock(return_value=(result, path))
        handler.cleanup_files = AsyncMock()
        event = SimpleNamespace(
            send=AsyncMock(), set_result=Mock(), plain_result=lambda text: text
        )
        return handler, event, result, path

    async def test_403_fallback_runs_when_retries_are_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            handler, event, result, path = self.make_handler(directory)
            handler._download_youtube_video.side_effect = [
                YoutubeDownloadError("HTTP Error 403: Forbidden"),
                (result, path),
            ]
            await handler._process_youtube(event, result.source_url)
            clients = [
                call.args[2] for call in handler._download_youtube_video.await_args_list
            ]
            self.assertEqual(clients, ["default", "web_embedded"])
            event.send.assert_awaited_once()
            handler.cleanup_files.assert_awaited_once_with([path], [])

    async def test_403_fallback_runs_after_last_regular_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            handler, event, result, path = self.make_handler(directory, retry_count=1)
            handler._download_youtube_video.side_effect = [
                YoutubeDownloadError("temporary failure"),
                YoutubeDownloadError("HTTP Error 403: Forbidden"),
                (result, path),
            ]
            await handler._process_youtube(event, result.source_url)
            self.assertEqual(
                [
                    call.args[2]
                    for call in handler._download_youtube_video.await_args_list
                ],
                ["default", "default", "web_embedded"],
            )
            event.send.assert_awaited_once()

    async def test_duration_limit_does_not_download(self):
        with tempfile.TemporaryDirectory() as directory:
            handler, event, result, _ = self.make_handler(directory)
            result.duration = 301
            await handler._process_youtube(event, result.source_url)
            handler._download_youtube_video.assert_not_awaited()
            event.set_result.assert_called_once()

    async def test_send_failure_still_cleans_video(self):
        with tempfile.TemporaryDirectory() as directory:
            handler, event, result, path = self.make_handler(directory)
            event.send.side_effect = RuntimeError("发送失败")
            with self.assertRaises(RuntimeError):
                await handler._process_youtube(event, result.source_url)
            handler.cleanup_files.assert_awaited_once_with([path], [])
