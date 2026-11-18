import asyncio
import os
import re
import shutil
import time
from typing import Optional

import aiofiles
import httpx

from astrbot.api import logger

from .constants import PLAYURL_API, PLAYURL_WBI_API
from .utils import (
    load_cookies,
    get_buvid,
    wbi_signed_params,
    BiliNetworkError,
    BiliBusinessError,
    BiliRiskControlError,
)
from ..download_io import (
    BudgetTicket,
    ByteBudget,
    DownloadLimitExceeded,
    IncompleteDownloadError,
    SizePrecheckExceeded,
    extract_size_from_headers,
    is_retryable_download_error,
    remove_quietly,
    replace_file,
    resolve_budget_limit,
    shield_cleanup,
    unique_temp_path,
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
)

_DOWNLOAD_HEADERS = {
    "referer": "https://www.bilibili.com",
    "User-Agent": UA,
}
_DOWNLOAD_TIMEOUT = httpx.Timeout(300.0, connect=30.0)
_PROBE_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def _new_download_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=_DOWNLOAD_HEADERS,
        follow_redirects=True,
        timeout=_DOWNLOAD_TIMEOUT,
    )


def _safe_filename(text: str, max_len: int = 40) -> str:
    text = text.strip()
    text = re.sub(r'[\\/:*?"<>|]', "", text)
    text = re.sub(r"\s+", "_", text)
    text = text[:max_len] if len(text) > max_len else text
    return text.strip("_")


def _make_base_name(author: str, title: str, unique_id: str) -> str:
    parts = [
        p
        for p in [_safe_filename(author, 20), _safe_filename(title, 30), unique_id]
        if p
    ]
    return "_".join(parts)


# ---------------------------------------------------------------------------
# 播放地址接口（含 WBI 风控回退）
# ---------------------------------------------------------------------------


async def _playurl_once(
    client: httpx.AsyncClient,
    api: str,
    params: dict,
    cookies: dict,
) -> dict:
    try:
        resp = await client.get(api, params=params, cookies=cookies)
    except httpx.HTTPError as e:
        raise BiliNetworkError(f"播放地址接口网络失败: {type(e).__name__}") from e
    if resp.status_code == 412:
        raise BiliRiskControlError(f"播放地址接口 HTTP 412: {api}")
    if resp.status_code >= 400:
        raise BiliNetworkError(f"播放地址接口 HTTP {resp.status_code}: {api}")
    try:
        result = resp.json()
    except ValueError as e:
        raise BiliBusinessError(-1, "播放地址接口返回非 JSON") from e
    if not isinstance(result, dict):
        raise BiliBusinessError(-1, "播放地址接口响应格式异常")
    if result.get("code") == -412:
        raise BiliRiskControlError(f"播放地址接口 code=-412: {api}")
    if result.get("code") != 0:
        raise BiliBusinessError(result.get("code"), result.get("message", ""))
    data = result.get("data")
    if not isinstance(data, dict):
        raise BiliBusinessError(-1, "播放地址接口缺少 data")
    return data


async def _get_playurl(
    bvid: str,
    cid: int,
    qn: int,
    fnval: int,
    cookies: dict | None = None,
) -> dict:
    params = {
        "bvid": bvid,
        "cid": cid,
        "qn": qn,
        "fnver": 0,
        "fnval": fnval,
        "fourk": 1,
        "otype": "json",
        "platform": "pc",
        "high_quality": 1,
    }
    b_3, b_4 = await get_buvid()
    cookie_dict = {
        "SESSDATA": (cookies or {}).get("SESSDATA", ""),
        "bili_jct": (cookies or {}).get("bili_jct", ""),
    }
    if b_3:
        cookie_dict["buvid3"] = b_3
    if b_4:
        cookie_dict["buvid4"] = b_4
    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}

    async with httpx.AsyncClient(
        headers=headers, timeout=httpx.Timeout(20.0, connect=10.0)
    ) as client:
        try:
            return await _playurl_once(client, PLAYURL_API, params, cookie_dict)
        except BiliRiskControlError as first:
            # 只回退一次；网络错误与业务错误不触发签名回退。
            logger.warning(f"播放地址接口触发风控，尝试 WBI 签名回退: {first}")
            try:
                signed = await wbi_signed_params(params)
                return await _playurl_once(client, PLAYURL_WBI_API, signed, cookie_dict)
            except (
                BiliRiskControlError,
                BiliNetworkError,
                BiliBusinessError,
            ) as e:
                raise BiliRiskControlError(f"播放地址接口 WBI 回退失败: {e}") from first


