import asyncio
import base64
import json
import os
from io import BytesIO
from urllib.parse import unquote

import aiofiles
import aiohttp
import qrcode

from astrbot.api import logger

from .constants import (
    ESTIMATED_BITRATES_MBPS,
    DEFAULT_HEADERS,
    COOKIE_CHECK_HEADERS,
    BUVID_API,
)

COOKIE_FILE: str | None = None
COOKIE_VALID: bool | None = None

# 登录成功后需保存的 Cookie 字段（来自轮询跳转 url 参数或 Set-Cookie）
LOGIN_COOKIE_KEYS = {
    "_uuid",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "SESSDATA",
    "bili_jct",
    "bili_ticket",
    "bili_ticket_expires",
    "CURRENT_FNVAL",
    "CURRENT_QUALITY",
    "enable_feed_channel",
    "enable_web_push",
    "header_theme_version",
    "home_feed_column",
    "LIVE_BUVID",
    "PVID",
    "browser_resolution",
    "buvid_fp",
    "buvid3",
    "buvid4",
    "fingerprint",
}

_BUVID_CACHE: tuple[str, str] | None = None


async def get_buvid() -> tuple[str, str]:
    """获取并缓存 buvid3/buvid4，失败时返回空元组不阻断调用。"""
    global _BUVID_CACHE
    if _BUVID_CACHE:
        return _BUVID_CACHE
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(BUVID_API, headers=DEFAULT_HEADERS) as response:
                data = await response.json()
        b_3 = str(data.get("data", {}).get("b_3", ""))
        b_4 = str(data.get("data", {}).get("b_4", ""))
        if b_3 and b_4:
            _BUVID_CACHE = (b_3, b_4)
            return _BUVID_CACHE
    except Exception as e:
        logger.warning(f"获取 buvid 失败: {e}")
    return ("", "")


async def build_request_cookies(use_login: bool = True) -> dict:
    """组装请求 Cookie：buvid 加上可选的登录 Cookie，降低匿名风控（412/-352）。"""
    b_3, b_4 = await get_buvid()
    cookies = {}
    if b_3:
        cookies["buvid3"] = b_3
    if b_4:
        cookies["buvid4"] = b_4
    if use_login:
        saved = await load_cookies() or {}
        for key in ("SESSDATA", "bili_jct", "DedeUserID"):
            if saved.get(key):
                cookies[key] = str(saved[key])
    return cookies


def init_bili_module(cookie_file_path: str):
    global COOKIE_FILE
    COOKIE_FILE = cookie_file_path
    os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
    logger.debug(f"bilibili 模块已初始化，Cookie 路径: {COOKIE_FILE}")


def estimate_size(quality_qn: int, duration_seconds: int) -> float:
    bitrate_mbps = ESTIMATED_BITRATES_MBPS.get(quality_qn, 1.0)
    return (bitrate_mbps * duration_seconds) / 8


def estimate_size_with_plan(
    quality_qn: int, duration_seconds: int, plan: dict
) -> float:
    """优先使用真实码率探测结果估算体积(MB)，缺失时回退静态估算。"""
    qualities = (plan or {}).get("qualities", {}) or {}
    # 先按本档真实带宽估算；116(1080P60) 无独立带宽时按同级 112(1080P+) 码率近似
    best_id = max([q for q in qualities if q <= quality_qn], default=None)
    info = qualities.get(best_id) if best_id is not None else None
    if info and info.get("bandwidth"):
        return (int(info["bandwidth"]) * duration_seconds) / 8 / (1024 * 1024)
    fallback_qn = 112 if quality_qn == 116 else quality_qn
    return estimate_size(fallback_qn, duration_seconds)


def map_quality_to_height(quality_code: int) -> int:
    if quality_code >= 120:
        return 120
    if quality_code >= 112:
        return 112
    if quality_code >= 80:
        return 80
    if quality_code >= 64:
        return 64
    if quality_code >= 32:
        return 32
    if quality_code >= 16:
        return 16
    return 80


def format_number(num):
    num = int(num)
    if num < 1e4:
        return str(num)
    if num < 1e8:
        return f"{num / 1e4:.1f}万"
    return f"{num / 1e8:.1f}亿"


async def bili_request(url: str, return_json: bool = True, cookies: dict | None = None):
    if not url or not isinstance(url, str):
        return {"code": -400, "message": "Invalid URL"}
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url, headers=DEFAULT_HEADERS, cookies=cookies or None
            ) as response:
                response.raise_for_status()
                if return_json:
                    data = await response.json()
                    if not isinstance(data, dict):
                        return {"code": -400, "message": "Invalid JSON format"}
                    return data
                return await response.read()
    except aiohttp.ClientError as e:
        return {"code": -400, "message": f"Network error: {str(e)}"}
    except asyncio.TimeoutError:
        return {"code": -400, "message": "Request timeout"}


async def load_cookies() -> dict | None:
    if not COOKIE_FILE or not os.path.exists(COOKIE_FILE):
        logger.warning(f"Cookie 文件不存在: {COOKIE_FILE}")
        return None
    try:
        async with aiofiles.open(COOKIE_FILE, "r", encoding="utf-8") as f:
            content = await f.read()
        if not content.strip():
            logger.warning("Cookie 文件为空")
            return None
        return json.loads(content)
    except json.JSONDecodeError:
        logger.error("Cookie 文件格式错误")
        return None
    except Exception as e:
        logger.error(f"加载 Cookie 失败: {e}")
        return None


