from __future__ import annotations

from plugins.link_resolver.compat.optional_deps import _decode_msgspec_fallback
from plugins.link_resolver.core.douyin.slides import SlidesInfo
from plugins.link_resolver.core.douyin.video import RouterData


def test_decode_msgspec_router_data_without_msgspec_runtime() -> None:
    payload = b'''{
      "loaderData": {
        "video_(id)/page": {
          "videoInfoRes": {
            "item_list": [
              {
                "aweme_id": "7627703506816602022",
                "create_time": 1,
                "author": {
                  "nickname": "tester",
                  "avatar_thumb": {"url_list": ["https://avatar.example/thumb.jpg"]}
                },
                "desc": "douyin video",
                "video": {
                  "play_addr": {"url_list": ["https://video.example/playwm.mp4"]},
                  "cover": {"url_list": ["https://video.example/cover.jpg"]},
                  "duration": 12
                }
              }
            ]
          }
        }
      }
    }'''

    decoded = _decode_msgspec_fallback(payload, RouterData)

    assert decoded.video_data.aweme_id == "7627703506816602022"
    assert decoded.video_data.author.nickname == "tester"
    assert decoded.video_data.video_url == "https://video.example/play.mp4"
    assert decoded.video_data.cover_url == "https://video.example/cover.jpg"


def test_decode_msgspec_fallback_accepts_msgspec_keyword_signature() -> None:
    payload = b'{"loaderData": {"video_(id)/page": {"videoInfoRes": {"item_list": []}}}}'

    decoded = _decode_msgspec_fallback(payload, type=RouterData)

    assert isinstance(decoded, RouterData)



def test_decode_msgspec_slides_data_without_msgspec_runtime() -> None:
    payload = b'''{
      "aweme_details": [
        {
          "author": {
            "nickname": "slides-author",
            "avatar_thumb": {"url_list": ["https://avatar.example/slides.jpg"]}
          },
          "desc": "slides post",
          "create_time": 2,
          "images": [
            {
              "url_list": ["https://image.example/1.jpg"],
              "video": {
                "play_addr": {"url_list": ["https://image.example/1.mp4"]},
                "cover": {"url_list": ["https://image.example/1-cover.jpg"]},
                "duration": 3
              }
            }
          ]
        }
      ]
    }'''

    decoded = _decode_msgspec_fallback(payload, SlidesInfo)

    assert decoded.aweme_details[0].name == "slides-author"
    assert decoded.aweme_details[0].image_urls == ["https://image.example/1.jpg"]
    assert decoded.aweme_details[0].dynamic_urls == ["https://image.example/1.mp4"]
