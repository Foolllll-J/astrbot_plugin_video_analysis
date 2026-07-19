import httpx


from .base import BaseStrategy, StrategyParams
from ..model import DouyinParseResult
from ..constants import API_FALLBACK_HEADERS, DEFAULT_TIMEOUT


class ThirdPartyStrategy(BaseStrategy):
    @property
    def name(self) -> str:
        return "third_party"

    async def execute(self, params: StrategyParams) -> DouyinParseResult:
        if not params.api_url:
            return DouyinParseResult(success=False, error="第三方 API 地址未配置")

        api_endpoint = f"{params.api_url}/api/hybrid/video_data"

        try:
            async with httpx.AsyncClient(
                timeout=DEFAULT_TIMEOUT, verify=False
            ) as client:
                response = await client.get(
                    api_endpoint,
                    params={"url": params.url, "minimal": False},
                    headers=API_FALLBACK_HEADERS,
                )

                if response.status_code != 200:
                    return DouyinParseResult(
                        success=False,
                        error=f"API 返回 HTTP {response.status_code}",
                        source=self.name,
                    )

                api_data = response.json()
                code = api_data.get("code")
                status_code = api_data.get("status_code")
                if code != 200 and status_code != 0:
                    return DouyinParseResult(
                        success=False,
                        error=f"API 返回业务错误: {api_data.get('msg', '')}",
                        source=self.name,
                        raw_data=api_data,
                    )

                data = api_data.get("data", {})
                desc = data.get("desc", "抖音作品")
                author = data.get("author", {}).get("nickname", "N/A")

                video_data = data.get("video")
                images = data.get("images") or data.get("image_post_info")

                if video_data:
                    bit_rate = video_data.get("bit_rate")
                    if not bit_rate:
                        return DouyinParseResult(
                            success=False,
                            error="API 未返回视频下载地址",
                            source=self.name,
                            raw_data=api_data,
                        )

                    bit_rate.sort(key=lambda x: x["quality_type"], reverse=True)
                    best = bit_rate[0]
                    video_url = best["play_addr"]["url_list"][0]
                    thumb_url = video_data.get("cover", {}).get("url_list", [None])[-1]

                    duration_ms = video_data.get("duration", 0) or 0
                    duration = duration_ms / 1000 if duration_ms else 0

                    return DouyinParseResult(
                        success=True,
                        title=desc,
                        author=author,
                        media_type="video",
                        duration=duration,
                        media_items=[{"url": video_url, "type": "video"}],
                        cover_url=thumb_url or "",
                        source=self.name,
                        raw_data=api_data,
                    )

                if images:
                    return DouyinParseResult(
                        success=True,
                        title=desc,
                        author=author,
                        media_type="image",
                        source=self.name,
                        raw_data=api_data,
                    )

                return DouyinParseResult(
                    success=False,
                    error="无法解析内容类型",
                    source=self.name,
                    raw_data=api_data,
                )

        except httpx.TimeoutException:
            return DouyinParseResult(
                success=False, error="第三方 API 请求超时", source=self.name
            )
        except Exception as e:
            return DouyinParseResult(
                success=False, error=f"第三方 API 请求失败: {e}", source=self.name
            )
