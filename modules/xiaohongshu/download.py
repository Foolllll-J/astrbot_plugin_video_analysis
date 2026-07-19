import asyncio
import hashlib
import os
import shutil
import tempfile
from urllib.parse import urljoin

import aiofiles
import httpx

from astrbot.api import logger

from .model import XiaohongshuParseResult
from .constants import DOWNLOAD_HEADERS, DEFAULT_TIMEOUT


class XiaohongshuDownloader:
    def __init__(self, download_dir: str, max_images: int = 20):
        self.download_dir = download_dir
        self.max_images = max_images

    async def download(self, result: XiaohongshuParseResult, url: str) -> dict:
        if not result.success:
            return {"error": result.error or "解析失败"}

        os.makedirs(self.download_dir, exist_ok=True)

        note_id = result.note_id or hashlib.md5(url.encode()).hexdigest()

        ordered_media = []
        img_count = 0

        for i, item in enumerate(result.media_items):
            candidate_urls: list[str] = item.get("urls") or []
            m_type = item["type"]

            if m_type == "video":
                if not candidate_urls:
                    continue
                v_file = os.path.join(self.download_dir, f"{note_id}.mp4")
                downloaded = False
                for v_url in candidate_urls:
                    if os.path.exists(v_file) or await self._download_file(
                        v_url, v_file
                    ):
                        downloaded = True
                        break
                if downloaded:
                    ordered_media.append({"path": v_file, "type": "video"})
            else:
                if img_count >= self.max_images:
                    logger.debug(
                        f"XHS 图片数量达到上限 {self.max_images}，跳过后续图片。"
                    )
                    break
                if not candidate_urls:
                    continue

                ext = ".jpg"
                img_file = os.path.join(
                    self.download_dir, f"{note_id}_{img_count}{ext}"
                )

                downloaded = False
                for img_url in candidate_urls:
                    if os.path.exists(img_file) or await self._download_file(
                        img_url, img_file
                    ):
                        downloaded = True
                        break

                if downloaded:
                    ordered_media.append({"path": img_file, "type": "image"})
                    img_count += 1

        if not ordered_media:
            return {"error": "没有下载到任何媒体文件"}

        return self._build_result(result, url, ordered_media)

    async def _download_file(self, url: str, save_path: str) -> bool:
        if ".m3u8" in url.lower():
            return await self._download_m3u8(url, save_path)
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(
                    timeout=DEFAULT_TIMEOUT, verify=False
                ) as client:
                    async with client.stream(
                        "GET", url, headers=DOWNLOAD_HEADERS, follow_redirects=True
                    ) as response:
                        response.raise_for_status()
                        async with aiofiles.open(save_path, "wb") as f:
                            async for chunk in response.aiter_bytes():
                                await f.write(chunk)
                return True
            except Exception as e:
                logger.warning(
                    f"XHS 文件下载失败 (attempt {attempt + 1}): {url}, 错误: {e}"
                )
                if attempt == 0:
                    await asyncio.sleep(1)
                if os.path.exists(save_path):
                    os.remove(save_path)
        return False

    async def _download_m3u8(self, m3u8_url: str, output_path: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, verify=False) as c:
                r = await c.get(m3u8_url, headers=DOWNLOAD_HEADERS, follow_redirects=True)
                r.raise_for_status()
                playlist = r.text
        except Exception as e:
            logger.warning(f"XHS M3U8 获取失败: {m3u8_url}, {e}")
            return False

        base = m3u8_url.rsplit("/", 1)[0] + "/" if "/" in m3u8_url else ""

        # Handle master playlist (multi-variant) — pick highest bandwidth
        if "#EXT-X-STREAM-INF" in playlist:
            variants = []
            lines = playlist.splitlines()
            i = 0
            while i < len(lines):
                line = lines[i]
                if line.startswith("#EXT-X-STREAM-INF:"):
                    params_str = line[len("#EXT-X-STREAM-INF:") :]
                    bw = 0
                    for part in params_str.split(","):
                        part = part.strip()
                        if part.upper().startswith("BANDWIDTH="):
                            bw = int(part.split("=", 1)[1].strip())
                    j = i + 1
                    while j < len(lines) and (
                        not lines[j].strip() or lines[j].strip().startswith("#")
                    ):
                        j += 1
                    if j < len(lines):
                        variants.append((bw, lines[j].strip()))
                    i = j
                else:
                    i += 1
            if not variants:
                logger.warning("XHS M3U8 主播放列表无可用变体")
                return False
            variants.sort(key=lambda v: v[0], reverse=True)
            best_uri = urljoin(base, variants[0][1])
            logger.debug(f"XHS M3U8 选择最高码率变体: {variants[0][0]} bps")
            return await self._download_m3u8(best_uri, output_path)

        # Media playlist — download segments
        seg_urls = [
            urljoin(base, line.strip())
            for line in playlist.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

        if not seg_urls:
            logger.warning(f"XHS M3U8 无分片: {m3u8_url}")
            return False

        tmpdir = tempfile.mkdtemp(prefix="xhs_m3u8_")
        seg_files = []
        try:
            for i, seg_url in enumerate(seg_urls):
                seg_path = os.path.join(tmpdir, f"seg_{i:05d}.ts")
                for attempt in range(2):
                    try:
                        async with httpx.AsyncClient(
                            timeout=DEFAULT_TIMEOUT, verify=False
                        ) as c:
                            resp = await c.get(
                                seg_url, headers=DOWNLOAD_HEADERS, follow_redirects=True
                            )
                            resp.raise_for_status()
                            async with aiofiles.open(seg_path, "wb") as f:
                                async for chunk in resp.aiter_bytes():
                                    await f.write(chunk)
                        seg_files.append(seg_path)
                        break
                    except Exception as e:
                        logger.warning(
                            f"XHS M3U8 分片 {i} 下载失败 (attempt {attempt + 1}): {seg_url}, {e}"
                        )
                        if attempt == 0:
                            await asyncio.sleep(1)
                        if os.path.exists(seg_path):
                            os.remove(seg_path)
                else:
                    raise RuntimeError(f"分片 {i} 下载失败")

            async with aiofiles.open(output_path, "wb") as out:
                for seg_path in seg_files:
                    async with aiofiles.open(seg_path, "rb") as inp:
                        while True:
                            chunk = await inp.read(65536)
                            if not chunk:
                                break
                            await out.write(chunk)
            return True
        except Exception as e:
            logger.warning(f"XHS M3U8 拼接失败: {e}")
            if os.path.exists(output_path):
                os.remove(output_path)
            return False
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @staticmethod
    def _build_result(
        result: XiaohongshuParseResult, url: str, ordered_media: list
    ) -> dict:
        if len(ordered_media) == 1 and ordered_media[0]["type"] == "video":
            return {
                "title": result.title,
                "author": result.author,
                "url": url,
                "video_path": ordered_media[0]["path"],
                "type": "video",
                "duration": 0,
            }

        return {
            "title": result.title,
            "author": result.author,
            "url": url,
            "ordered_media": ordered_media,
            "type": "multi_video"
            if any(i["type"] == "video" for i in ordered_media)
            else "images",
        }
