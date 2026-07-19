import json
import re

import httpx

from astrbot.api import logger

from .base import BaseStrategy, StrategyParams
from ..model import DouyinParseResult, parse_aweme_detail
from ..utils.url import AwemeIdFetcher

MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro Build/UQBC) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.6099.43 Mobile Safari/537.36"
)

PC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.6099.43 Safari/537.36"
)


class SharePageStrategy(BaseStrategy):
    @property
    def name(self) -> str:
        return "share_page"

    async def execute(self, params: StrategyParams) -> DouyinParseResult:
        url_match = re.search(r"(https?://[^\s]+)", params.url)
        if not url_match:
            return DouyinParseResult(
                success=False, error="未找到有效的 URL", source=self.name
            )

        extracted_url = url_match.group(1)

        try:
            id_fetcher = AwemeIdFetcher()
            aweme_id = await id_fetcher.get_aweme_id(extracted_url)
        except Exception as e:
            return DouyinParseResult(
                success=False, error=f"提取 aweme_id 失败: {e}", source=self.name
            )

        # 如果是 /slides/ 类型，优先尝试结构化 slidesinfo API
        if "/slides/" in extracted_url:
            try:
                slides_result = await self._try_slides_api(aweme_id)
                if slides_result.success:
                    return slides_result
                logger.debug(f"SharePage slides API 失败: {slides_result.error}")
            except Exception as e:
                logger.debug(f"SharePage slides API 异常: {e}")

        # HTML 页面抓取 _ROUTER_DATA
        page_urls = [
            f"https://www.iesdouyin.com/share/video/{aweme_id}/",
            f"https://www.iesdouyin.com/share/note/{aweme_id}/",
            f"https://www.douyin.com/video/{aweme_id}",
            f"https://www.douyin.com/note/{aweme_id}",
        ]

        for page_url in page_urls:
            logger.debug(f"SharePage 尝试: {page_url}")
            result = await self._try_share_page(page_url, aweme_id)
            if result.success:
                return result
            logger.debug(f"SharePage 失败: {page_url} -> {result.error}")

        return DouyinParseResult(
            success=False,
            error=f"SharePage 无法解析 aweme_id={aweme_id}",
            source=self.name,
        )

    async def _try_slides_api(self, aweme_id: str) -> DouyinParseResult:
        """通过 iesdouyin.com 的 slidesinfo 结构化接口获取作品数据。"""
        headers = {
            "User-Agent": MOBILE_USER_AGENT,
            "Referer": "https://www.iesdouyin.com/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    "https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/",
                    params={
                        "aweme_ids": f"[{aweme_id}]",
                        "request_source": "200",
                    },
                    headers=headers,
                )
                if response.status_code >= 400:
                    return DouyinParseResult(
                        success=False,
                        error=f"slides API HTTP {response.status_code}",
                        source=self.name,
                    )

                data = response.json()
        except Exception as e:
            return DouyinParseResult(
                success=False, error=f"slides API 请求失败: {e}", source=self.name
            )

        item = self._extract_first_item(data)
        if not item:
            return DouyinParseResult(
                success=False, error="slides API 响应中无有效条目", source=self.name
            )

        return parse_aweme_detail(item, aweme_id, self.name)

    @staticmethod
    def _extract_first_item(data: dict) -> dict | None:
        """从 API 响应中提取第一条有效作品数据。"""
        for key in ("item_list", "aweme_details", "aweme_list"):
            items = data.get(key)
            if isinstance(items, list) and items:
                for item in items:
                    if isinstance(item, dict) and item:
                        return item
        item = data.get("aweme_detail")
        if isinstance(item, dict) and item:
            return item
        return None

    async def _try_share_page(self, share_url: str, aweme_id: str) -> DouyinParseResult:
        is_pc = "douyin.com/video" in share_url or "douyin.com/note" in share_url
        ua = PC_USER_AGENT if is_pc else MOBILE_USER_AGENT
        headers = {
            "User-Agent": ua,
            "Referer": "https://www.douyin.com/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    share_url, headers=headers, follow_redirects=True
                )
                response.raise_for_status()
        except Exception as e:
            return DouyinParseResult(
                success=False, error=f"请求分享页失败: {e}", source=self.name
            )

        json_str = self._extract_router_data(response.text)
        if not json_str:
            logger.debug(f"SharePage 无 _ROUTER_DATA, 响应长度={len(response.text)}")
            return DouyinParseResult(
                success=False, error="未找到 _ROUTER_DATA", source=self.name
            )

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            return DouyinParseResult(
                success=False,
                error=f"_ROUTER_DATA JSON 解析失败: {e}",
                source=self.name,
            )

        aweme_detail = self._find_aweme_detail(data)
        if not aweme_detail:
            return DouyinParseResult(
                success=False,
                error="_ROUTER_DATA 中未找到 aweme_detail",
                source=self.name,
            )

        return parse_aweme_detail(aweme_detail, aweme_id, self.name)

    def _extract_router_data(self, html: str) -> str | None:
        marker = "window._ROUTER_DATA"
        start = html.find(marker)
        if start == -1:
            return None
        brace_start = html.find("{", start)
        if brace_start == -1:
            return None

        depth = 0
        in_string = False
        escaped = False
        for i in range(brace_start, len(html)):
            ch = html[i]
            if escaped:
                escaped = False
                continue
            if ch == "\\" and in_string:
                escaped = True
                continue
            if ch == '"' and not escaped:
                in_string = not in_string
                continue
            if not in_string:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        return html[brace_start : i + 1]
        return None

    def _find_aweme_detail(self, data: dict) -> dict | None:
        loader = data.get("loaderData")
        if not loader:
            return None

        def _search_dict(d: dict, _path: str = "") -> dict | None:
            if not isinstance(d, dict):
                return None
            if "aweme_detail" in d and isinstance(d["aweme_detail"], dict):
                logger.debug(f"SharePage aweme_detail 在 {_path}")
                return d["aweme_detail"]
            item_list = d.get("item_list")
            if isinstance(item_list, list) and item_list:
                first = item_list[0]
                if isinstance(first, dict):
                    logger.debug(f"SharePage item_list 在 {_path}")
                    return first
            for k, v in d.items():
                if isinstance(v, dict):
                    found = _search_dict(v, f"{_path}.{k}")
                    if found:
                        return found
            return None

        for entry_key, entry in loader.items():
            if not isinstance(entry, dict):
                continue
            found = _search_dict(entry, f"loaderData.{entry_key}")
            if found:
                return found

        return None