def _collect_urls(base_url: str | None, backup_list: list[str] | None) -> list[str]:
    urls: list[str] = []
    for bu in backup_list or []:
        if bu:
            urls.append(str(bu))
    if base_url:
        urls.append(str(base_url))
    seen: set[str] = set()
    return [u for u in urls if not (u in seen or seen.add(u))]


# 同级档位映射：112(1080P+) 与 116(1080P60) 码率相近，视为同级互备
QN_PEERS = {112: 116, 116: 112}


def _same_tier(a: int, b: int) -> bool:
    """判断两个档位是否同级（112 与 116 互备）。"""
    return a == b or QN_PEERS.get(a) == b


def _best_qn(accept_quality: list[int], target_qn: int) -> int:
    """在真实可用档位中选取最接近目标档的档位。"""
    if target_qn in accept_quality:
        return target_qn
    peer = QN_PEERS.get(target_qn)
    if peer and peer in accept_quality:
        return peer
    candidates = [q for q in accept_quality if q <= target_qn]
    return max(candidates) if candidates else min(accept_quality)


def _has_avc(dash_videos: list[dict]) -> bool:
    return any(str(v.get("codecs", "")).startswith("avc1") for v in dash_videos)


def _pick_dash_stream(
    streams: list[dict], target_qn: int, prefer_avc: bool = True
) -> dict | None:
    if prefer_avc:
        avc = [s for s in streams if str(s.get("codecs", "")).startswith("avc1")]
        if avc:
            candidates = [s for s in avc if s["id"] <= target_qn]
            if candidates:
                return max(candidates, key=lambda s: s["id"])
            return max(avc, key=lambda s: s["id"])
    candidates = [s for s in streams if s["id"] <= target_qn]
    if candidates:
        return max(candidates, key=lambda s: s["id"])
    return max(streams, key=lambda s: s["id"]) if streams else None


def _pick_best_audio(audios: list[dict]) -> dict | None:
    return max(audios, key=lambda a: a.get("bandwidth", 0)) if audios else None


# ---------------------------------------------------------------------------
# 体积预检：可信大小 > HEAD > Range GET
# ---------------------------------------------------------------------------


def _trusted_stream_size(stream: dict) -> Optional[int]:
    """接口直接给出的可信字节大小（durl.size / data_size 等）。"""
    if not isinstance(stream, dict):
        return None
    for key in ("size", "length", "content_length", "data_size"):
        value = stream.get(key)
        try:
            size = int(value)
        except (TypeError, ValueError):
            continue
        if size > 0:
            return size
    return None


async def _probe_stream_size(
    client: httpx.AsyncClient, urls: list[str], label: str = ""
) -> Optional[int]:
    """探测单路流的完整大小；无法确定返回 None，不直接判超限。"""
    for url in urls[:2]:
        head_supported = False
        try:
            async with client.stream("HEAD", url, timeout=_PROBE_TIMEOUT) as head:
                if head.status_code < 400:
                    head_supported = True
                    size = extract_size_from_headers(head.headers, head.status_code)
                    if size is not None:
                        return size
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, ValueError):
            pass

        # HEAD 成功但没有大小，或 HEAD 不被支持：用一次流式 Range GET 探测。
        try:
            async with client.stream(
                "GET",
                url,
                headers={"Range": "bytes=0-0"},
                timeout=_PROBE_TIMEOUT,
            ) as ranged:
                if ranged.status_code == 206:
                    # 206 取 Content-Range 的完整大小，而非片段 Content-Length
                    size = extract_size_from_headers(ranged.headers, 206)
                    if size is not None:
                        return size
                elif ranged.status_code == 200:
                    # 服务端忽略 Range：不读取完整正文，大小保留未知
                    return None
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, ValueError):
            continue
        if head_supported:
            return None
    return None


