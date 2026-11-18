"""下载层共用工具：字节预算、大小解析、临时文件与取消安全收尾。"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from typing import Any, Awaitable, Iterable, Optional

# 未配置业务限制时的防资源耗尽兜底上限。
DEFAULT_VIDEO_MAX_BYTES = 2 * 1024 * 1024 * 1024


class DownloadLimitExceeded(RuntimeError):
    """下载过程超过字节预算，应触发现有降档流程而非普通网络重试。"""

    def __init__(self, message: str, *, observed_bytes: Optional[int] = None):
        super().__init__(message)
        self.observed_bytes = observed_bytes


class IncompleteDownloadError(RuntimeError):
    """已知长度与实际写入不一致（可重试）。"""


class SizePrecheckExceeded(RuntimeError):
    """下载前已确认该档位大小超限，可提前跳过并降级。"""

    def __init__(self, message: str, *, observed_bytes: Optional[int] = None):
        super().__init__(message)
        self.observed_bytes = observed_bytes


class LoopLocalLock:
    """按事件循环惰性创建的锁（asyncio.Lock 绑定创建时的循环）。"""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock: Optional[asyncio.Lock] = None

    def get(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._lock is None or self._loop is not loop:
            self._loop = loop
            self._lock = asyncio.Lock()
        return self._lock


class ByteBudget:
    """DASH 音视频两路共享的原子字节预算。"""

    def __init__(self, limit: int):
        self.limit = max(1, int(limit))
        self.used = 0
        self._lock = LoopLocalLock()

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    async def consume(self, amount: int) -> None:
        amount = max(0, int(amount))
        async with self._lock.get():
            new_total = self.used + amount
            if new_total > self.limit:
                raise DownloadLimitExceeded(
                    "下载内容超过字节预算"
                    f"（{new_total / 1048576:.1f}MB > {self.limit / 1048576:.1f}MB）",
                    observed_bytes=new_total,
                )
            self.used = new_total

    async def release(self, amount: int) -> None:
        amount = max(0, int(amount))
        async with self._lock.get():
            self.used = max(0, self.used - amount)


class BudgetTicket:
    """单次流下载占用的预算；失败或文件被删除时释放。"""

    def __init__(self, budget: Optional[ByteBudget]):
        self.budget = budget
        self.reserved = 0

    async def consume(self, amount: int) -> None:
        if self.budget is None:
            return
        await self.budget.consume(amount)
        self.reserved += max(0, int(amount))

    async def rollback(self) -> None:
        """本次尝试失败，释放其已写入字节。"""
        if self.budget is None or self.reserved <= 0:
            return
        await self.budget.release(self.reserved)
        self.reserved = 0

    async def discard(self) -> None:
        """文件被删除时释放全部占用。"""
        await self.rollback()


def resolve_budget_limit(max_size_mb: Optional[float]) -> int:
    """把 MB 配置转换为字节硬限制；管理员豁免时仍保留安全上限。"""
    if max_size_mb is None:
        return DEFAULT_VIDEO_MAX_BYTES
    try:
        configured = float(max_size_mb)
    except (TypeError, ValueError):
        return DEFAULT_VIDEO_MAX_BYTES
    if configured <= 0 or configured == float("inf"):
        return DEFAULT_VIDEO_MAX_BYTES
    return min(int(configured * 1024 * 1024), DEFAULT_VIDEO_MAX_BYTES)


def unique_temp_path(path: str) -> str:
    """生成同一目录下的唯一临时文件路径。"""
    return f"{path}.{uuid.uuid4().hex}.part"


def replace_file(source: str, destination: str) -> None:
    """成功校验后原子替换目标文件。"""
    parent = os.path.dirname(destination)
    if parent:
        os.makedirs(parent, exist_ok=True)
    os.replace(source, destination)
    try:
        os.utime(destination, None)
    except OSError:
        pass


def remove_quietly(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


_CONTENT_RANGE_RE = re.compile(r"\s*bytes\s+(\d+)-(\d+)/(\d+)\s*", re.IGNORECASE)


def parse_content_range(value: Optional[str]):
    """严格解析 Content-Range: bytes start-end/total。"""
    match = _CONTENT_RANGE_RE.fullmatch(value or "")
    if not match:
        return None
    start, end, total = (int(part) for part in match.groups())
    if start > end or end >= total:
        return None
    return start, end, total


def extract_size_from_headers(headers, status: Optional[int] = None) -> Optional[int]:
    """提取完整文件大小；206 需 Content-Range 才可信，其余取 Content-Length。"""
    try:
        content_range = headers.get("Content-Range") or headers.get("content-range")
    except AttributeError:
        return None
    parsed = parse_content_range(content_range)
    if parsed:
        return parsed[2]
    if content_range:
        return None
    if status == 206:
        return None
    try:
        content_length = headers.get("Content-Length") or headers.get("content-length")
    except AttributeError:
        return None
    if content_length is None:
        return None
    try:
        size = int(content_length)
    except (TypeError, ValueError):
        return None
    return size if size > 0 else None


_RETRYABLE_EXC_NAMES = {
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "ReadError",
    "WriteError",
    "ConnectError",
    "RemoteProtocolError",
    "ServerDisconnectedError",
    "ClientConnectionError",
    "ClientConnectorError",
    "ClientOSError",
    "ClientPayloadError",
    "ServerTimeoutError",
    "TimeoutError",
    "IncompleteReadError",
}

_RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}


def is_retryable_download_error(exc: BaseException) -> bool:
    """仅网络/限流错误与截断文件可重试；取消与超限不可重试。"""
    if isinstance(
        exc, (asyncio.CancelledError, DownloadLimitExceeded, SizePrecheckExceeded)
    ):
        return False
    if isinstance(exc, IncompleteDownloadError):
        return True
    if type(exc).__name__ in _RETRYABLE_EXC_NAMES:
        return True
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status, int) and status in _RETRYABLE_STATUSES:
        return True
    return False


async def gather_cancel_on_error(*awaitables: Awaitable[Any]) -> list:
    """并发等待；任一失败/取消时取消并回收其余任务，再原样抛出。"""
    tasks = [asyncio.ensure_future(a) for a in awaitables]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
        raise


async def shield_cleanup(awaitable: Awaitable[Any]) -> None:
    """取消时仍完成清理；不吞掉原始 CancelledError。"""
    task = asyncio.ensure_future(awaitable)
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            continue
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        return


def iter_component_files(content: Iterable[Any]) -> list[str]:
    """递归提取消息组件引用的本地文件路径（供清理保护使用）。"""
    from urllib.parse import unquote, urlparse

    found: list[str] = []

    def local_path(candidate: Any) -> Optional[str]:
        if not isinstance(candidate, str) or not candidate:
            return None
        if candidate.startswith("file://"):
            parsed = urlparse(candidate)
            candidate = unquote(parsed.path)
            if (
                os.name == "nt"
                and len(candidate) > 2
                and candidate.startswith("/")
                and candidate[2] == ":"
            ):
                candidate = candidate[1:]
        if candidate.startswith(("http://", "https://", "base64://", "data:")):
            return None
        return os.path.abspath(candidate)

    def visit(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, (list, tuple)):
            for item in node:
                visit(item)
            return
        for attribute in ("path", "file", "file_"):
            path = local_path(getattr(node, attribute, None))
            if path:
                found.append(path)
                break
        nodes = getattr(node, "nodes", None)
        if nodes:
            visit(nodes)
        chain = getattr(node, "content", None)
        if isinstance(chain, (list, tuple)):
            visit(chain)

    visit(list(content))
    return found
