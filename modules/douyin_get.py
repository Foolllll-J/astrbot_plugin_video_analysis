from dataclasses import dataclass
from typing import Union, List, Dict, Any, Awaitable, Callable

import httpx
import aiofiles
import os
import json
import hashlib
from astrbot.api import logger
from .douyin_scraper.douyin_parser import DouyinParser
from .douyin_scraper.cookie_extractor import (
    extract_and_format_cookies,
    extract_douyin_cookies,
)

DOUYIN_COOKIE_FILE = None

class ParseError(Exception):
    """自定义解析错误"""
    pass

@dataclass
class Video:
    url: str
    thumb_url: str | None = None
    width: int = 0
    height: int = 0
    duration: int = 0
    bit_rate: list = None  # 用于降级

    def __post_init__(self):
        if self.bit_rate is None:
            self.bit_rate = []

@dataclass
class Image:
    url: str

class DYType:
    VIDEO = "video"
    IMAGE = "image"

@dataclass
class DYResult:
    type: str
    platform: str
    video: Video | None = None
    desc: str = ""
    author: str = "N/A"

    @staticmethod
    def parse(url: str, json_dict: Dict[str, Any]) -> 'DYResult':
        """解析外部 API 返回的 JSON 数据"""
        data = json_dict.get("data", {})
        desc = data.get("desc", "无标题")
        author = data.get("author", {}).get("nickname", "N/A")
        
        def v_p(video_data: Dict[str, Any]) -> Dict[str, Any]:
            bit_rate = video_data.get("bit_rate")
            if not bit_rate:
                raise ParseError("抖音解析失败: 未获取到视频下载地址 (API可能未返回所需数据)")

            bit_rate.sort(key=lambda x: x["quality_type"], reverse=True)
            best_bit_rate = bit_rate[0]

            video_url = best_bit_rate["play_addr"]["url_list"][0]
            thumb_url = video_data["cover"]["url_list"][-1]

            # 视频总时长（毫秒转秒）
            total_duration_ms = video_data.get("duration", 0) or 0
            total_duration = total_duration_ms / 1000 if total_duration_ms else 0

            return {
                "video_url": video_url,
                "thumb_url": thumb_url,
                "duration": total_duration,
                "width": best_bit_rate["play_addr"]["width"],
                "height": best_bit_rate["play_addr"]["height"],
                "bit_rate": bit_rate,
            }

        if video_data := data.get("video"):
            try:
                vpi = v_p(video_data)
                return DYResult(
                    type=DYType.VIDEO, desc=desc, author=author,
                    video=Video(
                        vpi["video_url"], thumb_url=vpi["thumb_url"],
                        width=vpi["width"], height=vpi["height"],
                        duration=vpi["duration"], bit_rate=vpi["bit_rate"]
                    ),
                    platform='douyin',
                )
            except Exception as e:
                logger.error(f"解析视频数据失败: {e}")
                # 返回一个标记，表示可能是图片类型
                raise ParseError(f"视频数据解析失败: {e}")
        
        if data.get("images") or data.get("image_post_info"):
            return DYResult(type=DYType.IMAGE, desc=desc, author=author, platform='douyin')

        raise Exception("无法解析内容类型或视频数据缺失")


def init_douyin_login(data_dir: str) -> None:
    global DOUYIN_COOKIE_FILE
    DOUYIN_COOKIE_FILE = os.path.join(data_dir, "douyin_cookies.json")


async def load_douyin_cookies() -> str | None:
    if not DOUYIN_COOKIE_FILE or not os.path.exists(DOUYIN_COOKIE_FILE):
        return None

    try:
        async with aiofiles.open(DOUYIN_COOKIE_FILE, "r", encoding="utf-8") as f:
            content = await f.read()
        if not content.strip():
            return None

        cookies = json.loads(content)
        if not isinstance(cookies, dict) or not cookies:
            return None

        cookie_str = "; ".join(
            f"{key}={value}" for key, value in cookies.items() if key and value
        )
        if not cookie_str:
            return None

        return extract_and_format_cookies(cookie_str)
    except json.JSONDecodeError:
        logger.error("抖音 Cookie 文件格式错误")
        return None
    except Exception as exc:
        logger.error(f"加载抖音 Cookie 失败: {exc}")
        return None


