"""超限回复模板：默认句不变，占位符可填，留空则不发送。"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace


def _load():
    path = Path(__file__).resolve().parents[1] / "core" / "common" / "reply_templates.py"
    spec = importlib.util.spec_from_file_location("reply_templates", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


reply_templates = _load()


class TestReplyTemplates(unittest.TestCase):
    def test_missing_template_keeps_the_old_sentence(self):
        owner = SimpleNamespace()
        self.assertEqual(
            reply_templates.duration_limit_reply(owner, 601, 600),
            "视频太长了你自己看去",
        )
        owner.max_video_size_mb = 90
        self.assertEqual(
            reply_templates.size_limit_reply(owner),
            "我没流量了, 看不了那么大的视频",
        )

    def test_duration_placeholders_use_ceil_and_floor_minutes(self):
        owner = SimpleNamespace(
            duration_limit_reply=(
                "这条大概有 {video_minutes} 分钟，"
                "我这边最长只能转 {limit_minutes} 分钟。"
            )
        )
        self.assertEqual(
            reply_templates.duration_limit_reply(owner, 601, 600),
            "这条大概有 11 分钟，我这边最长只能转 10 分钟。",
        )
        self.assertEqual(
            reply_templates.duration_limit_reply(owner, 301, 300),
            "这条大概有 6 分钟，我这边最长只能转 5 分钟。",
        )

    def test_size_placeholder_reads_the_configured_limit(self):
        owner = SimpleNamespace(
            max_video_size_mb=90,
            size_limit_reply="这个有点大，我这边最多转 {limit_mb}MB。",
        )
        self.assertEqual(
            reply_templates.size_limit_reply(owner),
            "这个有点大，我这边最多转 90MB。",
        )

    def test_blank_template_sends_nothing(self):
        owner = SimpleNamespace(
            duration_limit_reply="  ",
            size_limit_reply="",
            max_video_size_mb=90,
        )
        self.assertIsNone(reply_templates.duration_limit_reply(owner, 601, 600))
        self.assertIsNone(reply_templates.size_limit_reply(owner))

    def test_unknown_placeholder_is_left_in_place(self):
        text = reply_templates.render_reply_template(
            "时长 {video_minutes}，备注 {note}",
            "fallback",
            video_minutes=11,
        )
        self.assertEqual(text, "时长 11，备注 {note}")

    def test_broken_braces_fall_back_to_the_raw_template(self):
        self.assertEqual(
            reply_templates.render_reply_template("未闭合 {", "fallback"),
            "未闭合 {",
        )


if __name__ == "__main__":
    unittest.main()
