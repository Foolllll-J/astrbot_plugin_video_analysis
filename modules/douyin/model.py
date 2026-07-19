import re
from dataclasses import dataclass, field
from typing import Optional, Any


@dataclass
class VideoInfo:
    url: str
    thumb_url: Optional[str] = None
    width: int = 0
    height: int = 0
    duration: float = 0
    bit_rate: list = field(default_factory=list)


@dataclass
class DouyinParseResult:
    success: bool
    title: str = ""
    author: str = ""
    aweme_id: str = ""
    media_type: str = ""  # video / image / multi_video
    duration: float = 0
    media_items: list = field(
        default_factory=list
    )  # [{"urls": [str,...], "type": "video"|"image"}] 保持原始顺序
    video_bit_rate: list = field(default_factory=list)  # bit_rate 数组，供降级下载使用
    cover_url: str = ""
    source: str = ""
    error: str = ""
    raw_data: Any = None  # 完整原始响应，供下载复用


@dataclass
class StrategyResult:
    success: bool
    parse_result: Optional[DouyinParseResult] = None
    error: str = ""
    failure_detail: dict = field(default_factory=dict)


_AUDIO_EXT_RE = re.compile(r"\.(mp3|m4a|aac|wav|flac|ogg)(\?|$)", re.I)


def _clean_video_url(url: str) -> str | None:
    if not url or not isinstance(url, str):
        return None
    if not url.startswith("http"):
        return None
    if _AUDIO_EXT_RE.search(url):
        return None
    # 拒绝 video_id 本身是完整 URL 的情况（分享页/API 可能返回音乐 CDN 作为 video_id）
    if "video_id=" in url:
        after = url.split("video_id=", 1)[1]
        vid = after.split("&", 1)[0]
        if vid.startswith("http"):
            return None
    return url.replace("playwm", "play")


def _extract_urls_from_addr(play_addr: dict | None) -> list[str]:
    """从 play_addr 提取所有候选 URL，含 url_list/urlList 两种 key + uri 兜底。"""
    if not isinstance(play_addr, dict):
        return []
    urls: list[str] = []
    for key in ("url_list", "urlList"):
        raw = play_addr.get(key)
        if isinstance(raw, list):
            for u in raw:
                cleaned = _clean_video_url(u)
                if cleaned and cleaned not in urls:
                    urls.append(cleaned)
    # 始终追加 URI 兜底（即使 url_list 非空也加）
    uri = play_addr.get("uri")
    if uri:
        fallback = (
            f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio=720p&line=0"
        )
        if fallback not in urls:
            urls.append(fallback)
    return urls


def parse_aweme_detail(
    aweme_detail: dict, aweme_id: str, source: str
) -> DouyinParseResult:
    if not aweme_detail:
        return DouyinParseResult(
            success=False, error="aweme_detail 为空", source=source
        )

    media_type = "unknown"
    media_items: list[dict] = []

    if aweme_detail.get("images") and len(aweme_detail["images"]) > 0:
        media_type = "image"
        has_video_segment = False
        for item in aweme_detail["images"]:
            if item.get("video"):
                has_video_segment = True
                urls = _extract_urls_from_addr(item["video"].get("play_addr"))
                if urls:
                    media_items.append({"urls": urls, "type": "video"})
            else:
                for key in ("url_list", "urlList"):
                    raw = item.get(key)
                    if isinstance(raw, list) and raw:
                        img_url = raw[-1]
                        if isinstance(img_url, str) and img_url.startswith("http"):
                            media_items.append({"urls": [img_url], "type": "image"})
                        break
        if has_video_segment:
            media_type = "multi_video"
    elif aweme_detail.get("image_post_info"):
        media_type = "image"
        for image in aweme_detail["image_post_info"].get("images", []):
            display_image = image.get("display_image", {})
            for key in ("url_list", "urlList"):
                raw = display_image.get(key)
                if isinstance(raw, list) and raw:
                    img_url = raw[-1]
                    if isinstance(img_url, str) and img_url.startswith("http"):
                        media_items.append({"urls": [img_url], "type": "image"})
                    break
    elif aweme_detail.get("video"):
        media_type = "video"
        urls = _extract_urls_from_addr(aweme_detail["video"].get("play_addr"))
        if urls:
            media_items.append({"urls": urls, "type": "video"})

    duration = 0
    video_bit_rate: list = []
    video_data = aweme_detail.get("video", {})
    if video_data:
        duration_ms = video_data.get("duration", 0) or 0
        duration = duration_ms / 1000 if duration_ms else 0
        video_bit_rate = video_data.get("bit_rate", [])

    desc = aweme_detail.get("desc", "")
    author = aweme_detail.get("author", {}).get("nickname", "N/A")

    return DouyinParseResult(
        success=True,
        title=desc or "抖音作品",
        author=author,
        aweme_id=aweme_id,
        media_type=media_type,
        duration=duration,
        media_items=media_items,
        video_bit_rate=video_bit_rate,
        source=source,
        raw_data=aweme_detail,
    )