async def get_effective_douyin_cookie(
    *,
    cookie_loaded: bool,
    cookie_from_config: str,
    cookie_from_file: str,
    loader: Callable[[], Awaitable[str | None]] = load_douyin_cookies,
) -> tuple[str, bool, str]:
    resolved_file_cookie = cookie_from_file
    resolved_loaded = cookie_loaded

    if not resolved_loaded:
        resolved_file_cookie = await loader() or ""
        resolved_loaded = True

    effective_cookie = cookie_from_config or resolved_file_cookie or ""
    return effective_cookie, resolved_loaded, resolved_file_cookie


def format_douyin_failure_message(result: dict | None) -> str:
    user_message = "抱歉，抖音解析失败。"

    if not result:
        logger.error("Douyin 解析失败: empty result")
        return user_message

    failures = result.get("failure_info") or []
    error_message = result.get("error") or "unknown error"

    if failures:
        logger.error(
            f"Douyin 解析失败详情: error={error_message}, failures={json.dumps(failures, ensure_ascii=False)}"
        )
    else:
        logger.error(f"Douyin 解析失败详情: error={error_message}")

    return user_message


async def check_douyin_cookie_valid(cookie_string: str | None = None) -> bool:
    cookie_string = cookie_string or await load_douyin_cookies()
    if not cookie_string:
        return False

    _, is_valid, _ = extract_douyin_cookies(cookie_string)
    if not is_valid:
        logger.debug("抖音 Cookie 缺少关键字段")
        return False

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/133.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.douyin.com/",
        "Cookie": cookie_string,
    }

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.get(
                "https://www.douyin.com/aweme/v1/web/query/user/",
                headers=headers,
            )

            if response.status_code == 200:
                try:
                    data = response.json()
                except Exception:
                    data = {}

                if data.get("status_code") == 0 or "data" in data:
                    return True

            homepage = await client.get("https://www.douyin.com/", headers=headers)
            return homepage.status_code == 200 and "sessionid" in cookie_string
    except Exception as exc:
        logger.debug(f"抖音 Cookie 校验异常: {type(exc).__name__}: {exc}")
        return False

def _make_failure(stage: str, reason: str, message: str, details: str = "") -> dict:
    return {
        "stage": stage,
        "reason": reason,
        "message": message,
        "details": details,
    }


def _build_final_failure(failures: list[dict]) -> dict:
    return {
        "error": "抖音解析失败",
        "type": "error",
        "failure_info": failures,
    }


def _classify_local_parse_error(error_message: str, details: str = "") -> dict:
    error_text = f"{error_message} {details}".strip()

    if "Empty response" in error_text or "Invalid JSON" in error_text:
        return _make_failure(
            "local",
            "cookie_expired",
            "本地解析请求被拦截，Cookie 可能已过期。",
            details or error_message,
        )
    if "Failed to extract aweme_id" in error_text:
        return _make_failure(
            "local",
            "aweme_id_extract_failed",
            "无法从抖音分享内容中提取作品 ID，链接可能已失效。",
            details or error_message,
        )

    return _make_failure(
        "local",
        "local_parse_failed",
        "本地解析失败。",
        details or error_message,
    )


