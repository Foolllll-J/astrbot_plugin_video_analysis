import asyncio
import binascii
import json
import os
import time
import uuid
from urllib.parse import urlencode

import httpx
from SignerPy import get, sign, trace_id

from astrbot.api import logger

from .base import BaseStrategy, StrategyParams
from ..model import DouyinParseResult, parse_aweme_detail
from ..utils.url import AwemeIdFetcher

MOBILE_USER_AGENT = (
    "com.ss.android.ugc.aweme/390500 (Linux; U; Android 13; zh_CN; Pixel 6; "
    "Build/TQ3A.230805.001; Cronet/TTNetVersion:6b6f6e6e 2024-04-10 "
    "QuicVersion:47946d2a 2024-03-28)"
)
PLAY_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
DETAIL_HOSTS = (
    "api.amemv.com",
    "api3-core-c.amemv.com",
    "aweme.snssdk.com",
    "api5-normal-lf.amemv.com",
    "api3-normal-c.amemv.com",
)
REGISTER_HOSTS = ("log.snssdk.com", "api.amemv.com")
SIGN_PROFILES = (
    {"license_id": 1611921764, "version": 8404},
    {"license_id": 1611921764, "version": 4404},
)
PLAY_RATIOS = ("default", "1080p", "720p", "540p", "480p")
MOBILE_DEVICE_POOL_SIZE = 3

REG_PARAMS = get(
    {
        "aid": "1128",
        "app_name": "aweme",
        "version_code": "390500",
        "version_name": "39.5.0",
        "device_platform": "android",
        "os": "android",
        "os_version": "13",
        "ssmix": "a",
        "language": "zh",
        "channel": "wandoujia_aweme",
        "device_type": "Pixel 6",
        "device_brand": "google",
        "resolution": "1080*2400",
        "dpi": "420",
        "host_abi": "arm64-v8a",
        "manifest_version_code": "390500",
        "update_version_code": "390500",
        "ac": "wifi",
        "app_type": "normal",
        "cpu_support64": "true",
    }
)

QUERY_PARAMS = get(
    {
        "aid": "1128",
        "app_name": "aweme",
        "version_code": "390500",
        "version_name": "39.5.0",
        "device_platform": "android",
        "os": "android",
        "os_version": "13",
        "ssmix": "a",
        "language": "zh",
        "channel": "wandoujia_aweme",
        "device_type": "Pixel 6",
        "device_brand": "google",
        "resolution": "1080*2400",
        "dpi": "420",
        "host_abi": "arm64-v8a",
        "manifest_version_code": "390500",
        "update_version_code": "390500",
        "ac": "wifi",
        "is_guest_mode": "0",
        "minor_status": "0",
        "app_type": "normal",
    }
)

_DEVICE_CACHE_PATH: str | None = None
_device_pool: list[dict] = []
_device_pool_index: int = 0


def set_device_cache_dir(data_dir: str) -> None:
    global _DEVICE_CACHE_PATH
    _DEVICE_CACHE_PATH = os.path.join(data_dir, "douyin_device.json")


