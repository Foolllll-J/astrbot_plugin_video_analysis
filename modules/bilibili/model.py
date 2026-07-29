class BiliVideoInfo:
    def __init__(
        self,
        aid: int,
        cid: int,
        bvid: str,
        title: str,
        cover: str,
        duration: int,
        stats: dict,
        owner_name: str = "",
    ):
        self.aid = aid
        self.cid = cid
        self.bvid = bvid
        self.title = title
        self.cover = cover
        self.duration = duration
        self.stats = stats
        self.owner_name = owner_name

    def to_dict(self) -> dict:
        return {
            "aid": self.aid,
            "cid": self.cid,
            "bvid": self.bvid,
            "title": self.title,
            "cover": self.cover,
            "duration": self.duration,
            "stats": self.stats,
            "owner_name": self.owner_name,
        }