async def fetch_douyin_metadata(url: str, api_url: str = None, cookie: str = None) -> dict | None:
    """
    仅获取抖音视频的元数据（标题、时长、类型），不下载文件。
    同时返回预取的数据供 process_douyin_video 复用，避免重复解析。

    Args:
        url: 抖音分享链接
        api_url: 外部解析服务地址
        cookie: 抖音 Cookie

    Returns:
        dict: 包含 title, duration, type, source, prefetched_data 字段，或 None
    """
    # 1. 优先尝试本地解析
    if cookie:
        try:
            parser = DouyinParser(cookie)
            data = await parser.parse(url)

            if data and "error" not in data:
                title = data.get("desc", "抖音作品")
                duration = data.get("duration", 0) or 0

                # 判断类型
                media_urls = data.get("media_urls", [])
                video_count = sum(1 for m in media_urls if m.get("type") == "video")
                image_count = len(media_urls) - video_count

                media_type = "video"
                if video_count > 0 and image_count > 0:
                    media_type = "multi_video"
                elif image_count > 1:
                    media_type = "image"
                elif not media_urls:
                    media_type = "unknown"

                logger.debug(f"FetchMetadata 本地解析成功: title={title}, duration={duration}s, type={media_type}")
                return {
                    "title": title,
                    "duration": duration,
                    "type": media_type,
                    "source": "local",
                    "prefetched_data": data,  # 返回预取数据供后续使用
                    "prefetch_source": "local",
                }
        except Exception as e:
            logger.error(f"FetchMetadata 本地解析失败: {e}")

    # 2. 尝试 API video_data 解析
    if api_url:
        logger.debug("FetchMetadata: 尝试 video_data API")
        try:
            api_endpoint = f"{api_url}/api/hybrid/video_data"
            async with httpx.AsyncClient(timeout=30, verify=False) as client:
                response = await client.get(
                    api_endpoint,
                    params={"url": url, "minimal": False},
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                        'Referer': 'https://www.douyin.com/'
                    }
                )

                if response.status_code == 200:
                    api_data = response.json()
                    if api_data.get("code") == 200 or api_data.get("status_code") == 0:
                        data = api_data.get("data", {})
                        title = data.get("desc", "抖音作品")

                        # 从 video 数据中获取时长
                        duration = 0
                        if video_data := data.get("video"):
                            total_duration_ms = video_data.get("duration", 0) or 0
                            duration = total_duration_ms / 1000 if total_duration_ms else 0

                        # 判断类型
                        if data.get("video"):
                            media_type = "video"
                        elif data.get("images") or data.get("image_post_info"):
                            media_type = "image"
                        else:
                            media_type = "unknown"

                        logger.debug(f"FetchMetadata API 解析成功: title={title}, duration={duration}s, type={media_type}")
                        return {
                            "title": title,
                            "duration": duration,
                            "type": media_type,
                            "source": "api",
                            "prefetched_data": api_data,  # 返回预取数据供后续使用
                            "prefetch_source": "api",
                        }
        except Exception as e:
            logger.error(f"FetchMetadata API 解析失败: {e}")

    return None


