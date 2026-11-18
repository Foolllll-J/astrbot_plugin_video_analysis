"""抖音匿名 Web 身份运行时：ttwid TTL / 并发刷新合并 / generation / 失败冷却。

模块级单例，只维护匿名 ttwid，不写入或覆盖用户登录 Cookie。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from enum import Enum
from http.cookies import CookieError, SimpleCookie
from typing import Any, Optional
from urllib.parse import quote, urlencode

import httpx

from astrbot.api import logger

from ..download_io import LoopLocalLock
from .sign import ABogus

DOUYIN_WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
TTWID_REGISTER_URL = "https://ttwid.bytedance.com/ttwid/union/register/"
OPEN_ORIGIN = "https://open.douyin.com"
DEFAULT_REFERER = "https://www.douyin.com/"

DEFAULT_TTWID_TTL = 6 * 60 * 60
IDENTITY_REFRESH_FAILURE_COOLDOWN = 1.0
DETAIL_MAX_ATTEMPTS = 4
DETAIL_RETRY_BASE_DELAY = 0.25
REQUEST_TIMEOUT = 15.0


class _RetryAction(Enum):
    STOP = "stop"
    RETRY = "retry"
    RESIGN = "resign"
    IDENTITY_REJECTED = "identity_rejected"


@dataclass(frozen=True)
class _WebIdentity:
    ttwid: str
    expires_at: float
    generation: int


class DouyinAnonymousIdentity:
    """长期持有的匿名 Web 身份与有界重试详情请求。"""

    def __init__(self) -> None:
        self._identity: Optional[_WebIdentity] = None
        self._generation = 0
        self._lock = LoopLocalLock()
        self._failed_refresh_generation = -1
        self._failed_refresh_until = 0.0

    @staticmethod
    def _build_params(item_id: str) -> dict:
        return {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "aweme_id": str(item_id),
        }

    @staticmethod
    def _build_open_params(item_id: str) -> dict:
        return {"aweme_id": str(item_id), "aid": "6383"}

    @staticmethod
    def _registration_payload() -> dict:
        return {
            "region": "cn",
            "aid": 1768,
            "needFid": False,
            "service": "www.ixigua.com",
            "migrate_info": {"ticket": "", "source": "node"},
            "cbUrlProtocol": "https",
            "union": True,
        }

    @staticmethod
    def _parse_ttwid(response: httpx.Response) -> tuple[str, int]:
        value = response.cookies.get("ttwid")
        max_age = 0
        for header in response.headers.get_list("set-cookie"):
            cookie = SimpleCookie()
            try:
                cookie.load(header)
            except CookieError:
                continue
            morsel = cookie.get("ttwid")
            if morsel is None or not morsel.value:
                continue
            if not value:
                value = morsel.value
            try:
                max_age = int(morsel["max-age"] or 0)
            except (TypeError, ValueError):
                max_age = 0
            break
        return str(value or ""), max_age

    def _current_identity(self) -> Optional[_WebIdentity]:
        identity = self._identity
        if identity is not None and time.monotonic() < identity.expires_at:
            return identity
        return None

    def _record_refresh_failure(self, force_refresh: bool, generation: int) -> None:
        if not force_refresh:
            return
        self._failed_refresh_generation = generation
        self._failed_refresh_until = (
            time.monotonic() + IDENTITY_REFRESH_FAILURE_COOLDOWN
        )

    async def _get_identity(
        self,
        client: httpx.AsyncClient,
        *,
        force_refresh: bool = False,
        stale_generation: int = -1,
    ) -> Optional[_WebIdentity]:
        current = self._current_identity()
        if not force_refresh and current is not None:
            return current

        async with self._lock.get():
            current = self._current_identity()
            if not force_refresh and current is not None:
                return current
            if force_refresh:
                # 并发失败已被其他请求刷新：直接复用
                if current is not None and current.generation != stale_generation:
                    return current
                if (
                    self._failed_refresh_generation == stale_generation
                    and time.monotonic() < self._failed_refresh_until
                ):
                    return None

            headers = {
                "User-Agent": DOUYIN_WEB_UA,
                "Content-Type": "application/json; charset=utf-8",
            }
            try:
                response = await client.post(
                    TTWID_REGISTER_URL,
                    json=self._registration_payload(),
                    headers=headers,
                )
                if response.status_code >= 400:
                    self._record_refresh_failure(force_refresh, stale_generation)
                    return None
                ttwid, max_age = self._parse_ttwid(response)
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, ValueError):
                self._record_refresh_failure(force_refresh, stale_generation)
                return None

            if not ttwid:
                self._record_refresh_failure(force_refresh, stale_generation)
                return None
            ttl = min(max_age if max_age > 0 else DEFAULT_TTWID_TTL, DEFAULT_TTWID_TTL)
            self._generation += 1
            identity = _WebIdentity(
                ttwid=ttwid,
                expires_at=time.monotonic() + max(ttl, 60),
                generation=self._generation,
            )
            self._identity = identity
            self._failed_refresh_generation = -1
            self._failed_refresh_until = 0.0
            return identity

    @staticmethod
    def _contains_target(data: dict, item_id: str) -> bool:
        candidates: list[dict] = []
        detail = data.get("aweme_detail")
        if isinstance(detail, dict):
            candidates.append(detail)
        for key in ("aweme_details", "aweme_list", "item_list"):
            value = data.get(key)
            if isinstance(value, list):
                candidates.extend(v for v in value if isinstance(v, dict))
        return any(
            str(item.get("aweme_id") or item.get("id") or "") == str(item_id)
            for item in candidates
        )

    @staticmethod
    def _valid_payload(data: Any, item_id: str) -> Optional[dict]:
        if not isinstance(data, dict):
            return None
        if data.get("status_code") not in (None, 0, "0"):
            return None
        if not DouyinAnonymousIdentity._contains_target(data, item_id):
            return None
        return data

    async def _fetch_open_detail(
        self, client: httpx.AsyncClient, item_id: str
    ) -> Optional[dict]:
        """开放接口上下文：无签名、无登录 Cookie 的最小详情请求。"""
        headers = {
            "User-Agent": DOUYIN_WEB_UA,
            "Origin": OPEN_ORIGIN,
            "Referer": f"{OPEN_ORIGIN}/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        try:
            response = await client.get(
                DETAIL_API,
                params=self._build_open_params(item_id),
                headers=headers,
            )
            if response.status_code >= 400:
                return None
            body = response.text
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError:
            return None
        if not body or not body.lstrip().startswith("{"):
            return None
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None
        return self._valid_payload(data, item_id)

    async def _request_signed_once(
        self,
        client: httpx.AsyncClient,
        item_id: str,
        referer: str,
        identity: _WebIdentity,
    ) -> tuple[Optional[dict], _RetryAction]:
        params = self._build_params(item_id)
        param_string = urlencode(params)
        try:
            signature = ABogus().get_value(params)
        except Exception as e:
            logger.debug(f"匿名详情 A-Bogus 生成失败: {e}")
            return None, _RetryAction.STOP
        url = f"{DETAIL_API}?{param_string}&a_bogus={quote(signature, safe='')}"
        headers = {
            "User-Agent": DOUYIN_WEB_UA,
            "Referer": referer or DEFAULT_REFERER,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            # 只使用匿名 ttwid，不拼接/覆盖用户登录 Cookie
            "Cookie": f"ttwid={identity.ttwid}",
        }
        try:
            response = await client.get(url, headers=headers)
            status = response.status_code
            if status == 401:
                return None, _RetryAction.IDENTITY_REJECTED
            if status == 403:
                body = response.text.lower()
                if "uifid" in body and "not found" in body:
                    return None, _RetryAction.IDENTITY_REJECTED
                return None, _RetryAction.RESIGN
            if status == 408 or status >= 500:
                return None, _RetryAction.RETRY
            if status >= 400:
                return None, _RetryAction.STOP
            body = response.text
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError:
            return None, _RetryAction.RETRY
        if not body or not body.lstrip().startswith("{"):
            return None, _RetryAction.RESIGN
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return None, _RetryAction.RESIGN
        payload = self._valid_payload(data, item_id)
        if payload is None:
            return None, _RetryAction.RESIGN
        return payload, _RetryAction.STOP

    async def _fetch_signed_detail(
        self, client: httpx.AsyncClient, item_id: str, referer: str = ""
    ) -> Optional[dict]:
        identity = await self._get_identity(client)
        if identity is None:
            identity = await self._get_identity(client, force_refresh=True)
        if identity is None:
            return None

        refreshed = False
        rejections = 0
        transient_retry_used = False
        for attempt in range(DETAIL_MAX_ATTEMPTS):
            data, action = await self._request_signed_once(
                client, item_id, referer, identity
            )
            if data is not None or action is _RetryAction.STOP:
                return data
            if action is _RetryAction.RETRY:
                rejections = 0
                if transient_retry_used:
                    return None
                transient_retry_used = True
            elif action is _RetryAction.IDENTITY_REJECTED:
                rejections += 1
            else:
                rejections = 0
            if attempt + 1 >= DETAIL_MAX_ATTEMPTS:
                break

            if (
                action is _RetryAction.IDENTITY_REJECTED
                and rejections >= 2
                and not refreshed
            ):
                new_identity = await self._get_identity(
                    client,
                    force_refresh=True,
                    stale_generation=identity.generation,
                )
                refreshed = True
                rejections = 0
                if new_identity is not None:
                    identity = new_identity
                else:
                    logger.debug("刷新抖音匿名身份失败，复用当前身份继续重试")
            delay = DETAIL_RETRY_BASE_DELAY * (2**attempt)
            await asyncio.sleep(delay)
        logger.debug("抖音匿名详情请求达到最大尝试次数")
        return None

    async def fetch_detail(self, item_id: str, referer: str = "") -> Optional[dict]:
        """先开放接口，再匿名签名会话；都失败返回 None 交给后续策略。"""
        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT, follow_redirects=True
            ) as client:
                data = await self._fetch_open_detail(client, item_id)
                if data is not None:
                    return data
                logger.debug(f"开放接口未返回目标作品详情，回退匿名签名会话: {item_id}")
                return await self._fetch_signed_detail(client, item_id, referer)
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as e:
            logger.debug(f"抖音匿名详情请求异常: {type(e).__name__}")
            return None


_IDENTITY_RUNTIME = DouyinAnonymousIdentity()


def get_douyin_identity() -> DouyinAnonymousIdentity:
    """插件长期持有的匿名身份运行时（模块级单例）。"""
    return _IDENTITY_RUNTIME
