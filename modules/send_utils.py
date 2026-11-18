"""消息实际发送封装：await event.send(...) 感知真实发送结果，支持重试与部分失败处理。"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from .auto_delete import get_file_use_guard
from .download_io import iter_component_files


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


async def send_plain(event, text: str, **kwargs) -> bool:
    """best-effort 文本提示；失败不抛出。"""
    try:
        from astrbot.api.message_components import Plain
    except Exception:
        return False
    return await send_chain_with_retry(event, [Plain(text)], **kwargs)


async def deliver_items(
    event,
    items: list[list[Any]],
    *,
    attempts: int = 3,
    delay: float = 2.0,
    label: str = "媒体",
    logger_obj=None,
) -> tuple[int, list[int]]:
    """按顺序发送多项；部分失败时只重试失败项。

    Returns:
        (成功项数, 最终失败的原始下标列表)
    """
    succeeded = 0
    failed_indexes: list[int] = []
    for idx, content in enumerate(items):
        ok = await send_chain_with_retry(
            event,
            content,
            attempts=attempts,
            delay=delay,
            label=f"{label} 第 {idx + 1} 项",
            logger_obj=logger_obj,
        )
        if ok:
            succeeded += 1
        else:
            failed_indexes.append(idx)

    if failed_indexes:
        still_failed: list[int] = []
        for idx in failed_indexes:
            ok = await send_chain_with_retry(
                event,
                items[idx],
                attempts=max(1, attempts - 1),
                delay=delay,
                label=f"{label} 第 {idx + 1} 项(失败项重试)",
                logger_obj=logger_obj,
            )
            if ok:
                succeeded += 1
            else:
                still_failed.append(idx)
        failed_indexes = still_failed
    return succeeded, failed_indexes


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
            logger_obj.warning(f"{label}全部发送失败（共 {total} 项）")
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
            f"{label}部分发送失败：成功 {succeeded}/{total} 项，其余内容已发送。"
        )
    if send_notice:
        await send_plain(
            event,
            f"{label}有 {total - succeeded} 项发送失败，其余内容已发送。",
            attempts=1,
            delay=0,
            label=f"{label}部分失败提示",
            logger_obj=logger_obj,
        )
    return "partial"