async def process_douyin_video(
    url: str,
    download_dir: str,
    api_url: str = None,
    cookie: str = None,
    max_images: int = 20,
    max_size: float = 200,
    smart_downgrade: bool = True,
    prefetched_data: dict = None,
    prefetch_source: str = None,
):
    """
    获取抖音视频/图片。
    逻辑：
    1. 如果有 prefetched_data，优先使用预取数据跳过重复解析。
    2. 如果有 cookie，尝试本地解析。
    3. 如果本地解析失败或没有 cookie，且有 api_url，则尝试 API 解析。
    4. 如果都失败或都未提供，返回 None。

    Args:
        url (str): 抖音分享链接。
        download_dir (str): 文件下载目录。
        api_url (str): 外部解析服务的地址。
        cookie (str): 抖音 Cookie。
        max_images (int): 图文作品解析上限。
        max_size (float): 最大视频大小（MB）。
        smart_downgrade (bool): 是否启用清晰度降级。
        prefetched_data (dict): fetch_douyin_metadata 预取的数据，可选。
        prefetch_source (str): 预取数据的来源 ("local" 或 "api")，可选。

    Returns:
        dict: 包含视频/图片信息和下载路径，或 None
    """

    failures = []

    # 0. 如果有预取数据，优先使用
    if prefetched_data and prefetch_source:
        if prefetch_source == "local":
            try:
                # 复用本地解析的结果数据
                result, failure = await _process_prefetched_local_data(
                    url, download_dir, prefetched_data, max_images, max_size
                )
                if result:
                    return result
                if failure:
                    failures.append(failure)
            except Exception as e:
                logger.error(f"Douyin 预取数据处理失败: {e}")
                failures.append(
                    _make_failure("local", "prefetch_exception", "预取数据处理发生异常。", str(e))
                )
        elif prefetch_source == "api":
            try:
                # 复用 API 解析的结果数据
                result, failure = await _try_video_data_api(
                    url, download_dir, api_url, max_size, smart_downgrade,
                    prefetched_data=prefetched_data
                )
                if result is not None:
                    return result
                if failure:
                    failures.append(failure)
            except Exception as e:
                logger.error(f"Douyin 预取 API 数据处理失败: {e}")
                failures.append(
                    _make_failure("api", "prefetch_exception", "预取数据处理发生异常。", str(e))
                )

    # 1. 优先尝试本地解析 (如果有 Cookie)
    if cookie:
        logger.debug("Douyin: 尝试本地解析 (使用 Cookie)")
        try:
            result, failure = await process_douyin_video_local(url, download_dir, cookie, max_images, max_size)
            if result:
                return result
            if failure:
                failures.append(failure)
        except Exception as e:
            logger.error(f"Douyin 本地解析失败: {e}")
            failures.append(
                _make_failure("local", "local_exception", "本地解析发生异常。", str(e))
            )

    # 2. 尝试 API 解析
    if api_url:
        logger.debug("Douyin: 尝试 API 解析")
        # 先尝试原有的 video_data API
        result, failure = await _try_video_data_api(url, download_dir, api_url, max_size, smart_downgrade)
        if result is not None:
            return result
        if failure:
            failures.append(failure)

        # 如果失败，尝试新的 download API
        logger.debug("Douyin: 尝试 download API")
        result, failure = await _try_download_api(url, download_dir, api_url, max_images)
        if result is not None:
            return result
        if failure:
            failures.append(failure)

    if failures:
        return _build_final_failure(failures)

    return _build_final_failure(
        [
            _make_failure(
                "config",
                "no_parser_available",
                "未配置可用的抖音解析方式。",
                "未提供 Cookie，且未配置 API 兜底地址。",
            )
        ]
    )


async def _process_prefetched_local_data(url: str, download_dir: str, data: dict, max_images: int = 20, max_size: float = 200):
    """
    处理预取的本地解析数据，直接复用已下载的元数据，跳过重复解析。

    Args:
        data: fetch_douyin_metadata 返回的 prefetched_data

    Returns:
        (result, failure) 元组
    """
    title = data.get("desc", "抖音作品")
    author = data.get("author_nickname", "N/A")
    aweme_id = data.get("aweme_id", hashlib.md5(url.encode()).hexdigest())
    duration = data.get("duration", 0) or 0

    os.makedirs(download_dir, exist_ok=True)

    media_items = []

    for i, item in enumerate(data.get("media_urls", [])):
        m_url = item["url"]
        m_type = item["type"]

        if m_type == "video":
            v_file = os.path.join(download_dir, f"{aweme_id}_{i}.mp4")
            if os.path.exists(v_file) or await _download_file(m_url, v_file):
                media_items.append({"path": v_file, "type": "video"})
        else:  # image
            if len([m for m in media_items if m["type"] == "image"]) >= max_images:
                logger.debug(f"Douyin 预取数据处理: 图片数量达到上限 {max_images}，跳过后续图片。")
                continue

            file_ext = ".jpg"
            if ".png" in m_url.lower():
                file_ext = ".png"
            elif ".webp" in m_url.lower():
                file_ext = ".webp"
            elif ".gif" in m_url.lower():
                file_ext = ".gif"

            img_file = os.path.join(download_dir, f"{aweme_id}_{i}{file_ext}")
            if os.path.exists(img_file) or await _download_file(m_url, img_file):
                media_items.append({"path": img_file, "type": "image"})

    if not media_items:
        return None, _make_failure(
            "local",
            "media_download_failed",
            "预取数据解析拿到了作品信息，但媒体下载失败。",
            f"aweme_id={aweme_id}",
        )

    # 如果只有一个媒体
    if len(media_items) == 1:
        item = media_items[0]
        if item["type"] == "video":
            return {
                "title": title, "author": author, "url": url,
                "video_path": item["path"], "type": "video",
                "duration": duration
            }, None
        else:
            return {
                "title": title, "author": author, "url": url,
                "image_paths": [item["path"]], "type": "image",
                "duration": duration
            }, None

    # 多个媒体，保持原始顺序
    return {
        "title": title, "author": author, "url": url,
        "ordered_media": media_items,
        "type": "multi_video" if any(i["type"] == "video" for i in media_items) else "images",
        "duration": duration
    }, None


