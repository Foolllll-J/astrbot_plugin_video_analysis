import asyncio
import base64
import hashlib
import json
import os
import tempfile
import time
from io import BytesIO
from pathlib import Path
from typing import NamedTuple, Optional
from urllib.parse import unquote, urlencode

import aiofiles
import aiohttp
import qrcode

from astrbot.api import logger

from .constants import (
    ESTIMATED_BITRATES_MBPS,
    DEFAULT_HEADERS,
    COOKIE_CHECK_HEADERS,
    BUVID_API,
    API_BY_AID,
    API_BY_BVID,
    VIEW_WBI_API,
    NAV_API,
    MIXIN_KEY_ENC_TAB,
    WBI_KEY_TTL_SECONDS,
)
from ..download_io import LoopLocalLock, remove_quietly

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


# ---------------------------------------------------------------------------
# 错误分类：网络 / 业务 / 风控，回退只针对风控错误
# ---------------------------------------------------------------------------


class BiliNetworkError(RuntimeError):
    """传输层错误（连接、超时、非 JSON 等）。"""


class BiliBusinessError(RuntimeError):
    """接口明确返回的业务错误（非风控）。"""

    def __init__(self, code, message: str = ""):
        super().__init__(f"code={code} {message}")
        self.code = code


class BiliRiskControlError(RuntimeError):
    """HTTP 412 / code -412 风控错误，允许有限次 WBI 回退。"""


# ---------------------------------------------------------------------------
# WBI 签名（密钥缓存 + TTL）
# ---------------------------------------------------------------------------

_WBI_LOCK = LoopLocalLock()
_WBI_MIXIN_KEY = ""
_WBI_EXPIRES_AT = 0.0


def _extract_key_from_url(url: str) -> str:
    return Path(str(url or "").split("?", 1)[0]).stem


def _get_mixin_key(img_key: str, sub_key: str) -> str:
    raw = img_key + sub_key
    # nav 返回的 img_key/sub_key 各 32 字符；异常响应不能导致越界或生成错误签名。
    if len(raw) <= max(MIXIN_KEY_ENC_TAB):
        raise BiliBusinessError(-1, "WBI 密钥长度异常，拒绝生成签名")
    return "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]


async def get_wbi_mixin_key() -> str:
    """拉取 nav 中的 WBI 密钥并缓存；并发只拉取一次，TTL 到期后刷新。"""
    global _WBI_MIXIN_KEY, _WBI_EXPIRES_AT
    async with _WBI_LOCK.get():
        if _WBI_MIXIN_KEY and time.monotonic() < _WBI_EXPIRES_AT:
            return _WBI_MIXIN_KEY
        headers = dict(DEFAULT_HEADERS)
        headers["Accept"] = "application/json, text/plain, */*"
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.get(NAV_API, headers=headers) as resp:
                    if resp.status == 412:
                        raise BiliRiskControlError("nav 接口 HTTP 412")
                    if resp.status >= 400:
                        raise BiliNetworkError(f"nav 接口 HTTP {resp.status}")
                    payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise BiliNetworkError(f"获取 WBI 密钥网络失败: {type(e).__name__}") from e
        if not isinstance(payload, dict):
            raise BiliBusinessError(-1, "nav 响应格式异常")
        wbi_img = (payload.get("data") or {}).get("wbi_img") or {}
        img_key = _extract_key_from_url(str(wbi_img.get("img_url", "")))
        sub_key = _extract_key_from_url(str(wbi_img.get("sub_url", "")))
        if not img_key or not sub_key:
            raise BiliBusinessError(-1, "nav 未返回 img_key/sub_key")
        mixin_key = _get_mixin_key(img_key, sub_key)
        _WBI_MIXIN_KEY = mixin_key
        _WBI_EXPIRES_AT = time.monotonic() + WBI_KEY_TTL_SECONDS
        return mixin_key


