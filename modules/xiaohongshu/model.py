from dataclasses import dataclass, field


@dataclass
class XiaohongshuParseResult:
    success: bool
    title: str = ""
    desc: str = ""
    author: str = ""
    note_id: str = ""
    media_type: str = ""  # "video" | "image" | "multi_image"
    publish_time: str = ""
    media_items: list = field(
        default_factory=list
    )  # [{"urls": [str,...], "type": "video"|"image"}]
    image_urls: list = field(default_factory=list)  # 兼容旧格式
    video_url: str = ""
    cover_url: str = ""
    duration: int = 0
    has_title: bool = False
    error: str = ""
