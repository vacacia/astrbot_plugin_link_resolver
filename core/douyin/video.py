# region 导入
from typing import Any

from msgspec import Struct, field

from .errors import DouyinParseError
# endregion


# region 数据模型
class Avatar(Struct):
    url_list: list[str]


class Author(Struct):
    nickname: str
    avatar_thumb: Avatar | None = None
    avatar_medium: Avatar | None = None


class PlayAddr(Struct):
    url_list: list[str]


class Cover(Struct):
    url_list: list[str]


class Video(Struct):
    play_addr: PlayAddr
    cover: Cover
    duration: int


class Image(Struct):
    video: Video | None = None
    url_list: list[str] = field(default_factory=list)


class VideoData(Struct):
    aweme_id: str
    create_time: int
    author: Author
    desc: str
    images: list[Image] | None = None
    video: Video | None = None

    @property
    def image_urls(self) -> list[str]:
        return [urls[0] for urls in self.image_url_candidates]

    @property
    def image_url_candidates(self) -> list[list[str]]:
        return [
            list(dict.fromkeys(image.url_list))
            for image in self.images or []
            if image.url_list
        ]

    @property
    def video_url(self) -> str | None:
        return self.video_urls[0] if self.video_urls else None

    @property
    def video_urls(self) -> list[str]:
        if not self.video:
            return []
        return list(
            dict.fromkeys(
                url.replace("playwm", "play")
                for url in self.video.play_addr.url_list
                if url
            )
        )

    @property
    def dynamic_url_candidates(self) -> list[list[str]]:
        return [
            list(
                dict.fromkeys(
                    url.replace("playwm", "play")
                    for url in image.video.play_addr.url_list
                    if url
                )
            )
            for image in self.images or []
            if image.video and image.video.play_addr.url_list
        ]

    @property
    def cover_url(self) -> str | None:
        return (
            self.video.cover.url_list[0]
            if self.video and self.video.cover.url_list
            else None
        )

    @property
    def avatar_url(self) -> str | None:
        if (avatar := self.author.avatar_thumb) and avatar.url_list:
            return avatar.url_list[0]
        if (avatar := self.author.avatar_medium) and avatar.url_list:
            return avatar.url_list[0]
        return None


class VideoInfoRes(Struct):
    item_list: list[VideoData] = field(default_factory=list)

    @property
    def video_data(self) -> VideoData:
        if not self.item_list:
            raise DouyinParseError("no video data in videoInfoRes")
        return self.item_list[0]


class VideoOrNotePage(Struct):
    video_info_res: VideoInfoRes = field(
        name="videoInfoRes", default_factory=VideoInfoRes
    )


class LoaderData(Struct):
    video_page: VideoOrNotePage | None = field(name="video_(id)/page", default=None)
    note_page: VideoOrNotePage | None = field(name="note_(id)/page", default=None)


class RouterData(Struct):
    loader_data: LoaderData = field(name="loaderData", default_factory=LoaderData)
    errors: dict[str, Any] | None = None

    @property
    def video_data(self) -> VideoData:
        if page := self.loader_data.video_page:
            return page.video_info_res.video_data
        if page := self.loader_data.note_page:
            return page.video_info_res.video_data
        raise DouyinParseError(
            "missing video_(id)/page or note_(id)/page in router data"
        )


# endregion
