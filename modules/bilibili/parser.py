import re

import aiohttp

from astrbot.api import logger

from .constants import (
    REG_BV,
    REG_AV,
    REG_BILI_LIVE,
    REG_BILI_DYNAMIC,
    REG_BILI_SPACE,
    API_BY_AID,
    API_BY_BVID,
)
from .model import BiliVideoInfo
from .utils import bili_request, format_number


class UnsupportedBiliLinkError(Exception):
    pass


def _extract_aid(raw: str) -> str | None:
    s = str(raw or "").strip().lower()
    if not s.startswith("av"):
        return None
    m = re.search(r"\d+", s)
    return m.group(0) if m else None


def av2bv(av: str) -> str | None:
    match = REG_AV.search(str(av or ""))
    return match.group(0) if match else None


async def parse_video(bvid: str) -> BiliVideoInfo | None:
    if REG_AV.search(str(bvid or "")):
        aid = _extract_aid(bvid)
        api_url = API_BY_AID.format(aid)
    else:
        api_url = API_BY_BVID.format(bvid)

    data = await bili_request(api_url)
    if data.get("code") != 0:
        logger.warning(
            f"Bilibili API 返回错误: code={data.get('code')}, message={data.get('message', '')}"
        )
        return None

    info = data["data"]
    bvid = info.get("bvid", bvid)
    stats = {
        "view": format_number(info["stat"]["view"]),
        "like": format_number(info["stat"]["like"]),
        "danmaku": format_number(info["stat"]["danmaku"]),
        "coin": format_number(info["stat"]["coin"]),
        "favorite": format_number(info["stat"]["favorite"]),
    }

    return BiliVideoInfo(
        aid=info["aid"],
        cid=info["cid"],
        bvid=bvid,
        title=info["title"],
        cover=info["pic"],
        duration=info["duration"],
        stats=stats,
    )


async def parse_b23(short_url: str) -> BiliVideoInfo | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(
                f"https://{short_url}", allow_redirects=True
            ) as response:
                real_url = str(response.url)
                if REG_BILI_LIVE.search(real_url):
                    logger.debug(
                        f"短链解析到 Bilibili 直播间，不支持解析下载: {real_url}"
                    )
                    raise UnsupportedBiliLinkError(
                        "该链接为 Bilibili 直播间，当前不支持解析下载"
                    )
                if REG_BILI_DYNAMIC.search(real_url):
                    logger.debug(
                        f"短链解析到 Bilibili 动态，不支持解析下载: {real_url}"
                    )
                    raise UnsupportedBiliLinkError(
                        "该链接为 Bilibili 动态，当前不支持解析下载"
                    )
                if REG_BILI_SPACE.search(real_url):
                    logger.debug(
                        f"短链解析到 Bilibili 个人空间，不支持解析下载: {real_url}"
                    )
                    raise UnsupportedBiliLinkError(
                        "该链接为 Bilibili 个人空间，当前不支持解析下载"
                    )

                if REG_BV.search(real_url):
                    return await parse_video(REG_BV.search(real_url).group())
                if REG_AV.search(real_url):
                    return await parse_video(av2bv(REG_AV.search(real_url).group()))
                return None
    except aiohttp.ClientError as e:
        logger.warning(f"B23 短链解析网络错误: {e}")
        return None
