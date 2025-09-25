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

# --- 定义重试次数 ---
MAX_PROCESS_RETRIES = 2 # 总共尝试 3 次 (1次初始 + 2次重试)

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

    # ------------------------------------------------------------------
    # --- 核心逻辑和重试机制 ---
    # ------------------------------------------------------------------
    result = None
    last_error = None
    
    for attempt in range(MAX_PROCESS_RETRIES + 1):
        try:
            logger.info(f"尝试解析下载 (URL: {url}, 尝试次数: {attempt + 1}/{MAX_PROCESS_RETRIES + 1})")
            
            # 1. 调用核心下载函数
            result = await process_bili_video(url, download_flag=videos_download, quality=quality, use_login=use_login, event=None)
            logger.info(f"process_bili_video 返回结果：{result}")

            if not result:
                # 如果返回 None，说明链接本身解析失败或权限不足，不再重试
                logger.error("process_bili_video 返回空值，解析失败。不再重试。")
                yield event.plain_result("抱歉，这个B站链接我不能打开，请检查一下链接是否正确。")
                return

            file_path_rel = result.get("video_path")
            
            # 2. 检查文件是否存在
            if file_path_rel and os.path.exists(file_path_rel):
                logger.info(f"第 {attempt + 1} 次尝试成功，文件已找到。")
                break # 成功，跳出重试循环
            
            # 如果 result 不为 None 但文件不存在，视为下载或合成失败，记录并重试
            logger.warning(f"第 {attempt + 1} 次尝试失败：文件未找到。进行重试...")
            
        except Exception as e:
            last_error = e
            logger.error(f"第 {attempt + 1} 次尝试失败，发生异常: {e}", exc_info=True)
        
        # 3. 如果是最后一次尝试，跳出循环
        if attempt == MAX_PROCESS_RETRIES:
            logger.error(f"达到最大重试次数 ({MAX_PROCESS_RETRIES + 1} 次)，最终失败。")
            # 最终失败后，统一发送通用错误消息
            yield event.plain_result("抱歉，由于网络或下载问题，无法完成视频处理。请稍后重试。")
            return
        
        await asyncio.sleep(2) # 等待 2 秒后重试
        
    # ------------------------------------------------------------------
    # --- 消息发送逻辑 (仅在下载成功后执行) ---
    # ------------------------------------------------------------------

    # 如果 result 为 None，说明在循环中返回了错误消息，这里不应该继续执行
    if not result:
        return

    file_path_rel = result.get("video_path")
    media_component = None

    if file_path_rel:
        logger.info(f"解析结果中的视频文件路径 (相对): {file_path_rel}")
        
        if os.path.exists(file_path_rel):
            # 检查文件大小并创建 media_component (与原代码相同)
            # ... (这部分逻辑使用 file_path_rel 即可，不需要 file_path_abs)
            
            nap_file_path = await self._send_file_if_needed(file_path_rel) 
            file_size_mb = os.path.getsize(file_path_rel) / (1024 * 1024)
            logger.info(f"文件大小为 {file_size_mb:.2f} MB，最大限制为 {self.max_video_size} MB。")
            
            if file_size_mb > self.max_video_size:
                logger.warning("文件超出大小限制，将以文件形式发送。")
                media_component = Comp.File(file=nap_file_path, name=os.path.basename(nap_file_path))
            else:
                logger.info("文件在大小限制内，将以视频形式发送。")
                media_component = Comp.Video.fromFileSystem(path = nap_file_path)
        else:
            # 理论上不会执行，因为上面已经判断过了
            logger.error(f"逻辑错误：文件在重试循环中成功，但再次检查时丢失。路径: {file_path_rel}")
            yield event.plain_result("抱歉，文件下载成功但发送失败。")
            return

    logger.info("开始构建视频信息文本。")
    # ... (构建 info_text 逻辑保持不变) ...
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

    # 4. 文件清理 (在所有回复发送完成后执行)
    bili_download_dir_rel = "data/plugins/astrbot_plugin_video_analysis/download_videos/bili"
    logger.info(f"发送完成，开始清理B站旧文件，阈值：{self.delete_time}分钟 (目录: {bili_download_dir_rel})")
    await async_delete_old_files(bili_download_dir_rel, self.delete_time)
                
    # 捕获所有运行时异常的逻辑，已经通过循环内的 try/except 覆盖，
    # 并且在循环外已经通过 result 是否为 None 来发送通用失败消息。