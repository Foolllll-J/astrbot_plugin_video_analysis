"""跨平台分享链接解析结果本地缓存。

重复解析同一作品时，命中缓存可直接发送本地已落地的媒体文件，
避免再次调用源站 API 解析与重复下载。
"""

import asyncio
import os
import re
import time
from collections.abc import Awaitable, Callable

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .bilibili.constants import REG_AV, REG_B23, REG_BV
from .bilibili.parser import av2bv
from .douyin.parser import send_douyin_with_title_forward

# 平台 → (文本发送者名, 媒体发送者名)
SENDER_NAMES = {
    "douyin": ("抖音文案", "抖音内容"),
    "xhs": ("小红书正文", "小红书内容"),
}

SetEmojiFn = Callable[[int, bool], Awaitable[None]]


def visible_len(text: str) -> int:
    """去掉话题标签后，统计纯可见字符长度（中英文、数字）。"""
    text = re.sub(r"#[^#\s]+(?:\[[^\]]*\])?#?\s*", "", text)
    return len(re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]", "", text))


def build_xhs_meta_text(title: str, desc: str, has_title: bool) -> str:
    """组装小红书长文转发文本：「标题」+ 空行 + 正文，无标题时退化为正文/标题。"""
    meta_text = f"「{title}」" if has_title and title else ""
    if desc:
        meta_text = meta_text + "\n\u200b\n" + desc if meta_text else desc
    return meta_text or title or ""


async def forward_long_text(
    event: AstrMessageEvent,
    text: str,
    result: dict,
    set_emoji_fn: SetEmojiFn | None = None,
    text_sender_name: str = "抖音文案",
    media_sender_name: str = "抖音内容",
):
    """文本超长时，将文本与媒体组合为合并转发发送（非 aiocqhttp 平台自动降级）。"""
    async for response in send_douyin_with_title_forward(
        event,
        text,
        result,
        set_emoji_fn=set_emoji_fn,
        text_sender_name=text_sender_name,
        media_sender_name=media_sender_name,
    ):
        yield response


_DOUYIN_ID_RE = re.compile(
    r"modal_id=(\d+)|/(?:video|note|slides)/(\d+)|/share/(?:video|note|slides)/(\d+)"
)
_XHS_ID_RE = re.compile(r"/(?:explore|discovery/item)/([0-9a-zA-Z]+)")

# 短链域名：需要一次轻量重定向解析才能拿到内容 ID
_SHORT_HOSTS = {
    "bilibili": ("b23.tv", "bili2233.cn"),
    "douyin": ("v.douyin.com", "iesdouyin.com"),
    "xhs": ("xhslink.com", "xhslink.cn"),
}

_SHORT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/144.0.0.0 Safari/537.36"
)


def extract_content_id(url: str, platform: str) -> str | None:
    """从 URL 直接提取内容 ID（不含短链解析）。"""
    if not url:
        return None
    if platform == "bilibili":
        if REG_B23.search(url):
            return None  # 短链需先解析
        m = REG_BV.search(url)
        if m:
            return m.group()
        m = REG_AV.search(url)
        if m:
            return av2bv(m.group())
        return None
    if platform == "douyin":
        m = _DOUYIN_ID_RE.search(url)
        if m:
            return next((g for g in m.groups() if g), None)
        return None
    if platform == "xhs":
        m = _XHS_ID_RE.search(url)
        return m.group(1) if m else None
    return None


async def resolve_short_link(url: str, platform: str) -> str | None:
    """短链重定向解析，返回最终 URL；失败返回 None。"""
    hosts = _SHORT_HOSTS.get(platform) or ()
    if not any(h in (url or "").lower() for h in hosts):
        return None
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=8, verify=False
        ) as client:
            resp = await client.get(url, headers={"User-Agent": _SHORT_UA})
            return str(resp.url)
    except Exception as e:
        logger.debug(f"短链解析失败 ({platform}): {e}")
        return None


async def get_content_id(url: str, platform: str) -> str | None:
    """统一入口：内嵌 ID 优先，短链则解析一次后提取。"""
    content_id = extract_content_id(url, platform)
    if content_id:
        return content_id
    final_url = await resolve_short_link(url, platform)
    if final_url:
        return extract_content_id(final_url, platform)
    return None