def sign_wbi_params(params: dict, mixin_key: str) -> dict:
    """按 WBI 规则为参数追加 wts / w_rid。"""
    signed = dict(params)
    signed["wts"] = int(time.time())
    signed = dict(sorted(signed.items(), key=lambda item: item[0]))
    filtered: dict = {}
    for key, value in signed.items():
        text = str(value)
        for ch in "!'()*":
            text = text.replace(ch, "")
        filtered[key] = text
    query = urlencode(filtered)
    filtered["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return filtered


async def wbi_signed_params(params: dict) -> dict:
    return sign_wbi_params(params, await get_wbi_mixin_key())


# ---------------------------------------------------------------------------
# 基础请求与工具
# ---------------------------------------------------------------------------


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
    """按真实选中的视频流 + 音频流带宽估算体积(MB)，缺失时回退静态估算。"""
    plan = plan or {}
    qualities = plan.get("qualities", {}) or {}
    best_id = max([q for q in qualities if q <= quality_qn], default=None)
    info = qualities.get(best_id) if best_id is not None else None
    video_bw = int(info.get("bandwidth") or 0) if isinstance(info, dict) else 0
    try:
        audio_bw = int(plan.get("audio_bandwidth") or 0)
    except (TypeError, ValueError):
        audio_bw = 0
    if video_bw:
        return (video_bw + audio_bw) * duration_seconds / 8 / (1024 * 1024)
    fallback_qn = 112 if quality_qn == 116 else quality_qn
    base = estimate_size(fallback_qn, duration_seconds)
    if audio_bw:
        return base + audio_bw * duration_seconds / 8 / (1024 * 1024)
    return base


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
    """保留的通用请求接口（其它模块仍可复用）。"""
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


async def _view_once(session, url: str, params, headers: dict, cookies) -> dict:
    """单次视频信息请求，按网络/业务/风控分类。"""
    try:
        async with session.get(
            url, params=params, headers=headers, cookies=cookies or None
        ) as resp:
            if resp.status == 412:
                raise BiliRiskControlError(f"视频信息接口 HTTP 412: {url}")
            if resp.status >= 400:
                raise BiliNetworkError(f"视频信息接口 HTTP {resp.status}: {url}")
            try:
                data = await resp.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError) as e:
                raise BiliBusinessError(-1, "视频信息接口返回非 JSON") from e
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise BiliNetworkError(f"视频信息接口网络失败: {type(e).__name__}") from e
    if not isinstance(data, dict):
        raise BiliBusinessError(-1, "视频信息接口响应格式异常")
    if data.get("code") == -412:
        raise BiliRiskControlError("视频信息接口 code=-412")
    return data


async def fetch_video_info(
    *, bvid: str = "", aid: str = "", cookies: dict | None = None
) -> dict:
    """视频信息接口：普通 view 触发 412/-412 时，有限一次 WBI 签名回退。"""
    params = {"bvid": bvid} if bvid else {"aid": aid}
    api_url = API_BY_BVID.format(bvid) if bvid else API_BY_AID.format(aid)
    headers = dict(DEFAULT_HEADERS)
    headers["Accept"] = "application/json, text/plain, */*"
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            try:
                return await _view_once(session, api_url, None, headers, cookies)
            except BiliRiskControlError as first:
                logger.warning(f"视频信息接口触发风控，尝试 WBI 签名回退: {first}")
                try:
                    signed = await wbi_signed_params(params)
                    return await _view_once(
                        session, VIEW_WBI_API, signed, headers, cookies
                    )
                except (BiliRiskControlError, BiliNetworkError, BiliBusinessError) as e:
                    logger.warning(f"视频信息接口 WBI 回退失败: {e}")
                    return {"code": -412, "message": f"WBI 回退失败: {e}"}
    except BiliNetworkError as e:
        return {"code": -400, "message": str(e)}
    except BiliBusinessError as e:
        return {"code": -400, "message": str(e)}


# ---------------------------------------------------------------------------
# Cookie 持久化、校验与登录
# ---------------------------------------------------------------------------

_CREDENTIAL_REVISION = 0
_COOKIE_LOCK = LoopLocalLock()
_VALIDATION_CACHE = {
    "fingerprint": "",
    "revision": -1,
    "result": None,
    "at": 0.0,
}


