# ruff: noqa: E402
"""Unit tests for the Weibo extractor.

Run from the AcaBot repo root:
    .venv/bin/python -m pytest extensions/plugins/link_resolver/tests/test_weibo_extractor.py -q
"""

from __future__ import annotations

import unittest

from plugins.link_resolver.core.weibo import (
    WeiboExtractor,
    extract_weibo_links,
)


class TestWeiboExtractor(unittest.IsolatedAsyncioTestCase):
    def test_extract_weibo_links_variants(self):
        text = (
            "看看这个 https://weibo.com/1234567890/AbCdEfGhI "
            "还有 m.weibo.cn/status/AbCdEfGhI 和 t.cn/A6abcXYZ"
        )

        links = extract_weibo_links(text)

        self.assertIn("https://weibo.com/1234567890/AbCdEfGhI", links)
        self.assertIn("https://m.weibo.cn/status/AbCdEfGhI", links)
        self.assertIn("https://t.cn/A6abcXYZ", links)

    async def test_user_cookie_is_preferred_over_visitor_cookie(self):
        extractor = WeiboExtractor()
        extractor.set_cookie("SUB=foo; SUBP=bar")

        cookies = await extractor._get_request_cookies()

        self.assertEqual(cookies["SUB"], "foo")
        self.assertEqual(cookies["SUBP"], "bar")

    def test_build_result_prefers_long_text_and_original_image(self):
        extractor = WeiboExtractor(download_original=True)
        status = {
            "id": "1234567890123456",
            "mblogid": "AbCdEfGhI",
            "created_at": "Thu Mar 12 15:00:00 +0800 2026",
            "user": {"screen_name": "博主甲"},
            "isLongText": True,
            "longTextContent_raw": "完整正文\\n第二行",
            "text_raw": "截断正文",
            "pic_ids": ["pic1"],
            "pic_infos": {
                "pic1": {
                    "largest": {"url": "https://wx4.sinaimg.cn/large/pic1.jpg"},
                    "large": {"url": "https://wx4.sinaimg.cn/orj960/pic1.jpg"},
                }
            },
        }

        result = extractor._build_result(
            status, "https://weibo.com/1234567890/AbCdEfGhI"
        )

        self.assertEqual(result.text, "完整正文\\n第二行")
        self.assertEqual(result.image_urls, ["https://wx4.sinaimg.cn/large/pic1.jpg"])
        self.assertIsNone(result.video_url)

    def test_build_result_picks_highest_bitrate_video(self):
        extractor = WeiboExtractor()
        status = {
            "id": "1234567890123456",
            "mblogid": "AbCdEfGhI",
            "created_at": "Thu Mar 12 15:00:00 +0800 2026",
            "user": {"screen_name": "博主乙"},
            "text_raw": "视频微博",
            "page_info": {
                "type": "video",
                "page_pic": {"url": "https://wx4.sinaimg.cn/large/cover.jpg"},
                "media_info": {
                    "playback_list": [
                        {
                            "play_info": {
                                "bitrate": 1200,
                                "url": "https://media.example.com/low.mp4",
                            }
                        },
                        {
                            "play_info": {
                                "bitrate": 4800,
                                "url": "https://media.example.com/high.mp4",
                            }
                        },
                    ]
                },
            },
        }

        result = extractor._build_result(
            status, "https://weibo.com/1234567890/AbCdEfGhI"
        )

        self.assertEqual(result.video_url, "https://media.example.com/high.mp4")
        self.assertEqual(result.cover_url, "https://wx4.sinaimg.cn/large/cover.jpg")
        self.assertEqual(result.image_urls, [])

    def test_build_result_falls_back_to_retweeted_status_media(self):
        extractor = WeiboExtractor()
        status = {
            "id": "1234567890123456",
            "mblogid": "AbCdEfGhI",
            "created_at": "Thu Mar 12 15:00:00 +0800 2026",
            "user": {"screen_name": "转发者"},
            "text_raw": "转发评论",
            "retweeted_status": {
                "text_raw": "原微博正文",
                "user": {"screen_name": "原作者"},
                "pic_ids": ["pic1"],
                "pic_infos": {
                    "pic1": {
                        "large": {"url": "https://wx4.sinaimg.cn/orj960/original.jpg"}
                    }
                },
            },
        }

        result = extractor._build_result(
            status, "https://weibo.com/1234567890/AbCdEfGhI"
        )

        self.assertIn("转发评论", result.text)
        self.assertIn("转发自 @原作者", result.text)
        self.assertEqual(
            result.image_urls, ["https://wx4.sinaimg.cn/orj960/original.jpg"]
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
