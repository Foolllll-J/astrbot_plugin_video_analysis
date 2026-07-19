import hashlib
import os

import aiofiles
import httpx

from astrbot.api import logger

from .model import DouyinParseResult, _clean_video_url
from .constants import DOWNLOAD_HEADERS, DOWNLOAD_TIMEOUT


class DouyinDownloader:
    def __init__(
        self,
        download_dir: str,
        max_images: int = 20,
        max_size: float = 200,
        smart_downgrade: bool = True,
    ):
        self.download_dir = download_dir
        self.max_images = max_images
        self.max_size = max_size
        self.smart_downgrade = smart_downgrade

    async def download(self, result: DouyinParseResult, url: str) -> dict:
        if not result.success:
            return {"error": result.error or "解析失败"}

        os.makedirs(self.download_dir, exist_ok=True)

        if result.source in ("web_api", "share_page", "mobile_api"):
            return await self._download_local(result, url)
        elif result.source == "third_party":
            return await self._download_third_party(result, url)
        return {"error": f"未知来源: {result.source}"}

    async def _download_local(self, result: DouyinParseResult, url: str) -> dict:
        aweme_id = result.aweme_id or hashlib.md5(url.encode()).hexdigest()
        title = result.title
        author = result.author
        duration = result.duration

        media_items = []
        for i, item in enumerate(result.media_items):
            candidate_urls: list[str] = item.get("urls") or []
            m_type = item["type"]

            if m_type == "video":
                v_file = os.path.join(self.download_dir, f"{aweme_id}_{i}.mp4")

                downloaded = False
                if i == 0 and result.video_bit_rate:
                    dl_result = await self._download_with_downgrade(
                        url, v_file, result.video_bit_rate, title, author, duration
                    )
                    if dl_result or os.path.exists(v_file):
                        downloaded = True

                if not downloaded:
                    for c_url in candidate_urls:
                        if os.path.exists(v_file) or await self._download_file(
                            c_url, v_file
                        ):
                            downloaded = True
                            break

                if downloaded:
                    media_items.append({"path": v_file, "type": "video"})
            else:
                if (
                    len([m for m in media_items if m["type"] == "image"])
                    >= self.max_images
                ):
                    logger.debug(f"图片数量达到上限 {self.max_images}，跳过后续图片。")
                    continue

                img_url = candidate_urls[0] if candidate_urls else ""
                ext = ".jpg"
                if ".png" in img_url.lower():
                    ext = ".png"
                elif ".webp" in img_url.lower():
                    ext = ".webp"
                elif ".gif" in img_url.lower():
                    ext = ".gif"

                img_file = os.path.join(self.download_dir, f"{aweme_id}_{i}{ext}")
                if os.path.exists(img_file) or await self._download_file(
                    img_url, img_file
                ):
                    media_items.append({"path": img_file, "type": "image"})

        if not media_items:
            return {"error": "没有下载到任何媒体文件"}

        return self._build_result(title, author, url, media_items, duration)

    async def _download_third_party(self, result: DouyinParseResult, url: str) -> dict:
        raw_data = result.raw_data
        if not raw_data:
            return {"error": "第三方 API 无原始数据"}

        api_data = raw_data if isinstance(raw_data, dict) else {}
        data = api_data.get("data", {})
        video_data = data.get("video")

        if not video_data:
            return {
                "title": result.title,
                "author": result.author,
                "url": url,
                "video_path": None,
                "duration": result.duration,
                "type": result.media_type,
            }

        try:
            bit_rate = video_data.get("bit_rate", [])
            if not bit_rate:
                return {"error": "第三方 API 无码率信息"}

            bit_rate.sort(key=lambda x: x["quality_type"], reverse=True)
            duration = (video_data.get("duration", 0) or 0) / 1000

            simple_id = hashlib.md5(url.encode()).hexdigest()
            final_file = os.path.join(self.download_dir, f"{simple_id}.mp4")

            if os.path.exists(final_file):
                return {
                    "title": result.title,
                    "author": result.author,
                    "url": url,
                    "video_path": final_file,
                    "duration": duration,
                }

            success = await self._download_with_downgrade(
                url, final_file, bit_rate, result.title, result.author, duration
            )
            if success:
                return success

            return {"error": "第三方 API 所有清晰度均下载失败"}

        except Exception as e:
            logger.error(f"第三方 API 下载处理失败: {e}")
            return {"error": f"第三方 API 下载异常: {e}"}

    async def _download_with_downgrade(
        self,
        original_url: str,
        final_file: str,
        bit_rate: list,
        title: str,
        author: str,
        duration: float,
    ) -> dict | None:
        # 按分辨率降序排列
        # 过滤 ByteVC1 私有编码（无法被标准播放器解码，会导致有音无画）
        bit_rate = [br for br in bit_rate if br.get("is_bytevc1", 0) == 0]
        sorted_rates = sorted(
            bit_rate,
            key=lambda x: (
                x.get("play_addr", {}).get("width", 0)
                * x.get("play_addr", {}).get("height", 0),
                x.get("play_addr", {}).get("data_size", 0),
                x.get("bit_rate", 0),
            ),
            reverse=True,
        )

        for br in sorted_rates:
            play_addr = br.get("play_addr", {})
            url_list = play_addr.get("url_list") or play_addr.get("urlList")
            if not url_list:
                continue
            quality_url = _clean_video_url(url_list[0])

            try:
                async with httpx.AsyncClient(
                    timeout=DOWNLOAD_TIMEOUT, verify=False
                ) as client:
                    async with client.stream(
                        "GET", quality_url, headers=DOWNLOAD_HEADERS, follow_redirects=True
                    ) as resp:
                        resp.raise_for_status()
                        async with aiofiles.open(final_file, "wb") as f:
                            async for chunk in resp.aiter_bytes():
                                await f.write(chunk)

                file_size_mb = os.path.getsize(final_file) / (1024 * 1024)
                if file_size_mb > self.max_size and self.smart_downgrade:
                    os.remove(final_file)
                    continue

                return {
                    "title": title,
                    "author": author,
                    "url": original_url,
                    "video_path": final_file,
                    "duration": duration,
                }

            except Exception as e:
                logger.warning(f"抖音清晰度降级下载失败: {e}")
                if os.path.exists(final_file):
                    os.remove(final_file)
                continue

        return None

    async def _download_file(self, url: str, save_path: str) -> bool:
        try:
            async with httpx.AsyncClient(
                timeout=DOWNLOAD_TIMEOUT, verify=False
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
            logger.error(f"文件下载失败: {url}, 错误: {e}")
            if os.path.exists(save_path):
                os.remove(save_path)
            return False

    @staticmethod
    def _build_result(
        title: str, author: str, url: str, media_items: list, duration: float
    ) -> dict:
        if len(media_items) == 1:
            item = media_items[0]
            if item["type"] == "video":
                return {
                    "title": title,
                    "author": author,
                    "url": url,
                    "video_path": item["path"],
                    "type": "video",
                    "duration": duration,
                }
            else:
                return {
                    "title": title,
                    "author": author,
                    "url": url,
                    "image_paths": [item["path"]],
                    "type": "image",
                    "duration": duration,
                }

        return {
            "title": title,
            "author": author,
            "url": url,
            "ordered_media": media_items,
            "type": "multi_video"
            if any(i["type"] == "video" for i in media_items)
            else "images",
            "duration": duration,
        }
