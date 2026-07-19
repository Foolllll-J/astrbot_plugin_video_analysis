import hashlib
import logging
import os

import httpx

from .constants import DOWNLOAD_HEADERS, IMAGE_EXTS, TIMEOUT

logger = logging.getLogger(__name__)


class NgaDownloader:
    def __init__(self, download_dir: str, max_images: int = 10):
        self.download_dir = download_dir
        self.max_images = max_images
        self._proxy: str | None = None

    async def _download_file(self, url: str) -> str | None:
        if not url:
            return None
        ext = os.path.splitext(url.split("?")[0])[1].lower()
        if ext not in IMAGE_EXTS:
            ext = ".jpg"
        os.makedirs(self.download_dir, exist_ok=True)
        name = hashlib.md5(url.encode()).hexdigest()[:16] + ext
        path = os.path.join(self.download_dir, name)
        if os.path.exists(path):
            return path
        try:
            async with httpx.AsyncClient(
                proxy=self._proxy, timeout=TIMEOUT, follow_redirects=True
            ) as cli:
                resp = await cli.get(url, headers=DOWNLOAD_HEADERS)
                resp.raise_for_status()
                with open(path, "wb") as f:
                    f.write(resp.content)
            return path
        except Exception as e:
            logger.debug(f"NGA 下载失败 {url}: {e}")
            return None

    async def download(self, parse_result) -> dict:

        result: dict = {
            "ordered_media": [],
            "replies": [],
            "pid_map": getattr(parse_result, "pid_map", {}),
            "op_post_id": getattr(parse_result, "op_post_id", ""),
            "op_score": getattr(parse_result, "op_score", 0),
        }
        downloaded = 0

        for item in parse_result.media_items:
            if downloaded >= self.max_images:
                break
            path = await self._download_file(item.url)
            if path:
                result.setdefault("ordered_media", []).append(
                    {"path": path, "type": "image"}
                )
                downloaded += 1

        for reply in parse_result.replies:
            reply_dict = {
                "floor": reply.floor,
                "author": reply.author,
                "content": reply.content,
                "score": reply.score,
                "post_id": reply.post_id,
                "reply_to_pids": reply.reply_to_pids,
                "post_date": reply.post_date,
                "raw_bbcode": reply.raw_bbcode,
                "media": [],
            }
            for item in reply.media_items:
                if downloaded >= self.max_images:
                    break
                path = await self._download_file(item.url)
                if path:
                    reply_dict["media"].append({"path": path, "type": "image"})
                    downloaded += 1
            result["replies"].append(reply_dict)

        if not result["ordered_media"] and not result["replies"]:
            result["error"] = "未下载到任何内容"
        return result
