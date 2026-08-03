"""Simulate Bilibili output-path race in a deterministic way.

Run from the AcaBot repo root:
    .venv/bin/python -m pytest extensions/plugins/link_resolver/tests/test_bilibili_race_simulation.py -q
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from plugins.link_resolver.core.bilibili import handler as bili_handler
from plugins.link_resolver.core.bilibili.handler import BilibiliMixin


class _DummyHarness(BilibiliMixin):
    """Minimal harness that only exercises _download_video path behavior."""

    def __init__(self, cache_dir: Path):
        self._cache_dir = cache_dir
        self.video_quality = SimpleNamespace(name="_720P", value=64)
        self.max_video_size_mb = 200
        self.allow_quality_fallback = True
        self._download_latch: asyncio.Event | None = None

    async def _select_streams(self, video_obj, page_index, video_quality=None):
        stream = SimpleNamespace(
            url="https://example.com/fake-video",
            video_quality=self.video_quality,
            video_codecs="AVC",
        )
        return stream, None, 5.0

    async def _download_stream(
        self,
        url: str,
        output_path: Path,
        cookies: dict | None,
        max_bytes: int | None,
        headers: dict | None = None,
        retries: int = 3,
    ) -> int:
        del url, cookies, max_bytes, headers, retries
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if self._download_latch is not None:
            await self._download_latch.wait()
        output_path.write_bytes(b"fake-video-data")
        return output_path.stat().st_size

    async def _merge_av(self, v_path: Path, a_path: Path, output_path: Path) -> None:
        del v_path, a_path
        output_path.write_bytes(b"merged")

    async def cleanup_files(self, video_paths: list[Path], thumbnail_paths: list[Path]) -> None:
        del thumbnail_paths
        for video_path in video_paths:
            video_path.unlink(missing_ok=True)


class TestBilibiliRaceSimulation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="bili-race-sim-")
        self.cache_dir = Path(self._temp_dir.name)
        self.harness = _DummyHarness(self.cache_dir)
        self._orig_get_bili_video_path = bili_handler.get_bilibili_video_path
        bili_handler.get_bilibili_video_path = lambda: self.cache_dir

    async def asyncTearDown(self) -> None:
        bili_handler.get_bilibili_video_path = self._orig_get_bili_video_path
        self._temp_dir.cleanup()

    async def _run_two_downloads(self, request_a: str, request_b: str) -> tuple[Path, Path]:
        self.harness._download_latch = asyncio.Event()

        async def _worker(req_id: str) -> Path:
            path, _quality = await self.harness._download_video(
                video_obj=object(),
                bvid="BV1ntFdzrEXR",
                page_index=0,
                page_count=1,
                cookies={},
                request_id=req_id,
            )
            return path

        task_a = asyncio.create_task(_worker(request_a))
        task_b = asyncio.create_task(_worker(request_b))
        await asyncio.sleep(0.05)
        self.harness._download_latch.set()
        path_a, path_b = await asyncio.gather(task_a, task_b)
        return path_a, path_b

    async def test_legacy_shared_path_can_break_second_sender(self):
        """Shared output path means one cleanup can remove peer's send target."""
        path_a, path_b = await self._run_two_downloads("legacy", "legacy")
        self.assertEqual(path_a, path_b)

        await self.harness.cleanup_files([path_a], [])
        with self.assertRaises(FileNotFoundError):
            self.harness._assert_video_file_ready(path_b, source_tag="", request_id="legacy")

    async def test_unique_output_path_isolated_between_tasks(self):
        """Unique request_id keeps each task's output independent."""
        path_a, path_b = await self._run_two_downloads("reqA1234", "reqB5678")
        self.assertNotEqual(path_a, path_b)

        await self.harness.cleanup_files([path_a], [])
        size_b = self.harness._assert_video_file_ready(path_b, source_tag="", request_id="reqB5678")
        self.assertGreater(size_b, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
