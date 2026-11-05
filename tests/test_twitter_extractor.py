# ruff: noqa: E402
"""Unit tests for the Twitter/X extractor.

Run inside AstrBot container:
    cd /AstrBot
    python /AstrBot/data/plugins/astrbot_plugin_link_resolver/tests/test_twitter_extractor.py -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

for candidate in Path(__file__).resolve().parents:
    if (candidate / "data" / "plugins").exists():
        root_path = str(candidate)
        if root_path not in sys.path:
            sys.path.insert(0, root_path)
        break

from data.plugins.astrbot_plugin_link_resolver.core.twitter import (
    TwitterExtractor,
    extract_twitter_links,
)


class TestTwitterExtractor(unittest.IsolatedAsyncioTestCase):
    def test_extract_twitter_links_variants_and_dedup_by_tweet_id(self):
        text = (
            "先看 https://twitter.com/demo/status/1234567890123456789 "
            "再看同一条 https://x.com/demo/status/1234567890123456789?s=20 "
            "还有另一条 x.com/another/status/9876543210987654321 "
            "以及 https://twitter.com/i/web/status/112233445566778899"
        )

        links = extract_twitter_links(text)

        self.assertEqual(len(links), 3)
        self.assertIn("https://twitter.com/demo/status/1234567890123456789", links)
        self.assertIn("https://x.com/another/status/9876543210987654321", links)
        self.assertIn("https://twitter.com/i/web/status/112233445566778899", links)

    def test_build_result_maps_image_tweet_fields(self):
        extractor = TwitterExtractor()
        payload = {
            "tweet": {
                "text": "图片正文",
                "created_at": "Tue Apr 01 12:34:56 +0000 2025",
                "author": {"name": "Alice", "screen_name": "alice"},
                "media": {
                    "photos": [
                        {"url": "https://pbs.twimg.com/media/1.jpg"},
                        {"url": "https://pbs.twimg.com/media/2.jpg"},
                    ]
                },
            }
        }

        result = extractor._build_result(
            payload, "https://x.com/alice/status/1234567890123456789"
        )

        self.assertEqual(result.text, "图片正文")
        self.assertEqual(result.author, "Alice(@alice)")
        self.assertEqual(result.created_at, "2025-04-01")
        self.assertEqual(
            result.image_urls,
            [
                "https://pbs.twimg.com/media/1.jpg",
                "https://pbs.twimg.com/media/2.jpg",
            ],
        )
        self.assertEqual(result.video_urls, [])

    def test_build_result_maps_single_video_tweet_fields(self):
        extractor = TwitterExtractor()
        payload = {
            "tweet": {
                "text": "视频正文",
                "created_at": "Fri May 02 01:02:03 +0000 2025",
                "author": {"name": "Bob", "screen_name": "bob"},
                "media": {
                    "videos": [
                        {
                            "url": "https://video.twimg.com/ext_tw_video/demo.mp4",
                            "thumbnail_url": "https://pbs.twimg.com/media/cover.jpg",
                        }
                    ]
                },
            }
        }

        result = extractor._build_result(
            payload, "https://twitter.com/bob/status/1234567890123456789"
        )

        self.assertEqual(result.text, "视频正文")
        self.assertEqual(result.author, "Bob(@bob)")
        self.assertEqual(result.created_at, "2025-05-02")
        self.assertEqual(
            result.video_urls, ["https://video.twimg.com/ext_tw_video/demo.mp4"]
        )
        self.assertEqual(result.image_urls, [])

    def test_build_result_keeps_multiple_videos_and_images_for_mixed_media(self):
        extractor = TwitterExtractor()
        payload = {
            "tweet": {
                "text": "混合媒体正文",
                "created_at": "Sat Jun 07 08:09:10 +0000 2025",
                "author": {"name": "Carol", "screen_name": "carol"},
                "media": {
                    "photos": [
                        {"url": "https://pbs.twimg.com/media/a.jpg"},
                    ],
                    "videos": [
                        {"url": "https://video.twimg.com/ext_tw_video/a.mp4"},
                        {"url": "https://video.twimg.com/ext_tw_video/b.mp4"},
                    ],
                },
            }
        }

        result = extractor._build_result(
            payload, "https://x.com/carol/status/1234567890123456789"
        )

        self.assertEqual(result.image_urls, ["https://pbs.twimg.com/media/a.jpg"])
        self.assertEqual(
            result.video_urls,
            [
                "https://video.twimg.com/ext_tw_video/a.mp4",
                "https://video.twimg.com/ext_tw_video/b.mp4",
            ],
        )

    def test_build_result_accepts_status_root_and_media_all_external(self):
        extractor = TwitterExtractor()
        payload = {
            "status": {
                "text": "兼容新版结构",
                "created_at": "Mon Jul 14 03:04:05 +0000 2025",
                "author": {"name": "Erin", "screen_name": "erin"},
                "media": {
                    "photos": [
                        {"url": "https://pbs.twimg.com/media/main.jpg"},
                    ],
                    "all": [
                        {
                            "type": "photo",
                            "url": "https://pbs.twimg.com/media/main.jpg",
                        },
                        {
                            "type": "video",
                            "url": "https://video.twimg.com/ext_tw_video/all.mp4",
                        },
                    ],
                    "external": {
                        "type": "video",
                        "url": "https://video.twimg.com/ext_tw_video/external.mp4",
                    },
                },
            }
        }

        result = extractor._build_result(
            payload, "https://x.com/erin/status/1234567890123456789"
        )

        self.assertEqual(result.text, "兼容新版结构")
        self.assertEqual(result.author, "Erin(@erin)")
        self.assertEqual(result.created_at, "2025-07-14")
        self.assertEqual(result.image_urls, ["https://pbs.twimg.com/media/main.jpg"])
        self.assertEqual(
            result.video_urls,
            [
                "https://video.twimg.com/ext_tw_video/all.mp4",
                "https://video.twimg.com/ext_tw_video/external.mp4",
            ],
        )

    async def test_parse_returns_text_only_tweet_without_media(self):
        extractor = TwitterExtractor()
        extractor._fetch_status_json = AsyncMock(
            return_value={
                "tweet": {
                    "text": "纯文本",
                    "author": {"name": "Dave", "screen_name": "dave"},
                    "media": {},
                }
            }
        )

        result = await extractor.parse("https://x.com/dave/status/1234567890123456789")

        self.assertEqual(result.text, "纯文本")
        self.assertEqual(result.image_urls, [])
        self.assertEqual(result.video_urls, [])
        self.assertFalse(result.has_media)


if __name__ == "__main__":
    unittest.main(verbosity=2)
