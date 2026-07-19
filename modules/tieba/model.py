from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TiebaMedia:
    url: str
    thumb_url: Optional[str] = None
    width: int = 0
    height: int = 0


@dataclass
class TiebaReply:
    floor: int
    author: str
    content: str
    agree_num: int = 0
    media_items: list[TiebaMedia] = field(default_factory=list)


@dataclass
class TiebaParseResult:
    success: bool = True
    title: str = ""
    content: str = ""
    author: str = ""
    forum_name: str = ""
    tieba_id: str = ""
    media_type: str = ""
    media_items: list[TiebaMedia] = field(default_factory=list)
    image_urls: list[str] = field(default_factory=list)
    video_url: str = ""
    cover_url: str = ""
    duration: int = 0
    create_time: int = 0
    replies: list[TiebaReply] = field(default_factory=list)
    error: str = ""
    agree_num: int = 0