async def process_douyin_video_local(url: str, download_dir: str, cookie: str, max_images: int = 20, max_size: float = 200):
    """使用本地解析逻辑（不支持清晰度智能降级，因为本地解析无法获取清晰度选项信息）"""
    parser = DouyinParser(cookie)
    data = await parser.parse(url)

    if not data or "error" in data:
        error_message = data.get("error") if data else "Unknown"
        details = data.get("details", "") if data else ""
        logger.error(f"Douyin 本地解析返回错误: {error_message}")
        return None, _classify_local_parse_error(error_message, details)

    title = data.get("desc", "抖音作品")
    author = data.get("author_nickname", "N/A")
    aweme_id = data.get("aweme_id", hashlib.md5(url.encode()).hexdigest())
    duration = data.get("duration", 0) or 0

    logger.debug(f"Douyin 本地解析: title={title}, author={author}, duration={duration}s")

    os.makedirs(download_dir, exist_ok=True)

    media_items = []

    for i, item in enumerate(data.get("media_urls", [])):
        m_url = item["url"]
        m_type = item["type"]
        
        if m_type == "video":
            v_file = os.path.join(download_dir, f"{aweme_id}_{i}.mp4")
            if os.path.exists(v_file) or await _download_file(m_url, v_file):
                media_items.append({"path": v_file, "type": "video"})
        else: # image
            if len([m for m in media_items if m["type"] == "image"]) >= max_images:
                logger.debug(f"Douyin 本地解析: 图片数量达到上限 {max_images}，跳过后续图片。")
                continue

            file_ext = ".jpg"
            if ".png" in m_url.lower(): file_ext = ".png"
            elif ".webp" in m_url.lower(): file_ext = ".webp"
            elif ".gif" in m_url.lower(): file_ext = ".gif"
            
            img_file = os.path.join(download_dir, f"{aweme_id}_{i}{file_ext}")
            if os.path.exists(img_file) or await _download_file(m_url, img_file):
                media_items.append({"path": img_file, "type": "image"})

    if not media_items:
        return None, _make_failure(
            "local",
            "media_download_failed",
            "本地解析拿到了作品信息，但媒体下载失败。",
            f"aweme_id={aweme_id}",
        )
        
    # 如果只有一个媒体
    if len(media_items) == 1:
        item = media_items[0]
        if item["type"] == "video":
            return {
                "title": title, "author": author, "url": url,
                "video_path": item["path"], "type": "video",
                "duration": duration
            }, None
        else:
            return {
                "title": title, "author": author, "url": url,
                "image_paths": [item["path"]], "type": "image",
                "duration": duration
            }, None

    # 多个媒体，保持原始顺序
    return {
        "title": title, "author": author, "url": url,
        "ordered_media": media_items,
        "type": "multi_video" if any(i["type"] == "video" for i in media_items) else "images",
        "duration": duration
    }, None

