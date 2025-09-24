from astrbot.api.all import *
from astrbot.api.message_components import Node, Plain, Image, Video, Nodes
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
import astrbot.api.message_components as Comp

import re
import os
import json
import asyncio
import time

from .file_send_server import send_file
from .bili_get import process_bili_video
# 导入 auto_delete 模块
from .auto_delete import delete_old_files

# 将 auto_delete.py 中的函数改为异步，以在异步函数中调用
async def async_delete_old_files(folder_path: str, time_threshold_minutes: int) -> int:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, delete_old_files, folder_path, time_threshold_minutes)

@register("astrbot_plugin_video_analysis", "Foolllll", "可以解析B站视频", "0.1", "https://github.com/Foolllll-J/astrbot_plugin_video_analysis")
class videoAnalysis(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.nap_server_address = config.get("nap_server_address", "localhost")
        self.nap_server_port = config.get("nap_server_port", 3658)
        self.delete_time = config.get("delete_time", 60)
        self.max_video_size = config.get("max_video_size", 200)
        self.bili_quality = config.get("bili_quality", 32)
        self.bili_reply_mode = config.get("bili_reply_mode", 2)
        self.bili_url_mode = config.get("bili_url_mode", True)
        self.Merge_and_forward = config.get("Merge_and_forward", False)
        self.bili_use_login = config.get("bili_use_login", False)

    async def _send_file_if_needed(self, file_path: str) -> str:
        """Helper function to send file through NAP server if needed"""
        if self.nap_server_address != "localhost":
            return await send_file(file_path, HOST=self.nap_server_address, PORT=self.nap_server_port)
        return file_path

    def _create_node(self, event, content):
        """Helper function to create a node with consistent format"""
        return Node(
            uin=event.get_self_id(),
            name="astrbot",
            content=content
        )

@filter.event_message_type(EventMessageType.ALL)
async def auto_parse_bili(self: videoAnalysis, event: AstrMessageEvent, *args, **kwargs):
    """
    自动检测消息中是否包含bili分享链接，并根据配置进行解析。
    """
    message_str = event.message_str
    message_obj_str = str(event.message_obj)
    
    # 每次解析前，清理一次旧文件
    # 这里需要确保 auto_delete.py 文件中的函数是异步的
    bili_download_dir = "data/plugins/astrbot_plugin_video_analysis/download_videos/bili"
    logger.info(f"开始清理B站旧文件，阈值：{self.delete_time}分钟")
    await async_delete_old_files(bili_download_dir, self.delete_time)

    if re.search(r"reply", message_obj_str):
        return

    match_json = re.search(r"https:\\\\/\\\\/b23\.tv\\\\/[a-zA-Z0-9]+", message_obj_str)
    match_plain = re.search(r"(https?://b23\.tv/[\w]+|https?://bili2233\.cn/[\w]+|BV1\w{9}|av\d+)", message_str)

    if not (match_plain or match_json):
        return

    url = ""
    if match_plain:
        url = match_plain.group(1)
    elif match_json:
        url = match_json.group(0).replace("\\\\", "\\").replace("\\/", "/")

    quality = self.bili_quality
    reply_mode = self.bili_reply_mode
    url_mode = self.bili_url_mode
    use_login = self.bili_use_login
    videos_download = reply_mode in [2, 3, 4]
    zhuanfa = self.Merge_and_forward

    result = await process_bili_video(url, download_flag=videos_download, quality=quality, use_login=use_login, event=None)

    if not result:
        yield event.plain_result("抱歉，这个B站链接我不能打开，请检查一下链接是否正确。")
        return

    file_path = result.get("video_path")
    media_component = None
    if file_path and os.path.exists(file_path):
        nap_file_path = await self._send_file_if_needed(file_path) if self.nap_server_address != "localhost" else file_path
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if file_size_mb > self.max_video_size:
            media_component = Comp.File(file=nap_file_path, name=os.path.basename(nap_file_path))
        else:
            media_component = Comp.Video.fromFileSystem(path = nap_file_path)

    try:
        info_text = (
            f"📜 视频标题：{result.get('title', '未知标题')}\n"
            f"👀 观看次数：{result.get('view_count', 0)}\n"
            f"👍 点赞次数：{result.get('like_count', 0)}\n"
            f"💰 投币次数：{result.get('coin_count', 0)}\n"
            f"📂 收藏次数：{result.get('favorite_count', 0)}\n"
            f"💬 弹幕量：{result.get('danmaku_count', 0)}\n"
            f"⏳ 视频时长：{int(result.get('duration', 0) / 60)}分{result.get('duration', 0) % 60}秒\n"
        )
        if url_mode:
            info_text += f"🎥 视频直链：{result.get('direct_url', '无')}\n"
        info_text += f"🧷 原始链接：https://www.bilibili.com/video/{result.get('bvid', 'unknown')}"
    except Exception as e:
        logger.error(f"构建B站信息文本时出错: {e}")
        info_text = f"B站视频信息获取失败: {result.get('title', '未知视频')}"

    if reply_mode == 0:
        yield event.chain_result([Comp.Plain(info_text)])
    elif reply_mode == 1:
        cover_url = result.get("cover")
        if cover_url:
            if zhuanfa:
                ns = Nodes([])
                ns.nodes.append(self._create_node(event, [Comp.Image.fromURL(cover_url)]))
                ns.nodes.append(self._create_node(event, [Comp.Plain(info_text)]))
                yield event.chain_result([ns])
            else:
                yield event.chain_result([Comp.Image.fromURL(cover_url)])
                yield event.chain_result([Comp.Plain(info_text)])
        else:
            yield event.chain_result([Comp.Plain("封面图片获取失败\n" + info_text)])
    elif reply_mode == 2:
        if media_component:
            if zhuanfa:
                yield event.chain_result([Comp.Plain(info_text)])
                yield event.chain_result([media_component])
            else:
                yield event.chain_result([media_component])
        else:
            yield event.chain_result([Comp.Plain(info_text)])
    elif reply_mode == 3:
        cover_url = result.get("cover")
        if zhuanfa:
            if cover_url:
                ns = Nodes([])
                ns.nodes.append(self._create_node(event, [Comp.Image.fromURL(cover_url)]))
                ns.nodes.append(self._create_node(event, [Comp.Plain(info_text)]))
                yield event.chain_result([ns])
            else:
                yield event.chain_result([Comp.Plain("封面图片获取失败\n" + info_text)])
            if media_component:
                yield event.chain_result([media_component])
        else:
            if cover_url:
                yield event.chain_result([Comp.Image.fromURL(cover_url)])
            else:
                yield event.chain_result([Comp.Plain("封面图片获取失败")])
            yield event.chain_result([Comp.Plain(info_text)])
            if media_component:
                yield event.chain_result([media_component])
    elif reply_mode == 4:
        if media_component:
            yield event.chain_result([media_component])