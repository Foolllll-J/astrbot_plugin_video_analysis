import asyncio
import json
import os
import subprocess

import aiofiles

from astrbot.api import logger

from .utils import map_quality_to_height

YUTTO_PATH = "/root/.local/bin/yutto"


def check_yutto_installed() -> bool:
    if os.path.exists(YUTTO_PATH):
        return True
    try:
        subprocess.run(["yutto", "--version"], check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _yutto_cmd() -> str:
    return YUTTO_PATH if os.path.exists(YUTTO_PATH) else "yutto"


async def download_video_yutto(
    bvid: str,
    cookies_file: str,
    download_dir: str,
    quality: int = 80,
    num_workers: int = 8,
) -> str:
    if not check_yutto_installed():
        raise Exception("yutto is not installed or not found in PATH.")

    os.makedirs(download_dir, exist_ok=True)
    output_path = os.path.join(download_dir, f"{bvid}.mp4")

    try:
        async with aiofiles.open(cookies_file, "r", encoding="utf-8") as f:
            json_cookies = json.loads(await f.read())
            sessdata = json_cookies.get("SESSDATA")
            if not sessdata:
                raise ValueError("Cookie 文件中缺少 SESSDATA 字段。")
    except Exception as e:
        raise Exception(f"无法获取 SESSDATA Cookie: {e}")

    if os.path.exists(output_path):
        os.remove(output_path)

    quality_qn = map_quality_to_height(quality)
    yutto = _yutto_cmd()

    cmd = [
        yutto,
        f"https://www.bilibili.com/video/{bvid}",
        "-c",
        sessdata,
        "-d",
        download_dir,
        "-q",
        str(quality_qn),
        "-n",
        str(num_workers),
        "-w",
        "--no-color",
        "--no-progress",
        "--subpath-template",
        bvid,
        "--no-danmaku",
        "--no-subtitle",
        "--no-cover",
    ]

    cmd_str = " ".join(cmd)
    logger.debug(f"yutto CMD: {cmd_str[:200]}{'...' if len(cmd_str) > 200 else ''}")

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    _, stderr_data = await process.communicate()

    if process.returncode != 0:
        error_output = stderr_data.decode(errors="ignore").strip()
        raise Exception(f"yutto 下载失败: {error_output[:500]}")

    if os.path.exists(output_path):
        os.utime(output_path, None)
        return output_path

    raise Exception("yutto 运行成功但未生成文件")


async def download_video_yutto_no_login(
    bvid: str,
    download_dir: str,
    quality: int = 16,
    num_workers: int = 8,
) -> str:
    if not check_yutto_installed():
        raise Exception("yutto is not installed or not found in PATH.")

    os.makedirs(download_dir, exist_ok=True)
    output_path = os.path.join(download_dir, f"{bvid}.mp4")

    if os.path.exists(output_path):
        os.remove(output_path)

    quality_qn = map_quality_to_height(quality)
    yutto = _yutto_cmd()

    cmd = [
        yutto,
        f"https://www.bilibili.com/video/{bvid}",
        "-d",
        download_dir,
        "-q",
        str(quality_qn),
        "-n",
        str(num_workers),
        "-w",
        "--no-color",
        "--no-progress",
        "--subpath-template",
        bvid,
        "--no-danmaku",
        "--no-subtitle",
        "--no-cover",
    ]

    cmd_str = " ".join(cmd)
    logger.debug(
        f"yutto CMD (no login): {cmd_str[:200]}{'...' if len(cmd_str) > 200 else ''}"
    )

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    _, stderr_data = await process.communicate()

    if process.returncode != 0:
        error_output = stderr_data.decode(errors="ignore").strip()
        raise Exception(f"yutto 下载失败（无登录）: {error_output[:500]}")

    if os.path.exists(output_path):
        os.utime(output_path, None)
        return output_path

    raise Exception("yutto 运行成功但未生成文件")
