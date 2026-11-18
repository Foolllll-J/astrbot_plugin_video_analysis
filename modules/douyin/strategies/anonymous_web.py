"""接入现有策略链的匿名开放接口 / 匿名签名详情策略。

失败时返回 success=False，由 DouyinParser 继续尝试后续原有策略；
不使用也不覆盖用户登录 Cookie。
"""

from __future__ import annotations

import re

from astrbot.api import logger

from .base import BaseStrategy, StrategyParams
from ..model import DouyinParseResult, parse_aweme_detail
from ..identity import get_douyin_identity
from ..utils.url import AwemeIdFetcher


class AnonymousWebStrategy(BaseStrategy):
    @property
    def name(self) -> str:
        return "anonymous_web"

    @staticmethod
    def _pick_target_detail(data: dict, aweme_id: str):
        detail = data.get("aweme_detail")
        if isinstance(detail, dict) and str(
            detail.get("aweme_id") or detail.get("id") or ""
        ) == str(aweme_id):
            return detail
        for key in ("aweme_details", "aweme_list", "item_list"):
            items = data.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and str(
                    item.get("aweme_id") or item.get("id") or ""
                ) == str(aweme_id):
                    return item
        return None

    async def execute(self, params: StrategyParams) -> DouyinParseResult:
        url_match = re.search(r"(https?://[^\s]+)", params.url or "")
        if not url_match:
            return DouyinParseResult(
                success=False, error="未找到有效的 URL", source=self.name
            )
        url = url_match.group(1)
        try:
            aweme_id = await AwemeIdFetcher().get_aweme_id(url)
        except Exception as e:
            return DouyinParseResult(
                success=False,
                error=f"提取 aweme_id 失败: {e}",
                source=self.name,
            )
        try:
            data = await get_douyin_identity().fetch_detail(aweme_id, referer=url)
        except Exception as e:
            logger.debug(f"匿名策略异常，继续后续策略: {type(e).__name__}: {e}")
            return DouyinParseResult(
                success=False,
                error=f"匿名详情请求失败: {type(e).__name__}",
                source=self.name,
            )
        if not data:
            return DouyinParseResult(
                success=False,
                error="匿名开放接口与签名会话均未返回有效详情",
                source=self.name,
            )
        detail = self._pick_target_detail(data, aweme_id)
        if not detail:
            # 二次确认响应确实对应目标作品，避免错配内容进入后续下载
            return DouyinParseResult(
                success=False,
                error="匿名详情响应不含目标作品",
                source=self.name,
            )
        result = parse_aweme_detail(detail, aweme_id, self.name)
        if result.success:
            logger.debug(f"抖音解析成功: strategy={self.name}")
        return result