def _cookie_fingerprint(cookies: dict) -> str:
    payload = json.dumps(cookies, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _reset_validation_cache() -> None:
    _VALIDATION_CACHE.update(
        {"fingerprint": "", "revision": -1, "result": None, "at": 0.0}
    )


async def load_cookies() -> dict | None:
    if not COOKIE_FILE or not os.path.exists(COOKIE_FILE):
        return None
    try:
        async with aiofiles.open(COOKIE_FILE, "r", encoding="utf-8") as f:
            content = await f.read()
        if not content.strip():
            return None
        return json.loads(content)
    except json.JSONDecodeError:
        logger.error("Cookie 文件格式错误")
        return None
    except Exception as e:
        logger.error(f"加载 Cookie 失败: {e}")
        return None


async def save_cookies_dict(cookies: dict) -> bool:
    """原子落盘 + 尽量限制文件权限；成功后提升凭据版本并清掉旧校验缓存。"""
    global _CREDENTIAL_REVISION
    if not COOKIE_FILE:
        logger.error("保存 Cookie 失败: 未初始化 Cookie 路径")
        return False
    temp_path = ""
    try:
        async with _COOKIE_LOCK.get():
            parent = os.path.dirname(COOKIE_FILE) or "."
            os.makedirs(parent, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(
                prefix="bili_cookies.", suffix=".tmp", dir=parent
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            if os.name == "posix":
                try:
                    os.chmod(temp_path, 0o600)
                except OSError:
                    pass
            os.replace(temp_path, COOKIE_FILE)
            temp_path = ""
            _CREDENTIAL_REVISION += 1
            _reset_validation_cache()
        logger.info(f"Cookie 已保存到: {COOKIE_FILE}")
        return True
    except Exception as e:
        logger.error(f"保存 Cookie 失败: {e}")
        return False
    finally:
        if temp_path:
            remove_quietly(temp_path)


async def _validate_cookie_remote(cookies: dict) -> Optional[bool]:
    """远程校验；临时网络失败/不确定业务码返回 None，不判定凭据失效。"""
    url = "https://api.bilibili.com/x/member/web/account"
    headers = dict(COOKIE_CHECK_HEADERS)
    headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    try:
        async with aiohttp.ClientSession() as session:
            timeout = aiohttp.ClientTimeout(total=10)
            async with session.get(url, headers=headers, timeout=timeout) as resp:
                if resp.status >= 500:
                    return None
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("code") == 0:
        api_mid = str((data.get("data") or {}).get("mid", ""))
        return api_mid == str(cookies.get("DedeUserID", ""))
    if data.get("code") == -101:
        return False
    return None


async def check_cookie_valid_detailed(force: bool = False) -> Optional[bool]:
    """返回 True/False/None(临时网络失败)；缓存绑定凭据指纹与版本。"""
    global COOKIE_VALID
    cookies = await load_cookies()
    if not cookies:
        COOKIE_VALID = False
        return False

    required_fields = {
        "SESSDATA": lambda v: len(v) > 30 and "," in v,
        "bili_jct": lambda v: len(v) == 32,
        "DedeUserID": lambda v: v.isdigit(),
    }
    for field, validator in required_fields.items():
        if field not in cookies or not validator(str(cookies[field])):
            COOKIE_VALID = False
            return False

    fingerprint = _cookie_fingerprint(cookies)
    async with _COOKIE_LOCK.get():
        revision = _CREDENTIAL_REVISION
        cached = dict(_VALIDATION_CACHE)
    if not force and cached["result"] is not None:
        ttl = 300 if cached["result"] else 60
        if (
            cached["fingerprint"] == fingerprint
            and cached["revision"] == revision
            and time.monotonic() - cached["at"] < ttl
        ):
            COOKIE_VALID = cached["result"]
            return cached["result"]

    result = await _validate_cookie_remote(cookies)
    async with _COOKIE_LOCK.get():
        # 校验期间发生了新登录：旧结果不得污染新凭据的缓存
        if revision != _CREDENTIAL_REVISION:
            current = dict(_VALIDATION_CACHE)
            logger.debug("Cookie 校验期间凭据已更新，丢弃旧校验结果")
            if (
                current["result"] is not None
                and current["revision"] == _CREDENTIAL_REVISION
            ):
                COOKIE_VALID = current["result"]
                return current["result"]
            return None
        if result is not None:
            _VALIDATION_CACHE.update(
                {
                    "fingerprint": fingerprint,
                    "revision": revision,
                    "result": result,
                    "at": time.monotonic(),
                }
            )
            COOKIE_VALID = result
        elif cached["fingerprint"] == fingerprint and cached["result"] is not None:
            # 临时网络失败：沿用同指纹的既有结论，不写入「确定失效」
            COOKIE_VALID = cached["result"]
            return cached["result"]
    if result is None:
        logger.warning("Cookie 有效性校验遇临时网络失败，未能确定凭据状态")
    return result


async def check_cookie_valid(force: bool = False) -> bool:
    result = await check_cookie_valid_detailed(force=force)
    return bool(result)


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
    return {
        "qrcode_key": qr_data["qrcode_key"],
        "image_base64": base64.b64encode(buffered.getvalue()).decode(),
        "image_bytes": buffered.getvalue(),
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
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return {"code": -1, "message": "检查登录状态失败"}


def _collect_login_cookies(session: aiohttp.ClientSession, redirect_url: str) -> dict:
    """从轮询跳转 url 参数和 session cookie jar 两处合并登录 Cookie。"""
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


class LoginOutcome(NamedTuple):
    cookies: Optional[dict]
    saved: bool
    reason: str


_LOGIN_TIMEOUT_SECONDS = 150
_LOGIN_TASKS: set = set()
_ACTIVE_LOGIN_TASK = None
_LOGIN_GENERATION = 0
_LOGIN_LOCK = LoopLocalLock()


async def check_login_status_loop(
    session: aiohttp.ClientSession,
    qrcode_key: str,
    generation: int = 0,
    timeout_seconds: int = _LOGIN_TIMEOUT_SECONDS,
) -> LoginOutcome:
    """总截止时间控制登录超时；只有最新一代流程允许写入凭据。"""
    deadline = time.monotonic() + max(1, timeout_seconds)
    logger.info(f"等待登录...（总截止时间 {timeout_seconds} 秒）")
    last_code = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning("登录超时（总截止时间），请重试")
            return LoginOutcome(None, False, "timeout")
        try:
            status = await asyncio.wait_for(
                check_login_status(session, qrcode_key),
                timeout=min(5.0, remaining),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            status = {"code": -1}
        if status.get("code") != 0:
            await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
            continue
        data = status.get("data", {}) or {}
        poll_code = data.get("code")
        if poll_code == 0:
            try:
                cookies = _collect_login_cookies(session, data.get("url", ""))
                if not cookies.get("SESSDATA") or not cookies.get("DedeUserID"):
                    found = ",".join(sorted(cookies)) or "无"
                    raise ValueError(f"未取到 SESSDATA/DedeUserID，已获取字段: {found}")
            except Exception as e:
                logger.error(f"登录异常: {e}")
                return LoginOutcome(None, False, str(e))
            if generation != _LOGIN_GENERATION:
                logger.warning("旧登录流程已被新流程取代，不覆盖新凭据")
                return LoginOutcome(None, False, "superseded")
            saved = await save_cookies_dict(cookies)
            if not saved:
                logger.error("B站登录成功但 Cookie 保存失败")
                return LoginOutcome(cookies, False, "cookie_save_failed")
            logger.info("B站登录成功，Cookie 已保存")
            return LoginOutcome(cookies, True, "ok")
        if poll_code in (86038, 86105):
            logger.warning("二维码已过期，请重新获取")
            return LoginOutcome(None, False, "expired")
        if poll_code != last_code:
            msg = {86090: "已扫码，等待手机确认", 86101: "等待扫码"}.get(
                poll_code, f"轮询返回未知状态码: {poll_code}"
            )
            logger.debug(msg)
        last_code = poll_code
        await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


async def bili_login() -> tuple:
    """生成二维码并启动唯一的登录任务；旧流程先取消并回收。"""
    global _ACTIVE_LOGIN_TASK, _LOGIN_GENERATION
    async with _LOGIN_LOCK.get():
        previous = _ACTIVE_LOGIN_TASK
        _LOGIN_GENERATION += 1
        generation = _LOGIN_GENERATION
        _ACTIVE_LOGIN_TASK = None
    if previous is not None and not previous.done():
        previous.cancel()
        await asyncio.gather(previous, return_exceptions=True)

    logger.info("正在生成 B站 登录二维码...")
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
    try:
        qr_data = await generate_qrcode(session)
    except Exception as e:
        await session.close()
        logger.error(f"生成二维码异常: {e}")
        return None, None
    if not qr_data:
        await session.close()
        return None, None

    qrcode_key = qr_data["qrcode_key"]

    async def _run() -> LoginOutcome:
        try:
            return await check_login_status_loop(session, qrcode_key, generation)
        finally:
            await session.close()

    task = asyncio.create_task(_run())
    _ACTIVE_LOGIN_TASK = task
    _LOGIN_TASKS.add(task)
    task.add_done_callback(_LOGIN_TASKS.discard)
    return task, qr_data


async def cancel_bili_login_tasks() -> None:
    """插件卸载时取消并回收所有登录任务。"""
    global _ACTIVE_LOGIN_TASK
    tasks = [t for t in list(_LOGIN_TASKS) if not t.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _ACTIVE_LOGIN_TASK = None
