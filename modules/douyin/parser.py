"""
抖音解析门面类 DouyinParser

职责：
1. 编排策略链（Web API → 第三方 API）
2. 管理 Cookie 源（配置 > 文件）
3. 提供 parse() 统一入口
"""

import json
import os
import re
from typing import Callable, Awaitable

import aiofiles
import httpx

from astrbot.api import logger
from astrbot.api.message_components import Node, Plain, Nodes
import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent

from .model import DouyinParseResult
from .strategies.base import StrategyParams
from .strategies.web_api import WebApiStrategy
from .strategies.share_page import SharePageStrategy
from .strategies.third_party import ThirdPartyStrategy
from .strategies.mobile_api import MobileApiStrategy, set_device_cache_dir
from .strategies.anonymous_web import AnonymousWebStrategy
from .identity import get_douyin_identity
from .utils.cookie import extract_and_format_cookies


_COOKIE_FILE_PATH: str | None = None


def init_douyin_login(data_dir: str) -> None:
    global _COOKIE_FILE_PATH
    _COOKIE_FILE_PATH = os.path.join(data_dir, "douyin_cookies.json")
    set_device_cache_dir(data_dir)
    # 初始化长期持有的匿名身份运行时，避免每次解析重建
    get_douyin_identity()


async def _load_douyin_cookies_from_file() -> str | None:
    if not _COOKIE_FILE_PATH or not os.path.exists(_COOKIE_FILE_PATH):
        return None
    try:
        async with aiofiles.open(_COOKIE_FILE_PATH, "r", encoding="utf-8") as f:
            content = await f.read()
        if not content.strip():
            return None
        cookies = json.loads(content)
        if not isinstance(cookies, dict) or not cookies:
            return None
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items() if k and v)
        if not cookie_str:
            return None
        return extract_and_format_cookies(cookie_str)
    except Exception as e:
        logger.error(f"加载抖音 Cookie 文件失败: {e}")
        return None


async def get_effective_douyin_cookie(
    *,
    cookie_loaded: bool,
    cookie_from_config: str,
    cookie_from_file: str,
) -> tuple[str, bool, str]:
    resolved_file_cookie = cookie_from_file
    resolved_loaded = cookie_loaded

    if not resolved_loaded:
        resolved_file_cookie = (await _load_douyin_cookies_from_file()) or ""
        resolved_loaded = True

    effective_cookie = cookie_from_config or resolved_file_cookie or ""
    return effective_cookie, resolved_loaded, resolved_file_cookie


class DouyinParser:
    def __init__(self, cookie: str = "", api_url: str = "", data_dir: str = ""):
        self._cookie = cookie
        self._api_url = api_url

        self._strategies = []
        if cookie:
            self._strategies.append(WebApiStrategy())
        # 匿名策略作为额外一层，失败后继续原有策略链
        self._strategies.append(AnonymousWebStrategy())
        self._strategies += [
            MobileApiStrategy(),
            SharePageStrategy(),
            ThirdPartyStrategy(),
        ]

    @classmethod
    def from_config(
        cls, cookie: str = "", api_url: str = "", data_dir: str = ""
    ) -> "DouyinParser":
        return cls(cookie=cookie, api_url=api_url, data_dir=data_dir)

    _LIVE_URL_RE = re.compile(r"webcast\.amemv\.com|/webcast/|/live/")
    _USER_URL_RE = re.compile(r"(?:douyin\.com|iesdouyin\.com)/(?:share/)?user/")

    @staticmethod
    async def _check_live_url(url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                r = await client.get(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 Chrome/144.0.0.0 Safari/537.36"
                        )
                    },
                )
                return bool(DouyinParser._LIVE_URL_RE.search(str(r.url)))
        except Exception:
            return False

    @staticmethod
    async def _check_user_url(url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                r = await client.get(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 Chrome/144.0.0.0 Safari/537.36"
                        )
                    },
                )
                return bool(DouyinParser._USER_URL_RE.search(str(r.url)))
        except Exception:
            return False

    async def parse(self, url: str) -> DouyinParseResult:
        if await self._check_live_url(url):
            return DouyinParseResult(
                success=False, error="该链接为抖音直播间，暂不支持下载"
            )
        if await self._check_user_url(url):
            return DouyinParseResult(
                success=False, error="该链接为抖音用户主页，暂不支持下载"
            )

        params = StrategyParams(url=url, cookie=self._cookie, api_url=self._api_url)

        for strategy in self._strategies:
            try:
                result = await strategy.execute(params)
                if result.success:
                    logger.debug(f"抖音解析成功: strategy={strategy.name}")
                    return result
                logger.debug(f"抖音解析 {strategy.name} 失败: {result.error}")
            except Exception as e:
                logger.debug(f"抖音解析 {strategy.name} 异常，继续尝试后续策略: {e}")

        return DouyinParseResult(
            success=False,
            error="所有解析方式均失败",
        )


