import asyncio
import time
from collections import deque
from typing import Deque, Dict, Optional, Set, Tuple


class ParseRateLimiter:
    """解析请求限频器：滑动窗口 + 冷却 + 可选并发拦截。"""

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
        self._inflight: Set[str] = set()
        self._lock = asyncio.Lock()

    async def acquire(self, key: Optional[str], platform: str) -> Tuple[bool, Optional[str]]:
        if not self.enable:
            return True, None
        if not key:
            # 私聊或缺少身份信息时不参与限频
            return True, None

        now = time.time()
        async with self._lock:
            cooldown_until = self._cooldown_until.get(key, 0.0)
            if cooldown_until > now:
                remain = int(cooldown_until - now + 0.999)
                self.logger.info(f"[限频] 已拦截{platform}解析请求：用户({key})处于冷却中，剩余 {remain}s")
                return False, None
            self._cooldown_until.pop(key, None)

            if self.block_parallel and key in self._inflight:
                self.logger.info(f"[限频] 已拦截{platform}解析请求：用户({key})存在进行中的解析任务")
                return False, None

            history = self._records.setdefault(key, deque())
            cutoff = now - self.window_sec
            while history and history[0] <= cutoff:
                history.popleft()

            if len(history) >= self.max_requests:
                self._cooldown_until[key] = now + self.cooldown_sec
                self.logger.info(
                    f"[限频] 已拦截{platform}解析请求：用户({key})在 {self.window_sec}s "
                    f"内超过 {self.max_requests} 次，进入冷却 {self.cooldown_sec}s"
                )
                return False, None

            history.append(now)
            if self.block_parallel:
                self._inflight.add(key)
            return True, key

    async def release(self, key: Optional[str]) -> None:
        if not key:
            return
        async with self._lock:
            self._inflight.discard(key)
