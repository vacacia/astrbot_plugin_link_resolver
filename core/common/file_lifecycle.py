"""媒体文件写入与清理的生命周期工具."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


async def save_image_file(image: Any, output_path: Path, **save_options: Any) -> None:
    """保存图片, 并避免线程写入在任务取消后留下文件."""

    save_task = asyncio.create_task(
        asyncio.to_thread(image.save, output_path, **save_options)
    )
    try:
        await asyncio.shield(save_task)
    except asyncio.CancelledError:
        while not save_task.done():
            try:
                await asyncio.shield(save_task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        try:
            save_task.result()
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            output_path.unlink(missing_ok=True)
        raise
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