def format_douyin_failure_message(result: dict | None) -> str:
    user_message = "抱歉，抖音解析失败。"
    if not result:
        logger.error("抖音解析失败: empty result")
        return user_message

    failures = result.get("failure_info") or []
    error_message = result.get("error") or "unknown error"

    hint = (
        "可能是账号异常导致 Cookie 失效，建议尝试以下操作："
        "1. 重新抓取最新的 Cookie 并更新配置；"
        "2. 更换其他可用账号的 Cookie。"
    )
    if failures:
        logger.error(
            f"抖音解析失败详情: error={error_message}, failures={json.dumps(failures, ensure_ascii=False)}。{hint}"
        )
    else:
        logger.error(f"抖音解析失败详情: error={error_message}。{hint}")

    return user_message


async def send_douyin_with_title_forward(
    event: AstrMessageEvent,
    title: str,
    result: dict,
    set_emoji_fn: Callable[[int, bool], Awaitable[None]] | None = None,
    text_sender_name: str = "抖音文案",
    media_sender_name: str = "抖音内容",
) -> None:
    """标题超长时组合转发；await 实际发送，支持重试。"""
    from ..send_utils import (
        deliver_items,
        finish_delivery_report,
        send_chain_with_retry,
        send_plain,
    )

    sender_id = event.get_self_id()
    title_node = Node(uin=sender_id, name=text_sender_name, content=[Plain(title)])

    ordered_media = list(result.get("ordered_media", []) or [])
    if not ordered_media:
        for p in result.get("image_paths", []) or []:
            ordered_media.append({"path": p, "type": "image"})
        for p in result.get("video_paths", []) or []:
            ordered_media.append({"path": p, "type": "video"})
    if (
        not ordered_media
        and result.get("video_path")
        and os.path.exists(result["video_path"])
    ):
        ordered_media.append({"path": result["video_path"], "type": "video"})

    async def _fail_notice(text: str) -> None:
        await send_plain(event, text, attempts=1, delay=0, logger_obj=logger)
        if set_emoji_fn:
            await set_emoji_fn(424, False)
            await set_emoji_fn(357)

    if not ordered_media:
        logger.error("标题转发：没有找到媒体文件")
        await _fail_notice(title)
        return

    media_contents: list[list] = []
    for item in ordered_media:
        media_path = item["path"]
        if not os.path.exists(media_path):
            continue
        try:
            if item["type"] == "image":
                component = Comp.Image.fromFileSystem(path=media_path)
            else:
                component = Comp.Video.fromFileSystem(path=media_path)
            media_contents.append([component])
        except Exception as e:
            logger.error(f"标题转发处理媒体文件出错: {e}")

    if not media_contents:
        logger.error("标题转发：无法加载任何媒体文件")
        await _fail_notice(title)
        return

    media_nodes = [
        Node(uin=sender_id, name=media_sender_name, content=content)
        for content in media_contents
    ]
    all_nodes = [title_node] + media_nodes

    if event.get_platform_name() == "aiocqhttp":
        merged = Nodes(nodes=all_nodes)
        ok = await send_chain_with_retry(
            event,
            [merged],
            attempts=2,
            delay=1,
            label="标题转发",
            logger_obj=logger,
        )
        if ok:
            logger.debug(f"标题转发成功：{len(media_nodes)} 个媒体（合并转发）")
            report = "ok"
        else:
            logger.warning("标题转发合并失败，降级为标题 + 逐项媒体发送")
            await send_plain(event, title, attempts=1, delay=0, logger_obj=logger)
            succeeded, _ = await deliver_items(
                event,
                media_contents,
                attempts=2,
                delay=1,
                label="标题转发媒体",
                logger_obj=logger,
            )
            report = await finish_delivery_report(
                event,
                succeeded=succeeded,
                total=len(media_contents),
                label="标题转发媒体",
                logger_obj=logger,
            )
    else:
        logger.debug("当前平台不支持合并转发，标题转发降级为依次发送")
        await send_plain(event, title, attempts=1, delay=0, logger_obj=logger)
        succeeded, _ = await deliver_items(
            event,
            media_contents,
            attempts=2,
            delay=1,
            label="标题转发媒体",
            logger_obj=logger,
        )
        report = await finish_delivery_report(
            event,
            succeeded=succeeded,
            total=len(media_contents),
            label="标题转发媒体",
            logger_obj=logger,
        )

    if set_emoji_fn:
        await set_emoji_fn(424, False)
        await set_emoji_fn(357 if report == "failed" else 124)