class MediaCache:
    """基于 KV 的跨平台媒体元数据缓存。

    kv 需提供 get_kv_data / put_kv_data / delete_kv_data 三个异步方法
    （AstrBot Star 插件类直接传 self 即可，需 >= v4.9.2）。
    全部缓存存于单个 KV key 下的嵌套字典，便于整体扫描清理。
    """

    _KV_KEY = "video_analysis"

    def __init__(self, kv):
        self._kv = kv
        self._lock = asyncio.Lock()

    async def _load(self) -> dict:
        try:
            data = await self._kv.get_kv_data(self._KV_KEY, None)
        except Exception as e:
            logger.debug(f"KV 读取缓存失败: {e}")
            return {}
        return data if isinstance(data, dict) else {}

    async def _save(self, data: dict) -> None:
        try:
            await self._kv.put_kv_data(self._KV_KEY, data)
        except Exception as e:
            logger.debug(f"KV 写入缓存失败: {e}")

    async def get(self, platform: str, content_id: str, ttl_sec: int) -> dict | None:
        """命中且全部文件存在返回条目；过期/文件缺失/异常均自愈并返回 None。"""
        async with self._lock:
            data = await self._load()
            platform_data = data.get(platform)
            if not isinstance(platform_data, dict):
                return None
            entry = platform_data.get(content_id)
            if not isinstance(entry, dict):
                return None
            saved_at = entry.get("saved_at") or 0
            if ttl_sec > 0 and saved_at and time.time() - saved_at > ttl_sec:
                platform_data.pop(content_id, None)
                if not platform_data:
                    data.pop(platform, None)
                await self._save(data)
                return None
            for f in entry.get("media_files") or []:
                path = f.get("path")
                if not path or not os.path.exists(path):
                    platform_data.pop(content_id, None)
                    if not platform_data:
                        data.pop(platform, None)
                    await self._save(data)
                    return None
            return entry

    async def put(self, platform: str, content_id: str, entry: dict) -> None:
        async with self._lock:
            data = await self._load()
            platform_data = data.get(platform)
            if not isinstance(platform_data, dict):
                platform_data = {}
                data[platform] = platform_data
            platform_data[content_id] = entry
            await self._save(data)

    async def remove(self, platform: str, content_id: str) -> None:
        async with self._lock:
            data = await self._load()
            platform_data = data.get(platform)
            if isinstance(platform_data, dict) and content_id in platform_data:
                platform_data.pop(content_id, None)
                if not platform_data:
                    data.pop(platform, None)
                await self._save(data)

    async def cleanup(self, ttl_sec: int) -> int:
        """删除过期或文件已缺失的条目，返回清理条数（与文件自动清理联动）。"""
        async with self._lock:
            data = await self._load()
            if not data:
                return 0
            now = time.time()
            removed = 0
            for platform, items in list(data.items()):
                if not isinstance(items, dict):
                    removed += 1
                    data.pop(platform, None)
                    continue
                for content_id, entry in list(items.items()):
                    if not isinstance(entry, dict):
                        removed += 1
                        items.pop(content_id)
                        continue
                    saved_at = entry.get("saved_at") or 0
                    expired = ttl_sec > 0 and saved_at and now - saved_at > ttl_sec
                    missing = any(
                        not (f.get("path") and os.path.exists(f["path"]))
                        for f in entry.get("media_files") or []
                    )
                    if expired or missing:
                        removed += 1
                        items.pop(content_id)
                if not items:
                    data.pop(platform, None)
            if removed:
                await self._save(data)
                logger.debug(f"缓存清理完成，共删除 {removed} 个过期/失效条目")
            return removed


def media_files_from_result(result: dict) -> list[dict]:
    """将下载器返回的 result 归一为有序的 media_files 列表。"""
    ordered = result.get("ordered_media")
    if isinstance(ordered, list) and ordered:
        return [
            {"path": str(m["path"]), "type": m.get("type", "image")}
            for m in ordered
            if m.get("path")
        ]
    video_path = result.get("video_path")
    if video_path:
        return [{"path": str(video_path), "type": "video"}]
    image_paths = result.get("image_paths")
    if isinstance(image_paths, list) and image_paths:
        return [{"path": str(p), "type": "image"} for p in image_paths]
    return []


