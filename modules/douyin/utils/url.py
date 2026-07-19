import re

import httpx


class AwemeIdFetcher:
    _DOUYIN_VIDEO_URL_PATTERN = re.compile(r"video/([^/?]*)")
    _DOUYIN_VIDEO_URL_PATTERN_NEW = re.compile(r"[?&]vid=(\d+)")
    _DOUYIN_NOTE_URL_PATTERN = re.compile(r"note/([^/?]*)")
    _DOUYIN_DISCOVER_URL_PATTERN = re.compile(r"modal_id=([0-9]+)")

    @classmethod
    async def get_aweme_id(cls, url: str) -> str:
        if not isinstance(url, str):
            raise TypeError("参数必须是字符串类型")

        transport = httpx.AsyncHTTPTransport(retries=3)
        async with httpx.AsyncClient(transport=transport, timeout=10) as client:
            response = await client.get(url, follow_redirects=True)
            response.raise_for_status()
            response_url = str(response.url)

            for pattern in [
                cls._DOUYIN_VIDEO_URL_PATTERN,
                cls._DOUYIN_VIDEO_URL_PATTERN_NEW,
                cls._DOUYIN_NOTE_URL_PATTERN,
                cls._DOUYIN_DISCOVER_URL_PATTERN,
            ]:
                match = pattern.search(response_url)
                if match:
                    return match.group(1)

            raise ValueError(f"未在响应地址中找到 aweme_id: {response_url}")
