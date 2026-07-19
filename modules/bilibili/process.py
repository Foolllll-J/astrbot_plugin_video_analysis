import os

from astrbot.api import logger

from .constants import REG_B23, REG_BV, REG_AV
from .parser import parse_b23, parse_video, av2bv, UnsupportedBiliLinkError
from .download import download_video_yutto, download_video_yutto_no_login
from . import utils


async def process_bili_video(
    url: str,
    download_flag: bool = True,
    quality: int = 80,
    use_login: bool = True,
    event=None,
    download_dir: str | None = None,
) -> dict:
    logger.debug(f"开始处理 B站 链接: {url}")

    video_info = None
    try:
        if REG_B23.search(url):
            video_info = await parse_b23(REG_B23.search(url).group())
        elif REG_BV.search(url):
            video_info = await parse_video(REG_BV.search(url).group())
        elif REG_AV.search(url):
            bvid = av2bv(REG_AV.search(url).group())
            video_info = await parse_video(bvid) if bvid else None
        else:
            logger.warning("不支持的链接格式")
            return {"error": "不支持的链接格式"}
    except UnsupportedBiliLinkError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error(f"解析链接时发生错误: {e}")
        return {"error": f"解析链接时发生错误: {e}"}

    if not video_info:
        logger.warning("解析视频信息失败")
        return {"error": "解析视频信息失败"}

    bvid = video_info.bvid
    stats = video_info.stats

    if download_dir is None:
        download_dir = "data/plugins/astrbot_plugin_video_analysis/downloads/bili"

    cookies_file = utils.COOKIE_FILE

    cached_file = os.path.join(download_dir, f"{bvid}.mp4")
    if os.path.exists(cached_file):
        logger.info(f"本地已存在视频文件：{cached_file}，跳过下载")
        return {
            "video_path": cached_file,
            "title": video_info.title,
            "cover": video_info.cover,
            "duration": video_info.duration,
            "stats": stats,
            "bvid": bvid,
            "view_count": stats["view"],
            "like_count": stats["like"],
            "danmaku_count": stats["danmaku"],
            "coin_count": stats["coin"],
            "favorite_count": stats["favorite"],
        }

    filename = None
    if download_flag:
        if use_login:
            logger.debug("调用 yutto 进行下载 (需登录凭证)...")
            try:
                filename = await download_video_yutto(
                    bvid, cookies_file, download_dir, quality=quality, num_workers=8
                )
            except Exception as e:
                error_str = str(e)
                if "尚不支持 DASH 格式" in error_str:
                    logger.warning(f"yutto 高清下载失败 (DASH 不支持)。错误: {e}")
                    return {"error": f"下载失败: {e}"}

                logger.warning(f"yutto 高清下载失败: {e}")
                logger.debug("尝试降级到 360p 无需登录模式...")
                try:
                    filename = await download_video_yutto_no_login(
                        bvid, download_dir, quality=16, num_workers=8
                    )
                    logger.debug(f"360p 降级下载成功: {filename}")
                except Exception as fallback_e:
                    logger.error(f"360p 降级下载也失败: {fallback_e}")
                    return {"error": f"360p 降级下载也失败: {fallback_e}"}
        else:
            logger.debug("未启用登录，尝试下载 360p...")
            try:
                filename = await download_video_yutto_no_login(
                    bvid, download_dir, quality=16, num_workers=8
                )
            except Exception as e:
                logger.warning(f"360p 下载失败: {e}")
                return {"error": f"360p 下载失败: {e}"}

    if not filename and download_flag:
        logger.warning("下载失败，无法获取视频文件。")
        return {"error": "下载失败，无法获取视频文件 (未知错误)"}

    return {
        "title": video_info.title,
        "cover": video_info.cover,
        "duration": video_info.duration,
        "stats": stats,
        "video_path": filename,
        "view_count": stats["view"],
        "like_count": stats["like"],
        "danmaku_count": stats["danmaku"],
        "coin_count": stats["coin"],
        "favorite_count": stats["favorite"],
        "bvid": bvid,
    }
