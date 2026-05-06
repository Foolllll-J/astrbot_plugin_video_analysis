import asyncio
import time
from collections import deque
from typing import Deque, Dict, Iterable, MutableSet, Optional, Tuple

from astrbot.api.event import AstrMessageEvent


def is_qq_platform(event: AstrMessageEvent) -> bool:
    """当前是否为 QQ(aiocqhttp) 平台。"""
    try:
        platform_name = event.get_platform_name()
        if platform_name:
            return str(platform_name) == "aiocqhttp"
    except Exception:
        pass

    umo = getattr(event, "unified_msg_origin", "") or ""
    if ":" in umo:
        return umo.split(":", 1)[0] == "aiocqhttp"
    return False


def contains_blocked_keyword_in_title(
    title: str,
    blocked_keywords: Iterable[str],
    logger_obj=None,
) -> bool:
    """检查视频标题是否命中屏蔽关键词。"""
    normalized_keywords = [str(keyword).strip() for keyword in blocked_keywords if str(keyword).strip()]
    if not normalized_keywords or not title:
        return False

    for keyword in normalized_keywords:
        if keyword in title:
            if logger_obj:
                logger_obj.debug(f"视频标题命中解析屏蔽关键词，已跳过下载：关键词={keyword}，标题={title}")
            return True
    return False


async def check_group_level_requirement(
    event: AstrMessageEvent,
    min_group_level: int,
    logger_obj,
    unsupported_logged_platforms: Optional[MutableSet[str]] = None,
) -> bool:
    """检查群等级限制，仅在 QQ(aiocqhttp) 群聊中生效。"""
    threshold = max(0, int(min_group_level))
    if threshold <= 0:
        return True
    if not event.get_group_id():
        return True

    if not is_qq_platform(event):
        marker = "non_qq"
        if unsupported_logged_platforms is None or marker not in unsupported_logged_platforms:
            if unsupported_logged_platforms is not None:
                unsupported_logged_platforms.add(marker)
            logger_obj.debug("当前平台不支持群等级检测，已自动跳过群等级限制。")
        return True

    try:
        group_id = int(event.get_group_id())
        user_id = int(event.get_sender_id())
        info = await event.bot.api.call_action(
            "get_group_member_info",
            group_id=group_id,
            user_id=user_id,
            no_cache=True,
        )
        level = int(info.get("level", 0))
        role = str(info.get("role", "member"))
        if role in {"owner", "admin"}:
            return True
        if level < threshold:
            logger_obj.debug(
                f"群成员等级不足，已跳过视频解析：群号={group_id}，用户={user_id}，当前等级={level}，要求等级={threshold}"
            )
            return False
        return True
    except Exception as e:
        logger_obj.warning(f"获取群成员等级失败，默认放行视频解析: {e}")
        return True


class ParseGuard:
    """解析请求限制器：滑动窗口 + 冷却 + 可选并发拦截。"""

    _PARALLEL_STALE_SEC = 10 * 60

    def __init__(
        self,
        enable: bool,
        window_sec: int,
        max_requests: int,
        cooldown_sec: int,
        block_parallel: bool,
        logger_obj,
    ):
        self.enable = bool(enable)
        self.window_sec = max(1, int(window_sec))
        self.max_requests = max(1, int(max_requests))
        self.cooldown_sec = max(1, int(cooldown_sec))
        self.block_parallel = bool(block_parallel)
        self.logger = logger_obj

        self._records: Dict[str, Deque[float]] = {}
        self._cooldown_until: Dict[str, float] = {}
        self._inflight: Dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, key: Optional[str], platform: str) -> Tuple[bool, Optional[str]]:
        if not self.enable:
            return True, None
        if not key:
            return True, None

        now = time.time()
        async with self._lock:
            cooldown_until = self._cooldown_until.get(key, 0.0)
            if cooldown_until > now:
                remain = int(cooldown_until - now + 0.999)
                self.logger.info(f"[解析限制] 已拦截{platform}解析请求：用户({key})处于冷却中，剩余 {remain}s")
                return False, None
            self._cooldown_until.pop(key, None)

            if self.block_parallel and key in self._inflight:
                inflight_since = float(self._inflight.get(key, now))
                inflight_age = now - inflight_since
                if inflight_age >= self._PARALLEL_STALE_SEC:
                    self._inflight.pop(key, None)
                    self.logger.warning(
                        f"[解析限制] 检测到{platform}解析状态超时，已重置并放行："
                        f"用户({key})上次进行中状态已持续 {int(inflight_age)}s"
                    )
                else:
                    self.logger.info(f"[解析限制] 已拦截{platform}解析请求：用户({key})存在进行中的解析任务")
                    return False, None

            history = self._records.setdefault(key, deque())
            cutoff = now - self.window_sec
            while history and history[0] <= cutoff:
                history.popleft()

            if len(history) >= self.max_requests:
                self._cooldown_until[key] = now + self.cooldown_sec
                self.logger.info(
                    f"[解析限制] 已拦截{platform}解析请求：用户({key})在 {self.window_sec}s "
                    f"内超过 {self.max_requests} 次，进入冷却 {self.cooldown_sec}s"
                )
                return False, None

            history.append(now)
            if self.block_parallel:
                self._inflight[key] = now
            return True, key

    async def release(self, key: Optional[str]) -> None:
        if not key:
            return
        async with self._lock:
            self._inflight.pop(key, None)