def _load_cache() -> list[dict]:
    if not _DEVICE_CACHE_PATH or not os.path.exists(_DEVICE_CACHE_PATH):
        return []
    try:
        with open(_DEVICE_CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return [data]
    except Exception:
        pass
    return []


def _save_cache(devices: list[dict]) -> None:
    if not _DEVICE_CACHE_PATH:
        return
    try:
        os.makedirs(os.path.dirname(_DEVICE_CACHE_PATH), exist_ok=True)
        with open(_DEVICE_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(devices, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"保存设备缓存失败: {e}")


def _device_from_env() -> dict | None:
    device_id = os.getenv("DOUYIN_DEVICE_ID") or os.getenv("PARSEHUB_DOUYIN_DEVICE_ID")
    iid = (
        os.getenv("DOUYIN_IID")
        or os.getenv("PARSEHUB_DOUYIN_IID")
        or os.getenv("PARSEHUB_DOUYIN_INSTALL_ID")
    )
    if not device_id or not iid:
        return None
    return {
        "device_id": device_id.strip(),
        "iid": iid.strip(),
        "cdid": os.getenv("DOUYIN_CDID") or os.getenv("PARSEHUB_DOUYIN_CDID") or "",
        "openudid": os.getenv("DOUYIN_OPENUDID")
        or os.getenv("PARSEHUB_DOUYIN_OPENUDID")
        or "",
    }


class MobileApiStrategy(BaseStrategy):
    @property
    def name(self) -> str:
        return "mobile_api"

    async def execute(self, params: StrategyParams) -> DouyinParseResult:
        import re

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

        async with httpx.AsyncClient(timeout=20) as client:
            await self._ensure_device_pool(client)
            if not _device_pool:
                return DouyinParseResult(
                    success=False, error="移动端设备注册失败", source=self.name
                )

            device = self._next_device()

            detail = await self._fetch_detail(client, aweme_id, device)
            if not detail:
                return DouyinParseResult(
                    success=False, error="移动端 API 所有主机均失败", source=self.name
                )

            if detail.get("is_story") in (1, True, "1") or detail.get(
                "is_24_story"
            ) in (1, True, "1"):
                await self._attach_story_default_play(client, detail)

            return parse_aweme_detail(detail, aweme_id, self.name)

    async def _ensure_device_pool(self, client: httpx.AsyncClient) -> None:
        global _device_pool, _device_pool_index
        if _device_pool:
            return

        env_device = _device_from_env()
        if env_device:
            _device_pool = [env_device]
            _device_pool_index = 0
            _save_cache(_device_pool)
            logger.debug("移动端设备: 使用环境变量预设设备")
            return

        cached = _load_cache()
        if len(cached) >= MOBILE_DEVICE_POOL_SIZE:
            _device_pool = cached[:MOBILE_DEVICE_POOL_SIZE]
            _device_pool_index = 0
            logger.debug(f"移动端设备池: 从缓存加载 {len(_device_pool)} 台设备")
            return

        new_devices: list[dict] = []
        seen: set[tuple[str, str]] = set()
        errors = 0
        while (
            len(new_devices) < MOBILE_DEVICE_POOL_SIZE
            and errors < MOBILE_DEVICE_POOL_SIZE * 10
        ):
            device = await self._register_device(client)
            if not device:
                errors += 1
                await asyncio.sleep(0.2)
                continue
            key = (device["device_id"], device["iid"])
            if key in seen:
                errors += 1
                await asyncio.sleep(0.2)
                continue
            seen.add(key)
            new_devices.append(device)
            logger.debug(
                f"移动端设备注册: {device['device_id']} ({len(new_devices)}/{MOBILE_DEVICE_POOL_SIZE})"
            )

        if cached:
            new_devices = (cached + new_devices)[:MOBILE_DEVICE_POOL_SIZE]

        if not new_devices:
            return

        _device_pool = new_devices
        _device_pool_index = 0
        _save_cache(_device_pool)
        logger.debug(f"移动端设备池: {len(_device_pool)} 台设备已就绪")

    def _next_device(self) -> dict:
        global _device_pool_index
        device = _device_pool[_device_pool_index % len(_device_pool)]
        _device_pool_index = (_device_pool_index + 1) % len(_device_pool)
        return device

    async def _register_device(self, client: httpx.AsyncClient) -> dict | None:
        cdid = str(uuid.uuid4())
        openudid = binascii.hexlify(os.urandom(8)).decode()
        params = dict(REG_PARAMS)

        for host in REGISTER_HOSTS:
            query = urlencode(params)
            signed = sign(
                params=query,
                aid=1128,
                license_id=SIGN_PROFILES[0]["license_id"],
                version=SIGN_PROFILES[0]["version"],
                platform=0,
                sdk_version_str="v05.01.02-alpha.7-ov-android",
                sdk_version=83952160,
            )
            headers = {
                **signed,
                "User-Agent": MOBILE_USER_AGENT,
                "Content-Type": "application/json; charset=utf-8",
                "sdk-version": "2",
                "x-tt-trace-id": trace_id("0"),
            }
            url = f"https://{host}/service/2/device_register/?{query}"
            payload = {
                "magic_tag": "ss_app_log",
                "header": {
                    "display_name": "抖音",
                    "aid": 1128,
                    "channel": "wandoujia_aweme",
                    "package": "com.ss.android.ugc.aweme",
                    "app_version": "39.5.0",
                    "version_code": 390500,
                    "manifest_version_code": 390500,
                    "update_version_code": 390500,
                    "sdk_version": "3.9.5",
                    "sdk_target_version": 29,
                    "os": "Android",
                    "os_version": "13",
                    "os_api": 33,
                    "device_model": "Pixel 6",
                    "device_brand": "google",
                    "device_manufacturer": "Google",
                    "cpu_abi": "arm64-v8a",
                    "release_build": "TQ3A.230805.001",
                    "density_dpi": 420,
                    "display_density": "xhdpi",
                    "resolution": "1080x2400",
                    "language": "zh",
                    "timezone": 8,
                    "region": "CN",
                    "tz_name": "Asia/Shanghai",
                    "cdid": cdid,
                    "openudid": openudid,
                    "clientudid": str(uuid.uuid4()),
                    "google_aid": "",
                    "req_id": str(uuid.uuid4()),
                },
                "_gen_time": int(time.time()),
            }
            try:
                resp = await client.post(url, headers=headers, json=payload, timeout=20)
                body = resp.json()
                device_id = str(
                    body.get("device_id_str") or body.get("device_id") or ""
                )
                iid = str(
                    body.get("install_id_str")
                    or body.get("install_id")
                    or body.get("iid")
                    or ""
                )
                if device_id and iid and device_id != "0" and iid != "0":
                    return {
                        "device_id": device_id,
                        "iid": iid,
                        "cdid": cdid,
                        "openudid": openudid,
                    }
            except Exception:
                continue
        return None

    async def _fetch_detail(
        self, client: httpx.AsyncClient, aweme_id: str, device: dict
    ) -> dict | None:
        device_id = device["device_id"]
        iid = device["iid"]
        cdid = device.get("cdid", "")
        openudid = device.get("openudid", "")

        # 8 次重试，对齐 ParseHub
        for attempt in range(8):
            params = dict(QUERY_PARAMS)
            params["aweme_id"] = aweme_id
            params["device_id"] = device_id
            params["iid"] = iid
            if cdid:
                params["cdid"] = cdid
            if openudid:
                params["openudid"] = openudid
            query = urlencode(params)

            for profile in SIGN_PROFILES:
                signed = sign(
                    params=query,
                    aid=1128,
                    license_id=profile["license_id"],
                    version=profile["version"],
                    platform=0,
                    sdk_version_str="v05.01.02-alpha.7-ov-android",
                    sdk_version=83952160,
                )
                headers = {
                    **signed,
                    "User-Agent": MOBILE_USER_AGENT,
                    "x-tt-trace-id": trace_id(device_id),
                    "sdk-version": "2",
                    "passport-sdk-version": "203226",
                }

                for host in DETAIL_HOSTS:
                    url = f"https://{host}/aweme/v1/aweme/detail/?{query}"
                    try:
                        resp = await client.get(url, headers=headers)
                        if not resp.text:
                            continue
                        payload = resp.json()
                        detail = payload.get("aweme_detail")
                        if detail:
                            logger.debug(
                                f"移动端 API 成功: {host} v{profile['version']} (attempt {attempt + 1})"
                            )
                            return detail
                    except Exception:
                        continue
            await asyncio.sleep(0.15)
        return None

    async def _attach_story_default_play(
        self, client: httpx.AsyncClient, detail: dict
    ) -> None:
        """Story/日常 内容独立解析视频 URL，对齐 ParseHub"""
        video = detail.get("video") or {}
        video_uri = self._pick_video_uri(video)
        if not video_uri:
            return

        best = await self._resolve_best_play_url(client, video_uri)
        if not best:
            return

        play_addr = {
            "uri": video_uri,
            "url_list": [best["direct_url"]],
            "data_size": best["content_length"],
            "width": video.get("width") or 0,
            "height": video.get("height") or 0,
        }
        synthetic = {
            "bit_rate": best["bitrate_kbps"] or 0,
            "duration": video.get("duration") or 0,
            "play_addr": play_addr,
        }
        existing = video.get("bit_rate") or []
        video["bit_rate"] = [synthetic, *existing]

    @staticmethod
    def _pick_video_uri(video: dict) -> str:
        """从 play_addr.uri 或 bit_rate 中取最佳 video_uri"""
        uri = (video.get("play_addr", {})).get("uri")
        if uri:
            return str(uri)
        best_uri = ""
        best_bitrate = -1
        for item in video.get("bit_rate", []):
            candidate = (item.get("play_addr", {})).get("uri")
            bitrate = int(item.get("bit_rate", 0))
            if candidate and bitrate >= best_bitrate:
                best_uri = candidate
                best_bitrate = bitrate
        return best_uri

    async def _resolve_best_play_url(
        self, client: httpx.AsyncClient, video_uri: str
    ) -> dict | None:
        """用 HEAD 请求测试各清晰度，取 content-length 最大的"""
        play_headers = {
            "User-Agent": PLAY_USER_AGENT,
            "Referer": "https://www.douyin.com/",
        }
        best: dict | None = None
        for ratio in PLAY_RATIOS:
            api = f"https://aweme.snssdk.com/aweme/v1/play/?video_id={video_uri}&ratio={ratio}&line=0"
            try:
                resp = await client.head(
                    api, headers=play_headers, follow_redirects=True
                )
                content_length = int(resp.headers.get("content-length", 0))
                if best is None or content_length > best["content_length"]:
                    best = {
                        "ratio": ratio,
                        "direct_url": str(resp.url),
                        "content_length": content_length,
                        "bitrate_kbps": 0,
                    }
            except Exception:
                continue
        return best
