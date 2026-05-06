import asyncio
import json
import re
from astrbot.api import logger
from urllib.parse import urlencode

import httpx

from .cookie_extractor import extract_and_format_cookies
from .crawlers.douyin.web.endpoints import DouyinAPIEndpoints
from .crawlers.douyin.web.utils import AwemeIdFetcher, BogusManager


class DouyinParser:
    """
    一个独立的抖音分享链接解析器。
    """
    def __init__(self, cookie: str):
        # 使用cookie_extractor格式化cookie
        self.cookie = extract_and_format_cookies(cookie) if cookie else ""
        self.id_fetcher = AwemeIdFetcher()
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36"
        self.headers = {
            "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
            "User-Agent": self.user_agent,
            "Referer": "https://www.douyin.com/",
            "Cookie": self.cookie,
        }

    async def fetch_video_data(self, aweme_id: str) -> dict:
        """
        直接请求抖音API以获取视频数据。
        """
        params = {
            "aweme_id": aweme_id,
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "pc_client_type": "1",
            "version_code": "170400",
            "version_name": "17.4.0",
            "cookie_enabled": "true",
            "screen_width": "1920",
            "screen_height": "1080",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Edge",
            "browser_version": "117.0.2045.47",
            "browser_online": "true",
            "engine_name": "Blink",
            "engine_version": "117.0.0.0",
            "os_name": "Windows",
            "os_version": "10",
            "cpu_core_num": "16",
            "device_memory": "8",
            "platform": "PC",
            "downlink": "10",
            "effective_type": "4g",
            "round_trip_time": "50",
            "webid": "7318500000000000000",
            "msToken": "",
        }

        a_bogus = BogusManager.ab_model_2_endpoint(params, self.user_agent)
        endpoint = f"{DouyinAPIEndpoints.POST_DETAIL}?{urlencode(params)}&a_bogus={a_bogus}"

        async with httpx.AsyncClient() as client:
            response = await client.get(endpoint, headers=self.headers)
            response.raise_for_status()

            # Check if response is empty
            if not response.text:
                raise ValueError(
                    f"Empty response from Douyin API (aweme_id={aweme_id}). "
                    "This may indicate rate limiting, invalid cookie, or blocked request."
                )

            try:
                return response.json()
            except json.JSONDecodeError as exc:
                snippet = response.text[:200]
                content_type = response.headers.get("Content-Type", "")
                raise ValueError(
                    f"Invalid JSON response from Douyin API (aweme_id={aweme_id}, content_type={content_type}, snippet={snippet})"
                ) from exc

    def _process_data(self, raw_data: dict) -> dict:
        """
        处理原始API数据，提取关键信息和下载链接。
        """
        if not raw_data or "aweme_detail" not in raw_data:
            return {"error": "无效的原始数据格式"}

        aweme_detail = raw_data["aweme_detail"]

        media_type = "unknown"
        media_urls = []

        # 最可靠的判断方式：检查是否存在 images 列表并且其不为空
        if aweme_detail.get("images") and len(aweme_detail["images"]) > 0:
            # 默认为图文，但如果发现视频片段，则更新类型
            media_type = "image"
            images = aweme_detail.get("images")
            has_video_segment = False
            for item in images:
                # 检查每个item是图片还是视频片段
                if item.get("video"):
                    has_video_segment = True
                    video_list = item.get("video", {}).get("play_addr", {}).get("url_list")
                    if video_list:
                        media_urls.append({"url": video_list[0], "type": "video"})
                elif item.get("url_list"):
                    # 提取最高清的图片链接
                    media_urls.append({"url": item["url_list"][-1], "type": "image"})
            
            if has_video_segment:
                media_type = "multi_video"
        # 否则，当作普通单视频处理
        elif aweme_detail.get("video"):
            media_type = "video"
            video_list = aweme_detail.get("video", {}).get("play_addr", {}).get("url_list")
            if video_list:
                media_urls.append({"url": video_list[0], "type": "video"})

        # 提取视频时长（毫秒转秒）
        duration = 0
        video_data = aweme_detail.get("video", {})
        if video_data:
            duration_ms = video_data.get("duration", 0) or 0
            duration = duration_ms / 1000 if duration_ms else 0
            # 提取清晰度选项日志
            bit_rate = video_data.get("bit_rate")
            if bit_rate:
                quality_options = []
                for br in bit_rate:
                    quality_options.append({
                        "quality_type": br.get("quality_type"),
                        "gear_name": br.get("gear_name"),
                        "duration": br.get("duration"),
                    })
            else:
                logger.debug(f"[DouyinParser] 抖音视频无可用的 bit_rate 信息")

        # 提取基础信息
        processed_data = {
            "aweme_id": aweme_detail.get("aweme_id"),
            "type": media_type,
            "desc": aweme_detail.get("desc"),
            "create_time": aweme_detail.get("create_time"),
            "author_nickname": aweme_detail.get("author", {}).get("nickname"),
            "media_urls": media_urls,
            "duration": duration,
        }

        return processed_data

    async def parse(self, share_url: str) -> dict:
        """
        解析单个抖音分享链接，并返回处理后的核心数据。

        Args:
            share_url: 抖音分享链接 (短链/长链/口令均可).

        Returns:
            包含核心视频信息的字典。
        """
        print(f"正在解析链接: {share_url}")

        # 步骤 1: 从分享文案中提取有效的URL
        url_match = re.search(r"(https?://[^\s]+)", share_url)
        if not url_match:
            print("未能在分享文案中找到有效的URL")
            return {"error": "No valid URL found in the share text"}

        extracted_url = url_match.group(1)
        print(f"成功提取URL: {extracted_url}")

        # 步骤 2: 从URL中提取 aweme_id
        try:
            aweme_id = await self.id_fetcher.get_aweme_id(extracted_url)
            if not aweme_id:
                raise ValueError("未能从链接中提取到 aweme_id")
            print(f"成功提取 aweme_id: {aweme_id}")
        except Exception as e:
            print(f"提取 aweme_id 失败: {e}")
            return {"error": "Failed to extract aweme_id", "details": str(e)}

        # 步骤 3: 使用 aweme_id 获取视频详情
        try:
            raw_video_data = await self.fetch_video_data(aweme_id)
            print("成功获取视频数据！")
            # 步骤 4: 处理原始数据，提取核心信息
            processed_data = self._process_data(raw_video_data)
            return processed_data
        except Exception as e:
            print(f"获取或处理视频数据失败: {e}")
            return {"error": "Failed to fetch or process video data", "details": str(e)}