async def _download_file(url: str, save_path: str) -> bool:
    """通用下载函数"""
    try:
        async with httpx.AsyncClient(timeout=300, verify=False) as client: 
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36', 'Referer': 'https://www.douyin.com/'}
            async with client.stream('GET', url, headers=headers, follow_redirects=True) as response: 
                response.raise_for_status()
                async with aiofiles.open(save_path, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        await f.write(chunk)
        return True
    except Exception as e:
        logger.error(f"文件下载失败: {url}, 错误: {e}")
        if os.path.exists(save_path):
            os.remove(save_path)
        return False

async def _try_video_data_api(
    url: str,
    download_dir: str,
    api_url: str,
    max_size: float = 200,
    smart_downgrade: bool = True,
    prefetched_data: dict = None,
):
    """尝试使用 video_data API 解析（支持清晰度降级）"""

    # 如果有预取数据，复用它
    if prefetched_data:
        logger.debug("Douyin API: 使用预取数据跳过网络请求")
        try:
            result = DYResult.parse(url, prefetched_data)
        except Exception as e:
            logger.error(f"Douyin 预取数据解析失败: {e}")
            return None, _make_failure("api", "parse_error", "预取数据解析失败", str(e))

        if result.type != DYType.VIDEO:
            return {
                "title": result.desc,
                "author": result.author,
                "url": url,
                "video_path": None,
                "duration": result.video.duration if result.video else 0,
                "type": result.type,
            }, None

        if not result.video:
            return None, _make_failure(
                "api_video_data", "missing_video",
                "video_data API 未返回可下载的视频地址。"
            )

        video_url = result.video.url
        video_duration = result.video.duration
        video_bit_rate = result.video.bit_rate
    else:
        # 1. API 解析
        api_endpoint = f"{api_url}/api/hybrid/video_data"

        try:
            async with httpx.AsyncClient(timeout=30, verify=False) as client:
                params = {"url": url, "minimal": False}
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Referer': 'https://www.douyin.com/'
                }
                response = await client.get(api_endpoint, params=params, headers=headers)

                if response.status_code != 200:
                    return None, _make_failure(
                        "api_video_data", "http_error",
                        f"video_data API 返回 HTTP {response.status_code}。",
                        response.text[:300],
                    )

                api_data = response.json()
                if api_data.get("code") != 200 and api_data.get("status_code") != 0:
                    return None, _make_failure(
                        "api_video_data", "business_error",
                        "video_data API 返回业务错误。",
                        api_data.get("msg") or "",
                    )

                try:
                    result = DYResult.parse(url, api_data)
                except Exception as e:
                    return None, _make_failure(
                        "api_video_data", "parse_error",
                        "video_data API 返回的数据无法解析。", str(e),
                    )

                if result.type != DYType.VIDEO:
                    return {
                        "title": result.desc, "author": result.author, "url": url,
                        "video_path": None,
                        "duration": result.video.duration if result.video else 0,
                        "type": result.type,
                    }, None

                if not result.video:
                    return None, _make_failure(
                        "api_video_data", "missing_video",
                        "video_data API 未返回可下载的视频地址。"
                    )

                video_url = result.video.url
                video_duration = result.video.duration
                video_bit_rate = result.video.bit_rate

        except httpx.ReadTimeout:
            return None, _make_failure("api_video_data", "timeout", "video_data API 请求超时。")
        except Exception as e:
            return None, _make_failure(
                "api_video_data", "network_error",
                "video_data API 请求失败。", str(e)
            )

    # 3. 文件命名和缓存检查 (使用 URL 的 MD5 哈希作为唯一 ID)
    url_bytes = url.encode('utf-8')
    simple_id = hashlib.md5(url_bytes).hexdigest()

    os.makedirs(download_dir, exist_ok=True)
    final_file = os.path.join(download_dir, f"{simple_id}.mp4")

    # 降级顺序: 1 (1080P) -> 10 (720P) -> 20 (540P)
    DOUYIN_DOWNGRADE_ORDER = [1, 10, 20]

    def select_quality_url(bit_rate_list: list, target_quality: int) -> tuple[str, int] | None:
        """从 bit_rate 列表中选择目标清晰度，返回 (url, quality_type)"""
        for br in bit_rate_list:
            if br.get("quality_type") == target_quality:
                url_list = br.get("play_addr", {}).get("url_list")
                if url_list:
                    return url_list[0], target_quality
        return None

    def get_next_quality(current_idx: int) -> int | None:
        """获取下一个要尝试的清晰度"""
        for q in DOUYIN_DOWNGRADE_ORDER[current_idx + 1:]:
            if q not in attempted_qualities:
                return q
        return None

    # 检查缓存
    if os.path.exists(final_file):
        return {
            "title": result.desc, "author": result.author, "url": url,
            "video_path": final_file, "duration": video_duration
        }, None

    # 下载并可能降级
    current_quality = DOUYIN_DOWNGRADE_ORDER[0]  # 从最高开始
    attempted_qualities = set()
    last_error = None

    while True:
        if current_quality in attempted_qualities:
            logger.warning("已尝试所有可用清晰度，仍超出大小限制。")
            break
        attempted_qualities.add(current_quality)

        # 选择清晰度
        quality_info = select_quality_url(video_bit_rate, current_quality)
        if not quality_info:
            logger.debug(f"清晰度 {current_quality} 不可用，跳过")
            next_q = get_next_quality(DOUYIN_DOWNGRADE_ORDER.index(current_quality))
            if next_q is None:
                break
            current_quality = next_q
            continue

        download_url, used_quality = quality_info
        logger.debug(f"尝试下载抖音视频 (清晰度: {used_quality})")

        try:
            async with httpx.AsyncClient(timeout=300, verify=False) as client:
                download_headers = {'User-Agent': 'Mozilla/5.0'}
                async with client.stream('GET', download_url, headers=download_headers) as response:
                    response.raise_for_status()

                    async with aiofiles.open(final_file, "wb") as f:
                        async for chunk in response.aiter_bytes():
                            await f.write(chunk)

            # 检查文件大小
            file_size_mb = os.path.getsize(final_file) / (1024 * 1024)
            logger.debug(f"抖音视频下载完成 (清晰度 {used_quality})，文件大小: {file_size_mb:.2f}MB，最大限制: {max_size}MB")

            # 检查是否需要降级
            if file_size_mb > max_size and smart_downgrade:
                current_idx = DOUYIN_DOWNGRADE_ORDER.index(current_quality)
                next_q = get_next_quality(current_idx)
                if next_q:
                    logger.debug(f"视频大小 {file_size_mb:.2f}MB 超出限制 {max_size}MB，降级到清晰度 {next_q}")
                    os.remove(final_file)
                    current_quality = next_q
                    continue
                else:
                    logger.warning("已尝试所有可用清晰度，仍超出大小限制。")
                    break
            else:
                # 下载成功
                return {
                    "title": result.desc,
                    "author": result.author,
                    "url": url,
                    "video_path": final_file,
                    "duration": video_duration,
                    "view_count": 0, "like_count": 0, "danmaku_count": 0, "coin_count": 0, "favorite_count": 0
                }, None

        except Exception as e:
            last_error = e
            logger.error(f"抖音视频下载失败 (清晰度 {used_quality}): {e}")
            # 尝试降级
            current_idx = DOUYIN_DOWNGRADE_ORDER.index(current_quality)
            next_q = get_next_quality(current_idx)
            if next_q:
                current_quality = next_q
                if os.path.exists(final_file):
                    os.remove(final_file)
                continue
            else:
                break

    # 所有清晰度都失败
    if os.path.exists(final_file):
        os.remove(final_file)
    return None, _make_failure(
        "api_video_data",
        "download_failed",
        "抖音视频下载失败。",
        str(last_error) if last_error else "未知错误",
    )

