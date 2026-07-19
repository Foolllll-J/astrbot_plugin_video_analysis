from typing import Any


class NgaMedia:
    def __init__(
        self, url: str, thumb_url: str | None = None, width: int = 0, height: int = 0
    ):
        self.url = url
        self.thumb_url = thumb_url
        self.width = width
        self.height = height

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"url": self.url}
        if self.thumb_url:
            d["thumb_url"] = self.thumb_url
        if self.width:
            d["width"] = self.width
        if self.height:
            d["height"] = self.height
        return d


class NgaReply:
    def __init__(
        self,
        floor: int,
        author: str,
        content: str,
        score: int = 0,
        post_id: str = "",
        reply_to_pids: list[str] | None = None,
        media_items: list[NgaMedia] | None = None,
        raw_bbcode: str = "",
        post_date: str = "",
    ):
        self.floor = floor
        self.author = author
        self.content = content
        self.score = score
        self.post_id = post_id
        self.reply_to_pids = reply_to_pids or []
        self.media_items = media_items or []
        self.raw_bbcode = raw_bbcode
        self.post_date = post_date


class NgaParseResult:
    def __init__(
        self,
        success: bool,
        error: str = "",
        title: str = "",
        content: str = "",
        author: str = "",
        forum_name: str = "",
        tid: str = "",
        media_type: str = "text",
        media_items: list[NgaMedia] | None = None,
        image_urls: list[str] | None = None,
        video_url: str = "",
        cover_url: str = "",
        create_time: int = 0,
        replies: list[NgaReply] | None = None,
        total_replies: int = 0,
        pid_map: dict | None = None,
        op_post_id: str = "",
        op_score: int = 0,
    ):
        if pid_map is None:
            pid_map = {}
        self.success = success
        self.error = error
        self.title = title
        self.content = content
        self.author = author
        self.forum_name = forum_name
        self.tid = tid
        self.media_type = media_type
        self.media_items = media_items or []
        self.image_urls = image_urls or []
        self.video_url = video_url
        self.cover_url = cover_url
        self.create_time = create_time
        self.replies = replies or []
        self.total_replies = total_replies
        self.pid_map: dict[str, tuple[int, str, str]] = pid_map
        self.op_post_id: str = op_post_id
        self.op_score: int = op_score
