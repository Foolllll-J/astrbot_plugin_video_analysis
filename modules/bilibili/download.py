import asyncio
import os
import re
import shutil
import time

import aiofiles
import httpx

from astrbot.api import logger

from .constants import PLAYURL_API
from .utils import load_cookies

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
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
    cookie_dict = {
        "SESSDATA": (cookies or {}).get("SESSDATA", ""),
        "bili_jct": (cookies or {}).get("bili_jct", ""),
    }
    headers = {
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/",
    }
    async with httpx.AsyncClient(headers=headers) as client:
        resp = await client.get(PLAYURL_API, params=params, cookies=cookie_dict)
        resp.raise_for_status()
        result = resp.json()
    if result.get("code") != 0:
        raise Exception(
            f"Bilibili PlayURL API error: code={result.get('code')}, "
            f"message={result.get('message', '')}"
        )
    return result["data"]


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
    """在真实可用档位中选取最接近目标档的档位。

    优先精确命中目标档；目标档不存在时，若同级互备档存在（112↔116）
    则返回互备档；否则取不超过目标档的最高档，再无则取最低档。
    """
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


_LOG_INTERVAL = 60


async def _try_download_urls(
    client: httpx.AsyncClient,
    urls: list[str],
    save_path: str,
    label: str = "",
) -> str:
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    last_exc: Exception | None = None
    for i, url in enumerate(urls):
        try:
            logger.debug(f"{label} 尝试 CDN #{i + 1}/{len(urls)}")
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                start_time = time.monotonic()
                last_log = start_time
                async with aiofiles.open(save_path, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        await f.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if now - last_log >= _LOG_INTERVAL:
                            last_log = now
                            elapsed = now - start_time
                            mb = downloaded / (1024 * 1024)
                            if total:
                                pct = downloaded * 100 // total
                                eta_s = (total - downloaded) / max(
                                    downloaded / elapsed, 1
                                )
                                eta_str = (
                                    f"预计剩余{eta_s / 60:.0f}m"
                                    if eta_s >= 60
                                    else f"预计剩余{eta_s:.0f}s"
                                )
                                logger.debug(
                                    f"B站 {label} 下载中... {mb:.0f}MB/{total / (1024 * 1024):.0f}MB "
                                    f"({pct}%) 已用{elapsed / 60:.0f}m {eta_str}"
                                )
                            else:
                                logger.debug(
                                    f"B站 {label} 下载中... {mb:.0f}MB 已用{elapsed / 60:.0f}m"
                                )
            if os.path.exists(save_path) and os.path.getsize(save_path) > 0:
                os.utime(save_path, None)
                return save_path
            raise Exception("文件下载不完整")
        except Exception as e:
            last_exc = e
            logger.warning(f"{label} CDN #{i + 1} 失败: {e}")
            if os.path.exists(save_path):
                try:
                    os.remove(save_path)
                except Exception:
                    pass
            continue
    raise last_exc or Exception(f"{label} 所有 CDN 节点均下载失败")


async def _download_single(durl_urls: list[str], save_path: str) -> str:
    download_headers = {
        "referer": "https://www.bilibili.com",
        "User-Agent": UA,
    }
    async with httpx.AsyncClient(
        headers=download_headers,
        follow_redirects=True,
        timeout=httpx.Timeout(300.0, connect=30.0),
    ) as client:
        return await _try_download_urls(client, durl_urls, save_path, "视频流")


async def _download_dash(
    video_urls: list[str],
    audio_urls: list[str],
    save_path: str,
    need_reencode: bool = False,
) -> str:
    temp_dir = os.path.dirname(save_path)
    os.makedirs(temp_dir, exist_ok=True)
    baseno = os.path.basename(save_path)
    video_temp = os.path.join(temp_dir, f"_v_{baseno}")
    audio_temp = os.path.join(temp_dir, f"_a_{baseno}")

    download_headers = {
        "referer": "https://www.bilibili.com",
        "User-Agent": UA,
    }

    try:
        async with httpx.AsyncClient(
            headers=download_headers,
            follow_redirects=True,
            timeout=httpx.Timeout(300.0, connect=30.0),
        ) as client:
            v_task = _try_download_urls(client, video_urls, video_temp, "DASH 视频流")
            a_task = _try_download_urls(client, audio_urls, audio_temp, "DASH 音频流")
            v_result, a_result = await asyncio.gather(v_task, a_task)

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
                save_path,
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
                save_path,
            ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise Exception(
                "ffmpeg 未安装，B站 DASH 格式需要 ffmpeg 合并视频流和音频流。"
                "安装命令：apt-get update && apt-get install ffmpeg -y"
            )
        _, stderr_data = await process.communicate()

        if process.returncode != 0:
            raise Exception(
                f"ffmpeg merge failed: {stderr_data.decode(errors='ignore')[:500]}"
            )

        if os.path.exists(save_path):
            os.utime(save_path, None)
            return save_path
        raise Exception("ffmpeg 合并后文件未生成")
    finally:
        for tmp in (video_temp, audio_temp):
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass


async def download_video(
    bvid: str,
    cid: int,
    download_dir: str,
    quality: int = 64,
    cookies: dict | None = None,
    title: str = "",
    owner_name: str = "",
) -> str:
    os.makedirs(download_dir, exist_ok=True)
    base = _make_base_name(owner_name, title, bvid) if (title or owner_name) else bvid
    output_path = os.path.join(download_dir, f"{base}.mp4")
    if os.path.exists(output_path):
        os.remove(output_path)

    # Phase 1: PROBE — 探明可用画质
    probe_data = await _get_playurl(bvid, cid, qn=120, fnval=4048, cookies=cookies)
    accept_qn = probe_data.get("accept_quality") or []
    if not accept_qn:
        accept_qn = [v["id"] for v in (probe_data.get("dash") or {}).get("video") or []]
    target_qn = _best_qn(accept_qn, quality) if accept_qn else quality
    logger.debug(f"PROBE 结果: accept_quality={accept_qn}, target_qn={target_qn}")

    # Phase 2: 尝试单流 (fnval=0)，仅当实际质量达到目标才直接用
    merged = await _get_playurl(bvid, cid, qn=target_qn, fnval=0, cookies=cookies)
    actual_quality = merged.get("quality")
    fallback_durl_urls: list[str] = []
    if merged.get("durl"):
        durl_urls = _collect_urls(
            merged["durl"][0].get("url"), merged["durl"][0].get("backup_url")
        )
        if durl_urls:
            if actual_quality is None or actual_quality >= target_qn:
                return await _download_single(durl_urls, output_path)
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
            raise Exception(f"无法获取视频流: DASH 格式不可用 (target_qn={target_qn})")

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

        codec_hint = selected_video.get("codecs", "?")
        if need_reencode:
            logger.info(f"视频编码 {codec_hint} 非 avc，ffmpeg 重编码为 libx264")
        else:
            logger.debug(f"视频编码 {codec_hint}，ffmpeg stream copy")

        return await _download_dash(video_urls, audio_urls, output_path, need_reencode)
    except Exception as e:
        if fallback_durl_urls:
            logger.warning(
                f"DASH 下载失败 ({e})，降级使用单流兜底（实际质量 {actual_quality}）"
            )
            return await _download_single(fallback_durl_urls, output_path)
        raise


async def probe_quality_plan(bvid: str, cid: int, use_login: bool = False) -> dict:
    """探测各清晰度档位的真实码率（DASH bandwidth）。

    返回 {"qualities": {qn: {"bandwidth", "width", "height", "codecs"}}, "accept_quality": []}
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
        return {"qualities": {}, "accept_quality": []}

    accept_quality = probe_data.get("accept_quality") or []
    dash_videos = (probe_data.get("dash") or {}).get("video") or []
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
    return {"qualities": qualities, "accept_quality": accept_quality}


async def download_video_with_login(
    bvid: str,
    cid: int,
    download_dir: str,
    quality: int = 80,
    title: str = "",
    owner_name: str = "",
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
    )


async def download_video_no_login(
    bvid: str,
    cid: int,
    download_dir: str,
    quality: int = 16,
    title: str = "",
    owner_name: str = "",
) -> str:
    return await download_video(
        bvid=bvid,
        cid=cid,
        download_dir=download_dir,
        quality=quality,
        cookies=None,
        title=title,
        owner_name=owner_name,
    )