async def save_cookies_dict(cookies: dict) -> bool:
    try:
        async with aiofiles.open(COOKIE_FILE, "w", encoding="utf-8") as f:
            await f.write(json.dumps(cookies, ensure_ascii=False, indent=2))
        logger.info(f"Cookie 已保存到: {COOKIE_FILE}")
        return True
    except Exception as e:
        logger.error(f"保存 Cookie 失败: {e}")
        return False


async def check_cookie_valid() -> bool:
    global COOKIE_VALID
    COOKIE_VALID = None
    cookies = await load_cookies()
    if not cookies:
        logger.debug("未找到 Cookie 文件或 Cookie 文件为空，需要登录")
        return False

    required_fields = {
        "SESSDATA": lambda v: len(v) > 30 and "," in v,
        "bili_jct": lambda v: len(v) == 32,
        "DedeUserID": lambda v: v.isdigit(),
    }
    for field, validator in required_fields.items():
        if field not in cookies or not validator(str(cookies[field])):
            logger.debug(f"Cookie 字段验证失败: {field}")
            return False

    url = "https://api.bilibili.com/x/member/web/account"
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers = dict(COOKIE_CHECK_HEADERS)
    headers["Cookie"] = cookie_str

    try:
        async with aiohttp.ClientSession() as session:
            timeout = aiohttp.ClientTimeout(total=10)
            async with session.get(url, headers=headers, timeout=timeout) as response:
                data = await response.json()
                if data.get("code") == 0:
                    api_mid = str(data.get("data", {}).get("mid", ""))
                    cookie_mid = str(cookies.get("DedeUserID", ""))
                    if api_mid == cookie_mid:
                        COOKIE_VALID = True
                        return True
                return False
    except Exception as e:
        logger.warning(f"验证 Cookie 有效性时异常: {e}")
        return False


async def generate_qrcode(session: aiohttp.ClientSession) -> dict | None:
    url = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    headers = {
        **DEFAULT_HEADERS,
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }
    try:
        async with session.get(url, headers=headers) as response:
            data = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.error(f"获取二维码请求失败: {e}")
        return None
    if data.get("code") != 0:
        logger.error(f"获取二维码失败: {data.get('message')}")
        return None

    qr_data = data["data"]
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(qr_data["url"])
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode()

    return {
        "qrcode_key": qr_data["qrcode_key"],
        "image_base64": img_str,
        "url": qr_data["url"],
    }


async def check_login_status(session: aiohttp.ClientSession, qrcode_key: str) -> dict:
    url = f"https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={qrcode_key}"
    headers = {
        **DEFAULT_HEADERS,
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
    }
    try:
        async with session.get(url, headers=headers) as response:
            return await response.json()
    except aiohttp.ClientError:
        return {"code": -1, "message": "检查登录状态失败"}


def _collect_login_cookies(session: aiohttp.ClientSession, redirect_url: str) -> dict:
    """从轮询跳转 url 参数和 session cookie jar 两处合并登录 Cookie。

    B站可能把 SESSDATA/bili_jct/DedeUserID 放在 url 参数或 Set-Cookie 头里，
    两处都读才能兼容不同账号与风控状态。
    """
    cookies: dict = {}
    if "?" in redirect_url:
        for param in redirect_url.split("?", 1)[1].split("&"):
            if "=" in param:
                key, value = param.split("=", 1)
                if key in LOGIN_COOKIE_KEYS:
                    cookies[key] = unquote(value)
    for morsel in session.cookie_jar:
        if morsel.key in LOGIN_COOKIE_KEYS and morsel.value:
            cookies[morsel.key] = unquote(morsel.value)
    return cookies


async def check_login_status_loop(
    session: aiohttp.ClientSession, qrcode_key: str
) -> dict | None:
    logger.info("等待登录...（最多150秒）")
    last_code = None
    for _ in range(150):
        await asyncio.sleep(1)
        status = await check_login_status(session, qrcode_key)
        if status.get("code") != 0:
            continue
        data = status.get("data", {})
        poll_code = data.get("code")
        if poll_code == 0:
            logger.info("B站登录成功!")
            try:
                cookies = _collect_login_cookies(session, data.get("url", ""))
                if not cookies.get("SESSDATA") or not cookies.get("DedeUserID"):
                    found = ",".join(sorted(cookies)) or "无"
                    raise ValueError(f"未取到 SESSDATA/DedeUserID，已获取字段: {found}")
                await save_cookies_dict(cookies)
                return cookies
            except Exception as e:
                logger.error(f"登录异常: {e}")
                return None
        elif poll_code in (86038, 86105):
            logger.warning("二维码已过期，请重新获取")
            return None
        elif poll_code != last_code:
            # 仅在状态变化时记录，避免每秒刷同一条
            msg = {86090: "已扫码，等待手机确认", 86101: "等待扫码"}.get(
                poll_code, f"轮询返回未知状态码: {poll_code}"
            )
            logger.debug(msg)
        last_code = poll_code
    logger.warning("登录超时，请重试")
    return None


async def bili_login() -> tuple:
    logger.info("正在生成 B站 登录二维码...")
    # generate 与 poll 共用一个 session，让登录时下发的 buvid 自动接力到轮询请求
    timeout = aiohttp.ClientTimeout(total=20)
    session = aiohttp.ClientSession(timeout=timeout)
    try:
        qr_data = await generate_qrcode(session)
    except Exception as e:
        await session.close()
        logger.error(f"生成二维码异常: {e}")
        return None, None
    if not qr_data:
        await session.close()
        return None, None

    logger.info("B站 登录二维码已生成，等待扫码...")
    qrcode_key = qr_data["qrcode_key"]

    async def _run() -> dict | None:
        try:
            return await check_login_status_loop(session, qrcode_key)
        finally:
            await session.close()

    login_task = asyncio.create_task(_run())
    return login_task, qr_data
