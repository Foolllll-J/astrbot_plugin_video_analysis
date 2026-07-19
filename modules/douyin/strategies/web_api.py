import json
import random
import re
import secrets
import string
import time
from urllib.parse import urlencode, quote

import httpx


from .base import BaseStrategy, StrategyParams
from ..model import DouyinParseResult, parse_aweme_detail
from ..utils.url import AwemeIdFetcher
from ..sign import ABogus


POST_DETAIL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"

_FAKE_MS_TOKEN_CACHE: str | None = None


def _gen_fake_ms_token() -> str:
    global _FAKE_MS_TOKEN_CACHE
    if _FAKE_MS_TOKEN_CACHE:
        return _FAKE_MS_TOKEN_CACHE
    chars = string.ascii_letters + string.digits
    _FAKE_MS_TOKEN_CACHE = "".join(secrets.choice(chars) for _ in range(126)) + "=="
    return _FAKE_MS_TOKEN_CACHE


def _gen_webid() -> str:
    ts_part = str(int(time.time() * 1000))
    rand_part = str(random.randint(10000, 99999))
    return ts_part + rand_part


def _parse_cookie_to_dict(cookie_str: str) -> dict[str, str]:
    """将 cookie 字符串转为 dict。"""
    if not cookie_str:
        return {}
    result = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            result[key.strip()] = value.strip()
    return result


class WebApiStrategy(BaseStrategy):
    @property
    def name(self) -> str:
        return "web_api"

    async def execute(self, params: StrategyParams) -> DouyinParseResult:
        if not params.cookie:
            return DouyinParseResult(success=False, error="Web API 策略需要 Cookie")

        # 将 cookie 字符串转为 dict，如 httpx.AsyncClient(cookies=dict) 方式传递
        cookie_dict = _parse_cookie_to_dict(params.cookie)

        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        )
        headers = {
            "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
            "User-Agent": user_agent,
            "Referer": "https://www.douyin.com/",
        }

        url_match = re.search(r"(https?://[^\s]+)", params.url)
        if not url_match:
            return DouyinParseResult(success=False, error="未找到有效的 URL")

        extracted_url = url_match.group(1)

        try:
            id_fetcher = AwemeIdFetcher()
            aweme_id = await id_fetcher.get_aweme_id(extracted_url)
        except Exception as e:
            return DouyinParseResult(success=False, error=f"提取 aweme_id 失败: {e}")

        request_params = {
            "aweme_id": aweme_id,
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "pc_client_type": "1",
            "version_code": "290100",
            "version_name": "29.1.0",
            "cookie_enabled": "true",
            "screen_width": "1920",
            "screen_height": "1080",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Chrome",
            "browser_version": "130.0.0.0",
            "browser_online": "true",
            "engine_name": "Blink",
            "engine_version": "130.0.0.0",
            "os_name": "Windows",
            "os_version": "10",
            "cpu_core_num": "12",
            "device_memory": "8",
            "platform": "PC",
            "downlink": "10",
            "effective_type": "4g",
            "round_trip_time": "50",
        }

        try:
            ab_value = ABogus().get_value(request_params)
        except Exception as e:
            return DouyinParseResult(
                success=False,
                error=f"A-Bogus 生成失败: {e}",
                source=self.name,
            )
        a_bogus = quote(ab_value, safe="")
        endpoint = f"{POST_DETAIL}?{urlencode(request_params)}&a_bogus={a_bogus}"

        try:
            async with httpx.AsyncClient(cookies=cookie_dict) as client:
                response = await client.get(endpoint, headers=headers)
                response.raise_for_status()

                if not response.text:
                    return DouyinParseResult(
                        success=False,
                        error="API 返回空响应",
                        source=self.name,
                    )

                try:
                    raw_data = response.json()
                except json.JSONDecodeError:
                    return DouyinParseResult(
                        success=False,
                        error=f"API 返回非 JSON 数据: {response.text[:200]}",
                        source=self.name,
                    )

            aweme_detail = raw_data.get("aweme_detail")
            if not aweme_detail:
                aweme_detail = raw_data.get("story_25_filter")
            if not aweme_detail:
                return DouyinParseResult(
                    success=False, error="响应中无 aweme_detail", source=self.name
                )

            return parse_aweme_detail(aweme_detail, aweme_id, self.name)

        except httpx.HTTPStatusError as e:
            return DouyinParseResult(
                success=False,
                error=f"HTTP 错误: {e.response.status_code}",
                source=self.name,
            )
        except httpx.RequestError as e:
            return DouyinParseResult(
                success=False, error=f"请求失败: {e}", source=self.name
            )