def entry_to_result(entry: dict) -> dict:
    """将缓存条目还原为发送链路使用的 result dict。"""
    media_files = entry.get("media_files") or []
    result: dict = {
        "title": entry.get("title", ""),
        "type": entry.get("media_type", ""),
        "duration": entry.get("duration", 0),
    }
    if len(media_files) == 1 and media_files[0]["type"] == "video":
        result["video_path"] = media_files[0]["path"]
    elif media_files:
        result["ordered_media"] = [
            {"path": m["path"], "type": m["type"]} for m in media_files
        ]
        result["image_paths"] = [m["path"] for m in media_files if m["type"] == "image"]
        result["video_paths"] = [m["path"] for m in media_files if m["type"] == "video"]
    return result


async def lookup_cached_media(plugin, event, url: str, platform: str):
    """从本地媒体缓存定位已解析内容（与限流/并发拦截共用）。

    返回 (status, entry)，status 取值：
    - "hit"：缓存命中且全部文件存在，可直接发送 entry
    - "blocked"：缓存命中但被屏蔽词/时长限制拦截（表情已贴，视为已处理）
    - "miss"：无缓存 / 文件缺失 / 超限 / 异常，走正常解析
    """
    try:
        content_id = await get_content_id(url, platform)
        if not content_id:
            return "miss", None
        entry = await plugin.media_cache.get(
            platform, content_id, plugin.delete_time * 60
        )
        if not entry or not entry.get("media_files"):
            return "miss", None

        # 大小守卫：单视频超限则回退正常解析（保留智能降级能力）
        if not (
            plugin.admin_bypass_content_restrictions and plugin._is_admin_event(event)
        ):
            for f in entry["media_files"]:
                if (
                    f.get("type") == "video"
                    and os.path.exists(f["path"])
                    and os.path.getsize(f["path"]) / (1024 * 1024)
                    > plugin.max_video_size
                ):
                    logger.debug(f"缓存文件超限，回退正常解析: {f['path']}")
                    return "miss", None

        # 前置检查：屏蔽词 + 时长限制（拦截时已贴表情）
        is_video = entry.get("media_type") == "video"
        if await plugin._check_pre_conditions(
            event,
            entry.get("title", ""),
            entry.get("duration", 0),
            is_video=is_video,
        ):
            return "blocked", None
        logger.debug(f"缓存命中，直接发送: {platform}/{content_id}")
        return "hit", entry
    except Exception as e:
        logger.debug(f"缓存查询异常: {e}")
        return "miss", None


async def send_cached_result(plugin, event, entry: dict, platform: str):
    """按缓存条目直接发送本地媒体（与正常发送分支行为一致）。"""
    result = entry_to_result(entry)
    await plugin._set_emoji(event, 424)

    if plugin.text_forward_threshold > 0 and platform in ("douyin", "xhs"):
        if platform == "xhs":
            text = build_xhs_meta_text(
                result.get("title") or "",
                entry.get("desc") or "",
                entry.get("has_title", False),
            )
            text_sender, media_sender = SENDER_NAMES["xhs"]
        else:
            text = result.get("title") or ""
            text_sender, media_sender = SENDER_NAMES["douyin"]
        if visible_len(text) > plugin.text_forward_threshold:
            async for response in forward_long_text(
                event,
                text,
                result,
                set_emoji_fn=lambda emoji_id, set_val=True: plugin._set_emoji(
                    event, emoji_id, set_val
                ),
                text_sender_name=text_sender,
                media_sender_name=media_sender,
            ):
                yield response
            return

    if result.get("video_path") and os.path.exists(result["video_path"]):
        async for response in plugin._process_and_send(event, result, platform):
            yield response
        return

    sender_name = SENDER_NAMES.get(platform, ("抖音文案", "抖音内容"))[1]
    async for response in plugin._send_douyin_multimedia(event, result, sender_name):
        yield response
