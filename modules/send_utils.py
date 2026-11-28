"""消息实际发送封装：await event.send(...) 感知真实发送结果，支持重试、合并转发与按预算打包。"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Iterable, Optional

from .auto_delete import get_file_use_guard
from .download_io import iter_component_files

# 单次请求体预算：aiocqhttp 会把图片 base64 内联进请求体，超过后协议端多半超时或拒绝
PACK_MAX_BYTES = 15 * 1024 * 1024
PACK_MAX_IMAGES = 9
PACK_MAX_CHARS = 1500
# base64 相对原始文件的膨胀系数
_BASE64_RATIO = 1.37
# 无本地文件（URL / 已内联）时的单图经验体积
_FALLBACK_IMAGE_BYTES = 512 * 1024
# 协议端明确拒绝类异常：换更小负载重发有意义；超时类相反，重发可能造成重复
_REJECTED_EXCEPTION_NAMES = {"ActionFailed", "HttpFailed"}


def _component_type(component: Any) -> str:
    ctype = getattr(component, "type", "")
    return ctype.lower() if isinstance(ctype, str) else ""


def _component_bytes(component: Any) -> int:
    """估算组件在请求体中的体积；视频走本地路径不内联，不计入。"""
    ctype = _component_type(component)
    if ctype == "plain":
        return len(str(getattr(component, "text", "") or "").encode("utf-8"))
    if ctype in ("image", "record"):
        size = 0
        for path in iter_component_files([component]):
            try:
                size += os.path.getsize(path)
            except OSError:
                continue
        return int(size * _BASE64_RATIO) if size else _FALLBACK_IMAGE_BYTES
    return 0


def pack_chains(items: Iterable[Iterable[Any]]) -> list[list[Any]]:
    """按单次请求预算把内容打包成若干条消息，保持原有顺序。

    视频独占一条；超长文本按字数切片；图片受体积与张数双重约束。
    """
    from astrbot.api.message_components import Plain

    chains: list[list[Any]] = []
    current: list[Any] = []
    used_bytes = 0
    used_images = 0
    used_chars = 0

    def flush() -> None:
        nonlocal current, used_bytes, used_images, used_chars
        if current:
            chains.append(current)
        current = []
        used_bytes = 0
        used_images = 0
        used_chars = 0

    for item in items:
        for component in item:
            ctype = _component_type(component)

            if ctype == "video":
                flush()
                chains.append([component])
                continue

            if ctype == "plain":
                text = str(getattr(component, "text", "") or "")
                if not text:
                    continue
                for start in range(0, len(text), PACK_MAX_CHARS):
                    piece = text[start : start + PACK_MAX_CHARS]
                    piece_bytes = len(piece.encode("utf-8"))
                    if current and (
                        used_chars + len(piece) > PACK_MAX_CHARS
                        or used_bytes + piece_bytes > PACK_MAX_BYTES
                    ):
                        flush()
                    current.append(Plain(piece))
                    used_bytes += piece_bytes
                    used_chars += len(piece)
                continue

            size = _component_bytes(component)
            images = 1 if ctype in ("image", "record") else 0
            if current and (
                used_bytes + size > PACK_MAX_BYTES
                or used_images + images > PACK_MAX_IMAGES
            ):
                flush()
            current.append(component)
            used_bytes += size
            used_images += images

    flush()
    return chains


def classify_send_error(exc: Optional[BaseException]) -> str:
    """区分协议端明确拒绝与网络类失败，决定是否值得换更小负载重发。"""
    if exc is None:
        return "ok"
    if type(exc).__name__ in _REJECTED_EXCEPTION_NAMES:
        return "rejected"
    if getattr(exc, "retcode", None):
        return "rejected"
    return "transient"


async def send_chain_once(event, content: list[Any]) -> Optional[Exception]:
    """实际发送一次，返回异常或 None；CancelledError 原样向上传播。"""
    guard = get_file_use_guard()
    with guard.protect(iter_component_files(content)):
        try:
            await event.send(event.chain_result(content))
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 适配器异常由调用方决定重试/报告
            return exc


async def send_chain_with_retry(
    event,
    content: list[Any],
    *,
    attempts: int = 3,
    delay: float = 2.0,
    label: str = "消息",
    logger_obj=None,
) -> bool:
    """带有限重试的实际发送；仅在实际成功后记录成功日志。"""
    for attempt in range(max(1, attempts)):
        error = await send_chain_once(event, content)
        if error is None:
            if logger_obj:
                logger_obj.info(f"{label}发送成功 (总尝试次数: {attempt + 1})。")
            return True
        if attempt + 1 < attempts:
            if logger_obj:
                logger_obj.warning(
                    f"{label}发送失败 (第 {attempt + 1} 次)，"
                    f"等待 {delay:g} 秒后重试... 错误: {error}"
                )
            await asyncio.sleep(delay)
        elif logger_obj:
            logger_obj.error(
                f"{label}发送最终失败 ({attempts} 次重试)。错误: {error}",
                exc_info=True,
            )
    return False


async def send_merge_forward(
    event,
    nodes: list[Any],
    *,
    attempts: int = 2,
    delay: float = 1.0,
    label: str = "合并转发",
    logger_obj=None,
) -> str:
    """发送合并转发，返回 'ok' / 'rejected' / 'transient'。

    被协议端明确拒绝时不再重试（重试只会同样被拒），交由调用方换负载；
    网络类失败按次数重试，最终仍失败则判失败，不再改发，避免重复。
    """
    try:
        from astrbot.api.message_components import Nodes
    except Exception as exc:
        if logger_obj:
            logger_obj.warning(f"{label}不可用: {exc}")
        return "rejected"

    content = [Nodes(nodes=nodes)]
    for attempt in range(max(1, attempts)):
        error = await send_chain_once(event, content)
        if error is None:
            if logger_obj:
                logger_obj.info(f"{label}发送成功 (总尝试次数: {attempt + 1})。")
            return "ok"

        outcome = classify_send_error(error)
        if outcome == "rejected":
            if logger_obj:
                logger_obj.warning(f"{label}被协议端拒绝（不重试）: {error}")
            return "rejected"
        if attempt + 1 < attempts:
            if logger_obj:
                logger_obj.warning(
                    f"{label}发送失败 (第 {attempt + 1} 次)，"
                    f"等待 {delay:g} 秒后重试... 错误: {error}"
                )
            await asyncio.sleep(delay)
        elif logger_obj:
            logger_obj.error(f"{label}发送最终失败 ({attempts} 次)。错误: {error}")

    return "transient"


async def send_packed(
    event,
    items: list[list[Any]],
    *,
    attempts: int = 2,
    delay: float = 1.0,
    label: str = "内容",
    logger_obj=None,
) -> tuple[int, int]:
    """按预算打包后逐条发送，失败只影响该条。

    Returns:
        (成功条数, 总条数)
    """
    chains = pack_chains(items)
    if not chains:
        return 0, 0

    succeeded = 0
    for index, chain in enumerate(chains, 1):
        ok = await send_chain_with_retry(
            event,
            chain,
            attempts=attempts,
            delay=delay,
            label=f"{label} 第 {index}/{len(chains)} 条",
            logger_obj=logger_obj,
        )
        if ok:
            succeeded += 1
    return succeeded, len(chains)


async def send_nodes_with_fallback(
    event,
    nodes: list[Any],
    *,
    attempts: int = 2,
    delay: float = 1.0,
    label: str = "内容",
    logger_obj=None,
) -> str:
    """合并转发优先，失败或被拒时按预算打包重发，返回 'ok' / 'partial' / 'failed'。"""
    items = [list(getattr(node, "content", None) or []) for node in nodes]
    items = [item for item in items if item]
    if not items:
        if logger_obj:
            logger_obj.warning(f"{label}：没有可发送的内容")
        return "failed"

    try:
        platform = event.get_platform_name()
    except Exception:
        platform = ""

    if platform == "aiocqhttp" and len(items) > 1:
        outcome = await send_merge_forward(
            event,
            nodes,
            attempts=2,
            delay=delay,
            label=f"{label}合并转发",
            logger_obj=logger_obj,
        )
        if outcome == "ok":
            return "ok"
        # 被拒或超时都换更小负载重发：宁可多一条重复消息，也不让整篇内容丢掉
        if logger_obj:
            reason = "被协议端拒绝" if outcome == "rejected" else "无响应或超时"
            logger_obj.warning(f"{label}：合并转发{reason}，改为按预算打包发送")

    succeeded, total = await send_packed(
        event,
        items,
        attempts=attempts,
        delay=delay,
        label=label,
        logger_obj=logger_obj,
    )
    return await finish_delivery_report(
        event,
        succeeded=succeeded,
        total=total,
        label=label,
        logger_obj=logger_obj,
    )


async def send_plain(event, text: str, **kwargs) -> bool:
    """best-effort 文本提示；失败不抛出。"""
    try:
        from astrbot.api.message_components import Plain
    except Exception:
        return False
    return await send_chain_with_retry(event, [Plain(text)], **kwargs)


async def finish_delivery_report(
    event,
    *,
    succeeded: int,
    total: int,
    label: str = "内容",
    logger_obj=None,
    send_notice: bool = True,
) -> str:
    """全失败与部分失败分别记录/报告，返回 'ok' / 'partial' / 'failed'。"""
    if succeeded >= total:
        return "ok"
    if succeeded <= 0:
        if logger_obj:
            logger_obj.warning(f"{label}全部发送失败（共 {total} 条）")
        if send_notice:
            await send_plain(
                event,
                f"抱歉，{label}全部发送失败，请稍后重试。",
                attempts=1,
                delay=0,
                label=f"{label}全失败提示",
                logger_obj=logger_obj,
            )
        return "failed"
    if logger_obj:
        logger_obj.warning(
            f"{label}部分发送失败：成功 {succeeded}/{total} 条，其余内容已发送。"
        )
    if send_notice:
        await send_plain(
            event,
            f"{label}有 {total - succeeded} 条发送失败，其余内容已发送。",
            attempts=1,
            delay=0,
            label=f"{label}部分失败提示",
            logger_obj=logger_obj,
        )
    return "partial"
