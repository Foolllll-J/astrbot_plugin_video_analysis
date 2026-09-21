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
from .utils import bili_request, format_number, build_request_cookies


class UnsupportedBiliLinkError(Exception):
    pass


def _extract_aid(raw: str) -> str | None:
    s = str(raw or "").strip().lower()
    if not s.startswith("av"):
        return None
    m = re.search(r"\d+", s)
    return m.group(0) if m else None


# av 号 → BV 号确定性转换（B站现行算法，兼容新旧 av 号，非网络请求）
_AV2BV_ALPHABET = "FcwAPNKTMug3GV5Lj7EJnHpWsx4tb8haYeviqBz6rkCy12mUSDQX9RdoZf"
_AV2BV_XOR = 23442827791579
_AV2BV_MAX_AID = 1 << 51
_AV2BV_ENCODE_MAP = (8, 7, 0, 5, 1, 3, 2, 4, 6)


def av2bv(av: str) -> str | None:
    """将 av 号（如 av116781352032938）转换为规范 BV 号；无法解析时返回 None。"""
    match = REG_AV.search(str(av or ""))
    if not match:
        return None
    try:
        aid = int(match.group(0)[2:])
    except (ValueError, IndexError):
        return None
    if aid > _AV2BV_MAX_AID:
        return None
    bvid = [""] * 9
    tmp = (_AV2BV_MAX_AID | aid) ^ _AV2BV_XOR
    for i in range(9):
        bvid[_AV2BV_ENCODE_MAP[i]] = _AV2BV_ALPHABET[tmp % 58]
        tmp //= 58
    return "BV1" + "".join(bvid)


async def parse_av(av_str: str) -> BiliVideoInfo | None:
    """av 解析：优先本地算法转 BV；算法失败/失效时回退官方 aid API。"""
    bvid = av2bv(av_str)
    if bvid:
        info = await parse_video(bvid)
        if info:
            return info
    return await parse_video(av_str)


async def parse_video(bvid: str) -> BiliVideoInfo | None:
    bvid = str(bvid or "").strip()
    if REG_AV.fullmatch(bvid):
        aid = _extract_aid(bvid)
        api_url = API_BY_AID.format(aid)
    else:
        api_url = API_BY_BVID.format(bvid)

    data = await bili_request(api_url, cookies=await build_request_cookies())
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

    owner_name = (info.get("owner") or {}).get("name", "")

    return BiliVideoInfo(
        aid=info["aid"],
        cid=info["cid"],
        bvid=bvid,
        title=info["title"],
        cover=info["pic"],
        duration=info["duration"],
        stats=stats,
        owner_name=owner_name,
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
                    return await parse_av(REG_AV.search(real_url).group())
                return None
    except aiohttp.ClientError as e:
        logger.warning(f"B23 短链解析网络错误: {e}")
        return None