async def _preflight_streams(
    client: httpx.AsyncClient,
    streams: list[dict],
    max_bytes: int,
    label: str,
) -> None:
    """下载前用可信/探测到的合计大小提前跳过明确超限的档位。"""
    sizes: list[Optional[int]] = []
    for stream in streams:
        size = _trusted_stream_size(stream)
        if size is None:
            size = await _probe_stream_size(client, stream.get("urls") or [], label)
        sizes.append(size)
    if not sizes or any(size is None for size in sizes):
        logger.debug(f"{label} 体积预检：大小未知，回退估算与下载过程限额。")
        return
    total = sum(int(size) for size in sizes)
    if total > max_bytes:
        raise SizePrecheckExceeded(
            f"{label} 下载前预检 {total / 1048576:.1f}MB 超过上限 "
            f"{max_bytes / 1048576:.1f}MB",
            observed_bytes=total,
        )
    logger.debug(f"{label} 体积预检通过：合计 {total / 1048576:.1f}MB。")


def _estimate_selected_bytes(streams: list[dict], duration: float) -> float:
    """按实际选中的视频/音频带宽估算体积（MB），仅用于日志与回退说明。"""
    if duration <= 0:
        return 0.0
    total_bw = sum(int(s.get("bandwidth") or 0) for s in streams)
    return total_bw * duration / 8 / (1024 * 1024)


# ---------------------------------------------------------------------------
# 流式下载：临时文件 + 原子替换 + 完整性检查 + 共享预算
# ---------------------------------------------------------------------------

_LOG_INTERVAL = 60


