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

# 将 auto_delete.py 中的函数封装为异步，通过线程池执行
async def async_delete_old_files(folder_path: str, time_threshold_minutes: int) -> int:
    loop = asyncio.get_event_loop()
    # 使用 run_in_executor 在单独的线程中运行同步的 delete_old_files 函数
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
        
        logger.info(f"插件初始化完成。配置：NAP地址={self.nap_server_address}:{self.nap_server_port}, B站质量={self.bili_quality}, 回复模式={self.bili_reply_mode}, 使用登录={self.bili_use_login}")

    async def _send_file_if_needed(self, file_path: str) -> str:
        """Helper function to send file through NAP server if needed"""
        logger.debug(f"检查NAP配置... 地址: {self.nap_server_address}, 端口: {self.nap_server_port}")
        if self.nap_server_address != "localhost":
            logger.info(f"检测到非本地地址，尝试通过NAP服务器发送文件：{file_path}")
            return await send_file(file_path, HOST=self.nap_server_address, PORT=self.nap_server_port)
        logger.info(f"检测到本地地址，直接使用文件路径：{file_path}")
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
    logger.debug(f"接收到新消息，内容：{event.message_str}")
    message_str = event.message_str
    message_obj_str = str(event.message_obj)

    # 1. 文件清理 (始终在最前面执行)
    # 假设 bili_get.py 返回的路径总是 'data/plugins/astrbot_plugin_video_analysis/download_videos/bili' 开头
    bili_download_dir_rel = "data/plugins/astrbot_plugin_video_analysis/download_videos/bili"
    logger.info(f"开始清理B站旧文件，阈值：{self.delete_time}分钟 (目录: {bili_download_dir_rel})")
    await async_delete_old_files(bili_download_dir_rel, self.delete_time)

    if re.search(r"reply", message_obj_str):
        logger.debug("消息是回复类型，跳过解析。")
        return

    match_json = re.search(r"https:\\\\/\\\\/b23\.tv\\\\/[a-zA-Z0-9]+", message_obj_str)
    match_plain = re.search(r"(https?://b23\.tv/[\w]+|https?://bili2233\.cn/[\w]+|BV1\w{9}|av\d+)", message_str)

    if not (match_plain or match_json):
        logger.debug("未在消息中匹配到Bili链接，跳过。")
        return

    url = ""
    if match_plain:
        url = match_plain.group(1)
        logger.info(f"成功匹配到Bili纯文本链接：{url}")
    elif match_json:
        url = match_json.group(0).replace("\\\\", "\\").replace("\\/", "/")
        logger.info(f"成功匹配到Bili JSON链接：{url}")

    quality = self.bili_quality
    reply_mode = self.bili_reply_mode
    url_mode = self.bili_url_mode
    use_login = self.bili_use_login
    videos_download = reply_mode in [2, 3, 4]
    zhuanfa = self.Merge_and_forward

    # ---- 将所有核心业务逻辑放入 try 块中 ----
    try:
        logger.info("开始调用 process_bili_video 进行解析。")
        result = await process_bili_video(url, download_flag=videos_download, quality=quality, use_login=use_login, event=None)
        logger.info(f"process_bili_video 返回结果：{result}") # <<-- 关键日志：检查返回结果 -->>

        if not result:
            logger.error("process_bili_video 返回空值，解析失败。")
            yield event.plain_result("抱歉，这个B站链接我不能打开，请检查一下链接是否正确。")
            return

        file_path_rel = result.get("video_path")
        media_component = None
        
        if file_path_rel:
            logger.info(f"解析结果中的视频文件路径 (相对): {file_path_rel}")
            
            if os.path.exists(file_path_rel):
                logger.info("文件存在性检查通过。")
                
                # 发送文件时，使用绝对路径
                nap_file_path = await self._send_file_if_needed(file_path_rel) 
                
                # 检查文件大小
                file_size_mb = os.path.getsize(file_path_rel) / (1024 * 1024)
                logger.info(f"文件大小为 {file_size_mb:.2f} MB，最大限制为 {self.max_video_size} MB。")
                
                if file_size_mb > self.max_video_size:
                    logger.warning("文件超出大小限制，将以文件形式发送。")
                    media_component = Comp.File(file=nap_file_path, name=os.path.basename(nap_file_path))
                else:
                    logger.info("文件在大小限制内，将以视频形式发送。")
                    media_component = Comp.Video.fromFileSystem(path = nap_file_path)
            else:
                logger.warning(f"os.path.exists() 检查失败，文件不存在于路径: {file_path_rel}")
                # 此警告日志即对应你之前遇到的问题，如果出现，请检查 bili_get.py 的路径是否也已修正
        else:
            logger.warning("process_bili_video 未返回视频文件路径。")

        logger.info("开始构建视频信息文本。")
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
        logger.debug(f"构建完成的信息文本：\n{info_text}")
        
        # --- 发送消息逻辑 ---
        logger.info(f"根据配置的回复模式 {reply_mode} 和合并转发模式 {zhuanfa} 准备发送消息。")

        if reply_mode == 0:
            logger.info("回复模式为0 (纯文本)，发送信息文本。")
            yield event.chain_result([Comp.Plain(info_text)])
        elif reply_mode == 1:
            logger.info("回复模式为1 (带图片)。")
            cover_url = result.get("cover")
            if cover_url:
                logger.info(f"获取到封面URL: {cover_url}")
                if zhuanfa:
                    logger.info("开启合并转发，发送封面和文本节点。")
                    ns = Nodes([])
                    ns.nodes.append(self._create_node(event, [Comp.Image.fromURL(cover_url)]))
                    ns.nodes.append(self._create_node(event, [Comp.Plain(info_text)]))
                    yield event.chain_result([ns])
                else:
                    logger.info("未开启合并转发，分别发送封面和文本。")
                    yield event.chain_result([Comp.Image.fromURL(cover_url)])
                    yield event.chain_result([Comp.Plain(info_text)])
            else:
                logger.warning("未获取到封面URL，以纯文本形式发送。")
                yield event.chain_result([Comp.Plain("封面图片获取失败\n" + info_text)])
        elif reply_mode == 2:
            logger.info("回复模式为2 (带视频)。")
            if media_component:
                if zhuanfa:
                    logger.info("开启合并转发，发送文本和视频。")
                    yield event.chain_result([Comp.Plain(info_text)])
                    yield event.chain_result([media_component])
                else:
                    logger.info("未开启合并转发，分别发送视频和文本。")
                    yield event.chain_result([media_component])
            else:
                logger.warning("未获取到媒体组件，以纯文本形式发送。")
                yield event.chain_result([Comp.Plain(info_text)])
        elif reply_mode == 3:
            logger.info("回复模式为3 (完整)。")
            cover_url = result.get("cover")
            if zhuanfa:
                logger.info("开启合并转发，发送全部内容。")
                if cover_url:
                    ns = Nodes([])
                    ns.nodes.append(self._create_node(event, [Comp.Image.fromURL(cover_url)]))
                    ns.nodes.append(self._create_node(event, [Comp.Plain(info_text)]))
                    yield event.chain_result([ns])
                else:
                    logger.warning("未获取到封面URL，发送文本。")
                    yield event.chain_result([Comp.Plain("封面图片获取失败\n" + info_text)])
                if media_component:
                    yield event.chain_result([media_component])
            else:
                logger.info("未开启合并转发，分别发送全部内容。")
                if cover_url:
                    yield event.chain_result([Comp.Image.fromURL(cover_url)])
                else:
                    logger.warning("未获取到封面URL，发送失败信息。")
                    yield event.chain_result([Comp.Plain("封面图片获取失败")])
                yield event.chain_result([Comp.Plain(info_text)])
                if media_component:
                    yield event.chain_result([media_component])
        elif reply_mode == 4:
            logger.info("回复模式为4 (纯视频)。")
            if media_component:
                yield event.chain_result([media_component])
                logger.info("成功发送纯视频。")
            else:
                logger.warning("未获取到媒体组件，无法发送纯视频。")
                yield event.plain_result("抱歉，未能下载到视频文件。")
                
    except Exception as e:
        # 捕获所有运行时异常并打印堆栈信息
        logger.error(f"在 auto_parse_bili 核心逻辑中发生致命错误: {e}", exc_info=True)
        yield event.plain_result(f"抱歉，插件在处理时发生内部错误：{str(e)}")