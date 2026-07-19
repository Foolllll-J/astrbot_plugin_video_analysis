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
)

COOKIE_FILE: str | None = None
COOKIE_VALID: bool | None = None


def init_bili_module(cookie_file_path: str):
    global COOKIE_FILE
    COOKIE_FILE = cookie_file_path
    os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
    logger.debug(f"bilibili 模块已初始化，Cookie 路径: {COOKIE_FILE}")


def estimate_size(quality_qn: int, duration_seconds: int) -> float:
    bitrate_mbps = ESTIMATED_BITRATES_MBPS.get(quality_qn, 1.0)
    return (bitrate_mbps * duration_seconds) / 8


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


async def bili_request(url: str, return_json: bool = True):
    if not url or not isinstance(url, str):
        return {"code": -400, "message": "Invalid URL"}
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=DEFAULT_HEADERS) as response:
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


async def generate_qrcode() -> dict | None:
    url = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    data = await bili_request(url)
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

    logger.info("B站登录二维码已生成")
    return {
        "qrcode_key": qr_data["qrcode_key"],
        "image_base64": img_str,
        "url": qr_data["url"],
    }


async def check_login_status(qrcode_key: str) -> dict:
    url = f"https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={qrcode_key}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Accept-Encoding": "gzip, deflate",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                return await response.json()
    except aiohttp.ClientError:
        return {"code": -1, "message": "检查登录状态失败"}


async def check_login_status_loop(qrcode_key: str) -> dict | None:
    logger.info("等待登录...（最多40秒）")
    for _ in range(40):
        await asyncio.sleep(1)
        status = await check_login_status(qrcode_key)
        if status.get("code") == 0:
            data = status.get("data", {})
            if data.get("code") == 0:
                logger.info("B站登录成功!")
                try:
                    url = data.get("url", "")
                    cookies = {}
                    if "?" in url:
                        url_params = url.split("?")[1]
                        for param in url_params.split("&"):
                            if "=" in param:
                                key, value = param.split("=", 1)
                                useful_keys = [
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
                                    "fingerprint",
                                ]
                                if key in useful_keys:
                                    cookies[key] = unquote(value)
                        if not cookies.get("SESSDATA") or not cookies.get("DedeUserID"):
                            raise ValueError("获取的 Cookie 格式异常")
                        await save_cookies_dict(cookies)
                        return cookies
                    else:
                        raise ValueError("URL 格式异常，无法提取参数")
                except Exception as e:
                    logger.error(f"登录异常: {e}")
                    logger.debug(f"原始响应数据: {data}")
                    return None
            elif data.get("code") == -2:
                logger.warning("二维码已过期，请重新获取")
                return None
            elif data.get("code") in (-4, -5):
                logger.debug("等待手机上确认登录")
    logger.warning("登录超时，请重试")
    return None


async def bili_login() -> tuple:
    logger.info("正在生成 B站 登录二维码...")
    qr_data = await generate_qrcode()
    if not qr_data:
        return None, None

    logger.info("B站 登录二维码已生成，等待扫码...")
    qrcode_key = qr_data["qrcode_key"]
    login_task = asyncio.create_task(check_login_status_loop(qrcode_key))
    return login_task, qr_data
