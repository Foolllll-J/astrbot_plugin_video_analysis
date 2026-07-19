import hashlib
import os
from urllib.parse import urlparse, unquote

import httpx

from astrbot.api import logger

from .constants import DOWNLOAD_HEADERS, IMAGE_EXTS, TIMEOUT
from .model import TiebaParseResult


class TiebaDownloader:
    def __init__(self, download_dir: str, max_images: int = 20):
        self.download_dir = download_dir
        self.max_images = max_images
        os.makedirs(self.download_dir, exist_ok=True)

    async def _download_file(self, url: str, ext_hint: str = "") -> str | None:
        parsed = urlparse(url)
        path = unquote(parsed.path)
        _, ext = os.path.splitext(path)
        if not ext or ext.lower() not in IMAGE_EXTS:
            ext = ext_hint or ".jpg"
        if not ext.startswith("."):
            ext = f".{ext}"
        name = f"{hashlib.md5(url.encode()).hexdigest()}{ext}"
        dest = os.path.join(self.download_dir, name)
        if os.path.exists(dest):
            return dest
        try:
            async with httpx.AsyncClient(
                timeout=TIMEOUT, headers=DOWNLOAD_HEADERS
            ) as cli:
                resp = await cli.get(url, follow_redirects=True)
                resp.raise_for_status()
                content = resp.content
            with open(dest, "wb") as f:
                f.write(content)
            return dest
        except Exception as e:
            logger.error(f"贴吧下载失败: {url} -> {e}")
            return None

    async def download(self, parse_result: TiebaParseResult, url: str) -> dict:
        if not parse_result.success:
            return {"error": parse_result.error or "解析失败"}

        result: dict = {
            "title": parse_result.title,
            "author": parse_result.author,
            "url": url,
            "type": parse_result.media_type,
            "ordered_media": [],
            "replies": [],
            "agree_num": parse_result.agree_num,
        }

        if parse_result.media_type == "video" and parse_result.video_url:
            video_path = await self._download_file(parse_result.video_url)
            if video_path:
                result["video_path"] = video_path
                result["type"] = "video"
            else:
                result["error"] = "视频下载失败"
            return result

        downloaded = 0

        for item in parse_result.media_items:
            if downloaded >= self.max_images:
                break
            for candidate_url in [item.url, item.thumb_url]:
                if not candidate_url:
                    continue
                path = await self._download_file(candidate_url)
                if path:
                    result.setdefault("ordered_media", []).append(
                        {"path": path, "type": "image"}
                    )
                    downloaded += 1
                    break

        for reply in parse_result.replies:
            reply_dict = {
                "floor": reply.floor,
                "author": reply.author,
                "content": reply.content,
                "agree_num": reply.agree_num,
                "media": [],
            }
            for item in reply.media_items:
                if downloaded >= self.max_images:
                    break
                for candidate_url in [item.url, item.thumb_url]:
                    if not candidate_url:
                        continue
                    path = await self._download_file(candidate_url)
                    if path:
                        reply_dict["media"].append({"path": path, "type": "image"})
                        downloaded += 1
                        break
            result["replies"].append(reply_dict)

        return result