async def _try_download_urls(
    client: httpx.AsyncClient,
    urls: list[str],
    save_path: str,
    label: str = "",
    *,
    ticket: Optional[BudgetTicket] = None,
) -> str:
    os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
    last_exc: Exception | None = None
    for i, url in enumerate(urls):
        temp_path = unique_temp_path(save_path)
        written = 0
        try:
            logger.debug(f"{label} 尝试 CDN #{i + 1}/{len(urls)}")
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    raise httpx.HTTPStatusError(
                        f"HTTP {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                expected = None
                # 有 Content-Encoding 时 Content-Length 是压缩后长度，不做完整性比较
                if not resp.headers.get("content-encoding"):
                    expected = extract_size_from_headers(resp.headers, resp.status_code)
                start_time = time.monotonic()
                last_log = start_time
                async with aiofiles.open(temp_path, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        if not chunk:
                            continue
                        if ticket is not None:
                            await ticket.consume(len(chunk))
                        await f.write(chunk)
                        written += len(chunk)
                        now = time.monotonic()
                        if now - last_log >= _LOG_INTERVAL:
                            last_log = now
                            mb = written / (1024 * 1024)
                            if expected:
                                pct = written * 100 // expected
                                logger.debug(
                                    f"B站 {label} 下载中... {mb:.0f}MB/"
                                    f"{expected / (1024 * 1024):.0f}MB ({pct}%)"
                                )
                            else:
                                logger.debug(f"B站 {label} 下载中... {mb:.0f}MB")
                    await f.flush()
            if written <= 0:
                raise IncompleteDownloadError("文件下载为空")
            if expected is not None and written != expected:
                raise IncompleteDownloadError(
                    f"下载长度不完整: {written}/{expected} bytes"
                )
            replace_file(temp_path, save_path)
            temp_path = None
            return save_path
        except asyncio.CancelledError:
            # 取消不进入网络重试；finally 清理临时文件与预算
            raise
        except DownloadLimitExceeded:
            raise
        except Exception as e:
            last_exc = e
            logger.warning(f"{label} CDN #{i + 1} 失败: {e}")
            if not is_retryable_download_error(e):
                raise
            if i + 1 < len(urls):
                await asyncio.sleep(min(0.5 * (2**i), 2.0))
        finally:
            if temp_path:
                remove_quietly(temp_path)
            if ticket is not None and written:
                # 失败时释放本次占用；成功后由上层 discard
                if temp_path is not None:
                    await shield_cleanup(ticket.rollback())
    raise last_exc or Exception(f"{label} 所有 CDN 节点均下载失败")


async def _download_single(
    durl_urls: list[str],
    save_path: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    ticket: Optional[BudgetTicket] = None,
) -> str:
    if client is not None:
        return await _try_download_urls(
            client, durl_urls, save_path, "视频流", ticket=ticket
        )
    async with _new_download_client() as own:
        return await _try_download_urls(
            own, durl_urls, save_path, "视频流", ticket=ticket
        )


async def _download_dash(
    video_urls: list[str],
    audio_urls: list[str],
    save_path: str,
    need_reencode: bool = False,
    *,
    client: Optional[httpx.AsyncClient] = None,
    video_ticket: Optional[BudgetTicket] = None,
    audio_ticket: Optional[BudgetTicket] = None,
) -> str:
    temp_dir = os.path.dirname(save_path) or "."
    os.makedirs(temp_dir, exist_ok=True)
    tag = os.urandom(8).hex()
    baseno = os.path.basename(save_path)
    video_temp = os.path.join(temp_dir, f"_v_{tag}_{baseno}")
    audio_temp = os.path.join(temp_dir, f"_a_{tag}_{baseno}")
    merge_temp = unique_temp_path(save_path)
    process = None
    completed = False

    async def _fetch_both(active_client: httpx.AsyncClient):
        v_task = asyncio.create_task(
            _try_download_urls(
                active_client,
                video_urls,
                video_temp,
                "DASH 视频流",
                ticket=video_ticket,
            )
        )
        a_task = asyncio.create_task(
            _try_download_urls(
                active_client,
                audio_urls,
                audio_temp,
                "DASH 音频流",
                ticket=audio_ticket,
            )
        )
        tasks = [v_task, a_task]
        try:
            return await asyncio.gather(*tasks)
        except BaseException:
            # 一路失败/取消：取消并等待另一路结束后再清理
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
            raise

    try:
        if client is not None:
            v_result, a_result = await _fetch_both(client)
        else:
            async with _new_download_client() as own:
                v_result, a_result = await _fetch_both(own)

        if need_reencode:
            cmd = [
                "ffmpeg",
                "-i",
                v_result,
                "-i",
                a_result,
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-movflags",
                "+faststart",
                "-y",
                merge_temp,
            ]
        else:
            cmd = [
                "ffmpeg",
                "-i",
                v_result,
                "-i",
                a_result,
                "-c",
                "copy",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-movflags",
                "+faststart",
                "-y",
                merge_temp,
            ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise Exception(
                "ffmpeg 未安装，B站 DASH 格式需要 ffmpeg 合并视频流和音频流。"
                "安装命令：apt-get update && apt-get install ffmpeg -y"
            ) from e
        try:
            _, stderr_data = await process.communicate()
        except asyncio.CancelledError:
            # 合并中取消：终止并等待子进程，避免残留 ffmpeg 与半成品
            await shield_cleanup(_terminate_process(process))
            raise

        if process.returncode != 0:
            raise Exception(
                f"ffmpeg merge failed: {stderr_data.decode(errors='ignore')[:500]}"
            )
        if not os.path.exists(merge_temp) or os.path.getsize(merge_temp) <= 0:
            raise Exception("ffmpeg 合并后文件未生成或为空")
        # 重编码后大小可能变化，最终大小校验仍由上层完成
        replace_file(merge_temp, save_path)
        merge_temp = None
        completed = True
        return save_path
    finally:
        for tmp in (video_temp, audio_temp, merge_temp):
            remove_quietly(tmp)
        if not completed:
            for ticket in (video_ticket, audio_ticket):
                if ticket is not None:
                    await shield_cleanup(ticket.discard())


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """终止并等待 ffmpeg 子进程结束。"""
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


# ---------------------------------------------------------------------------
# 主下载流程
# ---------------------------------------------------------------------------


async def download_video(
    bvid: str,
    cid: int,
    download_dir: str,
    quality: int = 64,
    cookies: dict | None = None,
    title: str = "",
    owner_name: str = "",
    *,
    max_size_mb: Optional[float] = None,
    duration: float = 0,
) -> str:
    os.makedirs(download_dir, exist_ok=True)
    base = _make_base_name(owner_name, title, bvid) if (title or owner_name) else bvid
    output_path = os.path.join(download_dir, f"{base}.mp4")
    max_bytes = resolve_budget_limit(max_size_mb)
    budget = ByteBudget(max_bytes)

    async with _new_download_client() as client:
        # Phase 1: PROBE — 探明可用画质
        probe_data = await _get_playurl(bvid, cid, qn=120, fnval=4048, cookies=cookies)
        accept_qn = probe_data.get("accept_quality") or []
        if not accept_qn:
            accept_qn = [
                v["id"] for v in (probe_data.get("dash") or {}).get("video") or []
            ]
        target_qn = _best_qn(accept_qn, quality) if accept_qn else quality
        logger.debug(f"PROBE 结果: accept_quality={accept_qn}, target_qn={target_qn}")

        # Phase 2: 单流 (fnval=0)，仅当实际质量达到目标才直接用
        merged = await _get_playurl(bvid, cid, qn=target_qn, fnval=0, cookies=cookies)
        actual_quality = merged.get("quality")
        fallback_durl_urls: list[str] = []
        durl_item = (merged.get("durl") or [{}])[0]
        if merged.get("durl"):
            durl_urls = _collect_urls(durl_item.get("url"), durl_item.get("backup_url"))
            if durl_urls:
                if actual_quality is None or actual_quality >= target_qn:
                    stream = dict(durl_item)
                    stream["urls"] = durl_urls
                    await _preflight_streams(client, [stream], max_bytes, "单流")
                    ticket = BudgetTicket(budget)
                    try:
                        return await _download_single(
                            durl_urls, output_path, client=client, ticket=ticket
                        )
                    except BaseException:
                        remove_quietly(output_path)
                        await shield_cleanup(ticket.discard())
                        raise
                fallback_durl_urls = durl_urls

        # Phase 3: DASH (fnval=4048)
        try:
            if shutil.which("ffmpeg") is None:
                raise Exception(
                    "ffmpeg 未安装，B站 DASH 格式需要 ffmpeg 合并视频流和音频流。"
                    "安装命令：apt-get update && apt-get install ffmpeg -y"
                )
            dash_data = await _get_playurl(
                bvid, cid, qn=target_qn, fnval=4048, cookies=cookies
            )
            dash = dash_data.get("dash")
            if not dash or not dash.get("video"):
                raise Exception(
                    f"无法获取视频流: DASH 格式不可用 (target_qn={target_qn})"
                )

            videos = dash["video"]
            audios = dash.get("audio", [])
            need_reencode = not _has_avc(videos)
            selected_video = _pick_dash_stream(videos, target_qn, prefer_avc=True)
            if not selected_video:
                raise Exception("DASH 响应中无可用的视频流")
            selected_audio = _pick_best_audio(audios)
            if not selected_audio:
                raise Exception("DASH 响应中无可用的音频流")

            video_urls = _collect_urls(
                selected_video.get("baseUrl"), selected_video.get("backupUrl")
            )
            audio_urls = _collect_urls(
                selected_audio.get("baseUrl"), selected_audio.get("backupUrl")
            )
            if not video_urls:
                raise Exception("DASH 视频流 URL 列表为空")
            if not audio_urls:
                raise Exception("DASH 音频流 URL 列表为空")

            video_stream = {
                **selected_video,
                "urls": video_urls,
            }
            audio_stream = {
                **selected_audio,
                "urls": audio_urls,
            }
            estimate_mb = _estimate_selected_bytes(
                [selected_video, selected_audio], duration
            )
            if estimate_mb:
                logger.debug(
                    f"选中视频+音频估算体积: {estimate_mb:.1f}MB（时长 {duration:.0f}s）"
                )
            # 视频与音频共享一次下载的字节预算；只有全部已知才能提前跳过
            await _preflight_streams(
                client, [video_stream, audio_stream], max_bytes, "DASH 音视频合计"
            )

            v_ticket = BudgetTicket(budget)
            a_ticket = BudgetTicket(budget)
            try:
                codec_hint = selected_video.get("codecs", "?")
                if need_reencode:
                    logger.info(
                        f"视频编码 {codec_hint} 非 avc，ffmpeg 重编码为 libx264"
                    )
                else:
                    logger.debug(f"视频编码 {codec_hint}，ffmpeg stream copy")
                return await _download_dash(
                    video_urls,
                    audio_urls,
                    output_path,
                    need_reencode,
                    client=client,
                    video_ticket=v_ticket,
                    audio_ticket=a_ticket,
                )
            except BaseException:
                remove_quietly(output_path)
                await shield_cleanup(v_ticket.discard())
                await shield_cleanup(a_ticket.discard())
                raise
        except (asyncio.CancelledError, SizePrecheckExceeded, DownloadLimitExceeded):
            # 取消、下载前明确超限、过程超限：不进入普通网络错误兜底
            raise
        except Exception as e:
            if fallback_durl_urls:
                logger.warning(
                    f"DASH 下载失败 ({e})，降级使用单流兜底（实际质量 {actual_quality}）"
                )
                stream = dict(durl_item)
                stream["urls"] = fallback_durl_urls
                await _preflight_streams(client, [stream], max_bytes, "单流兜底")
                ticket = BudgetTicket(budget)
                try:
                    return await _download_single(
                        fallback_durl_urls,
                        output_path,
                        client=client,
                        ticket=ticket,
                    )
                except BaseException:
                    remove_quietly(output_path)
                    await shield_cleanup(ticket.discard())
                    raise
            raise


async def probe_quality_plan(bvid: str, cid: int, use_login: bool = False) -> dict:
    """探测各清晰度档位的真实码率（DASH bandwidth）与音频带宽。

    返回 {"qualities": {...}, "accept_quality": [], "audio_bandwidth": int}
    """
    cookies = None
    if use_login:
        try:
            cookies = await load_cookies()
        except Exception as e:
            logger.warning(f"加载 B 站 Cookie 用于探测失败: {e}")
    try:
        probe_data = await _get_playurl(bvid, cid, qn=120, fnval=4048, cookies=cookies)
    except Exception as e:
        logger.warning(f"探测清晰度码率失败: {e}")
        return {"qualities": {}, "accept_quality": [], "audio_bandwidth": 0}

    accept_quality = probe_data.get("accept_quality") or []
    dash = probe_data.get("dash") or {}
    dash_videos = dash.get("video") or []
    try:
        audio_bandwidth = max(
            (int(a.get("bandwidth") or 0) for a in (dash.get("audio") or [])),
            default=0,
        )
    except (TypeError, ValueError):
        audio_bandwidth = 0
    qualities: dict = {}
    by_id: dict = {}
    for v in dash_videos:
        by_id.setdefault(v.get("id"), []).append(v)
    for qn, streams in by_id.items():
        if qn is None:
            continue
        avc = [s for s in streams if str(s.get("codecs", "")).startswith("avc1")]
        pool = avc or streams
        best = max(pool, key=lambda s: s.get("bandwidth", 0))
        qualities[qn] = {
            "bandwidth": int(best.get("bandwidth", 0) or 0),
            "width": int(best.get("width", 0) or 0),
            "height": int(best.get("height", 0) or 0),
            "codecs": best.get("codecs", ""),
        }
    return {
        "qualities": qualities,
        "accept_quality": accept_quality,
        "audio_bandwidth": audio_bandwidth,
    }


async def download_video_with_login(
    bvid: str,
    cid: int,
    download_dir: str,
    quality: int = 80,
    title: str = "",
    owner_name: str = "",
    *,
    max_size_mb: Optional[float] = None,
    duration: float = 0,
) -> str:
    cookies = await load_cookies()
    if not cookies:
        raise Exception("未找到登录 Cookie，请使用 /bili_login 登录")
    return await download_video(
        bvid=bvid,
        cid=cid,
        download_dir=download_dir,
        quality=quality,
        cookies=cookies,
        title=title,
        owner_name=owner_name,
        max_size_mb=max_size_mb,
        duration=duration,
    )


async def download_video_no_login(
    bvid: str,
    cid: int,
    download_dir: str,
    quality: int = 16,
    title: str = "",
    owner_name: str = "",
    *,
    max_size_mb: Optional[float] = None,
    duration: float = 0,
) -> str:
    return await download_video(
        bvid=bvid,
        cid=cid,
        download_dir=download_dir,
        quality=quality,
        cookies=None,
        title=title,
        owner_name=owner_name,
        max_size_mb=max_size_mb,
        duration=duration,
    )
