import asyncio
import threading
from pathlib import Path

import pytest
from plugins.link_resolver.core.common.file_lifecycle import save_image_file


@pytest.mark.asyncio
async def test_save_image_file_removes_late_write_after_cancellation(tmp_path):
    save_started = threading.Event()
    allow_save = threading.Event()
    output_path = tmp_path / "card.png"

    class SlowImage:
        def save(self, path: Path, **_kwargs) -> None:
            save_started.set()
            allow_save.wait(timeout=2)
            path.write_bytes(b"late card")

    save_task = asyncio.create_task(save_image_file(SlowImage(), output_path))
    assert await asyncio.to_thread(save_started.wait, 1)

    save_task.cancel()
    allow_save.set()

    with pytest.raises(asyncio.CancelledError):
        await save_task

    assert not output_path.exists()


@pytest.mark.asyncio
async def test_save_image_file_preserves_cancellation_when_late_save_fails(tmp_path):
    save_started = threading.Event()
    allow_save = threading.Event()
    output_path = tmp_path / "card.png"

    class FailingImage:
        def save(self, _path: Path, **_kwargs) -> None:
            save_started.set()
            allow_save.wait(timeout=2)
            raise RuntimeError("save failed")

    save_task = asyncio.create_task(save_image_file(FailingImage(), output_path))
    assert await asyncio.to_thread(save_started.wait, 1)

    save_task.cancel()
    allow_save.set()

    with pytest.raises(asyncio.CancelledError):
        await save_task

    assert not output_path.exists()


@pytest.mark.asyncio
async def test_save_image_file_removes_late_write_after_repeated_cancellation(tmp_path):
    save_started = threading.Event()
    allow_save = threading.Event()
    save_finished = threading.Event()
    output_path = tmp_path / "card.png"

    class SlowImage:
        def save(self, path: Path, **_kwargs) -> None:
            save_started.set()
            allow_save.wait(timeout=2)
            path.write_bytes(b"late card")
            save_finished.set()

    save_task = asyncio.create_task(save_image_file(SlowImage(), output_path))
    assert await asyncio.to_thread(save_started.wait, 1)

    save_task.cancel()
    await asyncio.sleep(0)
    save_task.cancel()
    allow_save.set()

    with pytest.raises(asyncio.CancelledError):
        await save_task

    assert await asyncio.to_thread(save_finished.wait, 1)
    assert not output_path.exists()