async def _try_download_api(url: str, download_dir: str, api_url: str, max_images: int = 20):
    """尝试使用 download API 下载"""
    logger.debug("Douyin: 开始使用 download API 解析")
    
    api_endpoint = f"{api_url}/api/download"
    
    try:
        async with httpx.AsyncClient(timeout=60, verify=False, follow_redirects=True) as client:
            params = {
                "url": url,
                "prefix": "true",
                "with_watermark": "false"
            }
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            }
            
            response = await client.get(api_endpoint, params=params, headers=headers)
            
            if response.status_code != 200:
                logger.error(f"Douyin download API HTTP 错误: {response.status_code}")
                logger.error(f"响应内容前500字符: {response.text[:500]}")
                return None, _make_failure(
                    "api_download",
                    "http_error",
                    f"download API 返回 HTTP {response.status_code}。",
                    response.text[:300],
                )
            
            # 检查 Content-Type 来判断是图片还是视频
            content_type = response.headers.get('content-type', '')
            
            # 如果是 JSON 响应，可能是错误信息
            if 'application/json' in content_type:
                try:
                    error_data = response.json()
                    logger.error(f"Douyin download API 返回 JSON 错误: {json.dumps(error_data, ensure_ascii=False)}")
                    return None, _make_failure(
                        "api_download",
                        "business_error",
                        "download API 返回错误信息。",
                        json.dumps(error_data, ensure_ascii=False)[:300],
                    )
                except:
                    pass
            
            # 生成唯一ID
            url_bytes = url.encode('utf-8')
            simple_id = hashlib.md5(url_bytes).hexdigest()
            os.makedirs(download_dir, exist_ok=True)
            
            # 判断文件类型
            if 'image' in content_type:
                logger.debug("Douyin download API: 检测到图片类型")
                
                # 保存文件
                file_ext = '.jpg' if 'jpeg' in content_type or 'jpg' in content_type else '.png' if 'png' in content_type else '.jpg'
                final_file = os.path.join(download_dir, f"{simple_id}{file_ext}")
                
                async with aiofiles.open(final_file, 'wb') as f:
                    await f.write(response.content)
                
                logger.debug("Douyin 单张图片下载完成")
                return {
                    "title": "抖音图片",
                    "author": "N/A",
                    "url": url,
                    "image_paths": [final_file],
                    "type": "image"
                }, None
                
            elif 'video' in content_type or 'octet-stream' in content_type:
                final_file = os.path.join(download_dir, f"{simple_id}.mp4")
                
                if os.path.exists(final_file):
                    return {
                        "title": "抖音视频",
                        "author": "N/A",
                        "url": url,
                        "video_path": final_file
                    }, None
                
                async with aiofiles.open(final_file, 'wb') as f:
                    await f.write(response.content)
                
                logger.debug("Douyin 视频下载完成")
                return {
                    "title": "抖音视频",
                    "author": "N/A",
                    "url": url,
                    "video_path": final_file
                }, None
            
            elif 'application/zip' in content_type or 'application/x-zip' in content_type:
                logger.debug("Douyin: 检测到图集（ZIP）")
                # 下载并解压 ZIP
                zip_file = os.path.join(download_dir, f"{simple_id}.zip")
                
                async with aiofiles.open(zip_file, 'wb') as f:
                    await f.write(response.content)
                
                # 解压 ZIP
                import zipfile
                extract_dir = os.path.join(download_dir, f"{simple_id}_images")
                os.makedirs(extract_dir, exist_ok=True)
                
                with zipfile.ZipFile(zip_file, 'r') as zip_ref:
                    zip_ref.extractall(extract_dir)
                
                # 获取所有图片文件
                image_files = []
                for root, dirs, files in os.walk(extract_dir):
                    for file in files:
                        if file.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                            if len(image_files) >= max_images:
                                logger.debug(f"Douyin API 解析: 图片数量达到上限 {max_images}，忽略其余提取的图片。")
                                break
                            image_files.append(os.path.join(root, file))
                    if len(image_files) >= max_images:
                        break
                
                logger.debug(f"从ZIP中提取了 {len(image_files)} 张图片")
                
                # 删除 ZIP 文件
                os.remove(zip_file)
                
                return {
                    "title": "抖音图集",
                    "author": "N/A",
                    "url": url,
                    "image_paths": sorted(image_files),
                    "type": "images"
                }, None
             
            else:
                logger.warning(f"Douyin download API: 未知的 Content-Type: {content_type}")
                logger.warning(f"响应内容前500字符: {response.text[:500]}")
                return None, _make_failure(
                    "api_download",
                    "unknown_content_type",
                    "download API 返回了未知内容类型。",
                    content_type or response.text[:300],
                )
                 
    except httpx.ReadTimeout:
        logger.error("Douyin download API 请求超时")
        return None, _make_failure(
            "api_download", "timeout", "download API 请求超时。"
        )
    except Exception as e:
        logger.error(f"Douyin download API 错误: {e}", exc_info=True)
        return None, _make_failure(
            "api_download", "network_error", "download API 请求失败。", str(e)
        )
