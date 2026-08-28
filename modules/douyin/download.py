import hashlib
import os
import re
import time

import aiofiles
import httpx

from astrbot.api import logger

from .model import DouyinParseResult, _clean_video_url
from .constants import DOWNLOAD_HEADERS, DOWNLOAD_TIMEOUT


def _safe_filename(text: str, max_len: int = 40) -> str:
    text = text.strip()
    text = re.sub(r'[\\/:*?"<>|]', "", text)
    text = re.sub(r"\s+", "_", text)
    text = text[:max_len] if len(text) > max_len else text
    return text.strip("_")


def _make_base_name(author: str, title: str, unique_id: str) -> str:
    parts = [
        p
        for p in [_safe_filename(author, 20), _safe_filename(title, 30), unique_id]
        if p
    ]
    return "_".join(parts)


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

    async def _try_download_one(
        self, url: str, save_path: str, label: str = ""
    ) -> bool:
        try:
            async with httpx.AsyncClient(
                timeout=DOWNLOAD_TIMEOUT, verify=False
            ) as client:
                async with client.stream(
                    "GET", url, headers=DOWNLOAD_HEADERS, follow_redirects=True
                ) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length", 0))
                    downloaded = 0
                    start_time = time.monotonic()
                    last_log = start_time
                    async with aiofiles.open(save_path, "wb") as f:
                        async for chunk in resp.aiter_bytes():
                            await f.write(chunk)
                            downloaded += len(chunk)
                            now = time.monotonic()
                            if now - last_log >= 60:
                                last_log = now
                                elapsed = now - start_time
                                mb = downloaded / (1024 * 1024)
                                if total:
                                    pct = downloaded * 100 // total
                                    eta_s = (total - downloaded) / max(
                                        downloaded / elapsed, 1
                                    )
                                    eta_str = (
                                        f"预计剩余{eta_s / 60:.0f}m"
                                        if eta_s >= 60
                                        else f"预计剩余{eta_s:.0f}s"
                                    )
                                    logger.debug(
                                        f"{label}下载中... {mb:.0f}MB/{total / (1024 * 1024):.0f}MB "
                                        f"({pct}%) 已用{elapsed / 60:.0f}m {eta_str}"
                                    )
                                else:
                                    logger.debug(
                                        f"{label}下载中... {mb:.0f}MB 已用{elapsed / 60:.0f}m"
                                    )
        except Exception:
            if os.path.exists(save_path):
                os.remove(save_path)
            return False

        return True

    def _preselect_video_urls(self, result: DouyinParseResult) -> list[str]:
        """主下载路径的 data_size 前置预选。

        仅当最高画质档的 data_size 已知且超过大小限制时，返回一档不超过限制的
        最高画质 URL；否则返回空列表，保持原有 raw play_addr 下载逻辑（不降质）。
        """
        if not self.smart_downgrade or self.max_size <= 0:
            return []
        br = result.video_bit_rate or []
        if not br:
            return []
        sorted_rates = sorted(
            br,
            key=lambda x: (
                x.get("play_addr", {}).get("width", 0)
                * x.get("play_addr", {}).get("height", 0),
                x.get("play_addr", {}).get("data_size", 0),
                x.get("bit_rate", 0),
            ),
            reverse=True,
        )
        limit_bytes = self.max_size * 1024 * 1024
        best_size = (sorted_rates[0].get("play_addr") or {}).get("data_size") or 0
        if not best_size or best_size <= limit_bytes:
            return []
        for tier in sorted_rates[1:]:
            pa = tier.get("play_addr", {}) or {}
            size = pa.get("data_size") or 0
            if not size or size > limit_bytes:
                continue
            urls = pa.get("url_list") or pa.get("urlList") or []
            cleaned = [_clean_video_url(u) for u in urls if isinstance(u, str)]
            return [u for u in cleaned if u]
        return []

    def video_all_qualities_over_limit(self, result: DouyinParseResult) -> bool:
        """最低清晰度仍超限：所有已知码率档的 data_size 都超过大小限制。

        仅当所有档位 data_size 均已知且全部超限时返回 True；任何一档大小未知
        或存在可容纳档时返回 False，交由正常下载逻辑处理。
        """
        if not self.smart_downgrade or self.max_size <= 0:
            return False
        br = result.video_bit_rate or []
        if not br:
            raw = result.raw_data or {}
            video = (raw.get("data") or {}).get("video") or {}
            br = video.get("bit_rate") or []
        if not br:
            return False
        limit_bytes = self.max_size * 1024 * 1024
        for tier in br:
            size = (tier.get("play_addr") or {}).get("data_size") or 0
            if not size:
                return False  # 存在未知大小档，无法可靠预判
            if size <= limit_bytes:
                return False  # 存在可容纳档
        return True

    async def _download_local(self, result: DouyinParseResult, url: str) -> dict:
        aweme_id = result.aweme_id or hashlib.md5(url.encode()).hexdigest()
        title = result.title
        author = result.author
        duration = result.duration
        base_name = _make_base_name(author, title, aweme_id)

        media_items = []
        for i, item in enumerate(result.media_items):
            if len(media_items) >= self.max_images:
                logger.debug(
                    f"媒体数量达到上限 {self.max_images}，跳过后续媒体。"
                )
                break

            candidate_urls: list[str] = item.get("urls") or []
            m_type = item["type"]

            if m_type == "video":
                v_file = os.path.join(self.download_dir, f"{base_name}_{i}.mp4")

                downloaded = False

                preselect_urls = self._preselect_video_urls(result) if i == 0 else []
                ordered_urls = [u for u in preselect_urls if u not in candidate_urls]
                ordered_urls += candidate_urls

                for c_url in ordered_urls:
                    if os.path.exists(v_file):
                        downloaded = True
                        break
                    if await self._try_download_one(c_url, v_file, "抖音"):
                        downloaded = True
                        break

                if not downloaded and i == 0 and result.video_bit_rate:
                    dl_result = await self._download_with_downgrade(
                        url, v_file, result.video_bit_rate, title, author, duration
                    )
                    if dl_result or os.path.exists(v_file):
                        downloaded = True

                if downloaded:
                    media_items.append({"path": v_file, "type": "video"})
            else:
                img_url = candidate_urls[0] if candidate_urls else ""
                ext = ".jpg"
                if ".png" in img_url.lower():
                    ext = ".png"
                elif ".webp" in img_url.lower():
                    ext = ".webp"
                elif ".gif" in img_url.lower():
                    ext = ".gif"

                img_file = os.path.join(self.download_dir, f"{base_name}_{i}{ext}")
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

            simple_id = hashlib.md5(url.encode()).hexdigest()[:12]
            base_name = _make_base_name(result.author, result.title, simple_id)
            final_file = os.path.join(self.download_dir, f"{base_name}.mp4")

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
            data_size = play_addr.get("data_size") or 0
            if (
                self.smart_downgrade
                and data_size
                and data_size > self.max_size * 1024 * 1024
            ):
                logger.debug(f"抖音降级：跳过 data_size={data_size}B 超限档")
                continue
            quality_url = _clean_video_url(url_list[0])

            if os.path.exists(final_file):
                os.remove(final_file)

            ok = await self._try_download_one(quality_url, final_file, "抖音降级")
            if not ok:
                continue

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
                    total = int(response.headers.get("content-length", 0))
                    downloaded = 0
                    start_time = time.monotonic()
                    last_log = start_time
                    async with aiofiles.open(save_path, "wb") as f:
                        async for chunk in response.aiter_bytes():
                            await f.write(chunk)
                            downloaded += len(chunk)
                            now = time.monotonic()
                            if now - last_log >= 60:
                                last_log = now
                                elapsed = now - start_time
                                mb = downloaded / (1024 * 1024)
                                if total:
                                    pct = downloaded * 100 // total
                                    eta_s = (total - downloaded) / max(
                                        downloaded / elapsed, 1
                                    )
                                    eta_str = (
                                        f"预计剩余{eta_s / 60:.0f}m"
                                        if eta_s >= 60
                                        else f"预计剩余{eta_s:.0f}s"
                                    )
                                    logger.debug(
                                        f"抖音下载中... {mb:.0f}MB/{total / (1024 * 1024):.0f}MB "
                                        f"({pct}%) 已用{elapsed / 60:.0f}m {eta_str}"
                                    )
                                else:
                                    logger.debug(
                                        f"抖音下载中... {mb:.0f}MB 已用{elapsed / 60:.0f}m"
                                    )
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
