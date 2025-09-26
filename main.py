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
MAX_PROCESS_RETRIES = 2 # 核心逻辑 (下载/解析) 总共尝试 3 次
MAX_SEND_RETRIES = 2    # 消息发送 (回复) 总共尝试 3 次

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
        
    # ⚠️ NOTICE: _safe_send 函数已被删除，因为它是导致 SyntaxError 的原因，且其逻辑已内联。

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
    # --- 外层重试循环：核心逻辑 (解析、下载、合成) ---
    # ------------------------------------------------------------------
    result = None
    
    for attempt in range(MAX_PROCESS_RETRIES + 1):
        try:
            logger.info(f"尝试解析下载 (URL: {url}, 尝试次数: {attempt + 1}/{MAX_PROCESS_RETRIES + 1})")
            
            # 1. 调用核心下载函数
            result = await process_bili_video(url, download_flag=videos_download, quality=quality, use_login=use_login, event=None)
            logger.info(f"process_bili_video 返回结果：{result}")

            # 2. 检查结果是否为空 (新的重试点)
            if not result:
                if attempt < MAX_PROCESS_RETRIES:
                    logger.warning("process_bili_video 返回空值，可能是 API 偶发性失败。等待后重试...")
                    await asyncio.sleep(3) # 休息 3 秒后重试解析
                    continue
                else:
                    # 最终失败，直接跳出循环
                    logger.error("process_bili_video 连续返回空值，最终失败。")
                    break
            
            file_path_rel = result.get("video_path")
            
            # 3. 检查文件是否存在 (下载成功)
            if file_path_rel and os.path.exists(file_path_rel):
                logger.info(f"第 {attempt + 1} 次尝试成功，文件已找到。")
                break # 成功，跳出重试循环
            
            # 如果文件不存在，记录为下载/合成失败，等待重试
            if attempt < MAX_PROCESS_RETRIES:
                 logger.warning("下载/合成失败，文件未找到。进行重试...")
            
        except Exception as e:
            # 捕获异常（网络中断、FFmpeg 失败等）
            if attempt < MAX_PROCESS_RETRIES:
                logger.error(f"第 {attempt + 1} 次尝试失败，发生异常: {e}. 等待后重试...", exc_info=False)
            else:
                logger.error(f"第 {attempt + 1} 次尝试失败，发生致命异常: {e}", exc_info=True)
        
        # 达到最大重试次数，跳出循环
        if attempt == MAX_PROCESS_RETRIES:
            logger.error(f"核心处理达到最大重试次数 ({MAX_PROCESS_RETRIES + 1} 次)，最终失败。")
            break
        
        await asyncio.sleep(2) # 每次失败后等待 2 秒

    # ------------------------------------------------------------------
    # --- 消息发送逻辑 (仅在下载成功后执行) ---
    # ------------------------------------------------------------------

    # 检查核心处理是否成功
    if not result or not result.get("video_path") or not os.path.exists(result["video_path"]):
        logger.warning("核心处理最终失败，向用户发送通用错误。")
        yield event.plain_result("抱歉，由于网络或解析问题，无法完成视频处理。请稍后重试。")
        # 最终清理 (即使失败也要尝试清理可能残余的文件)
        bili_download_dir_rel = "data/plugins/astrbot_plugin_video_analysis/download_videos/bili"
        await async_delete_old_files(bili_download_dir_rel, self.delete_time)
        return # <-- 退出异步生成器，不带值

    # --- 准备发送组件 ---
    file_path_rel = result.get("video_path")
    media_component = None
    
    # 构建 media_component
    nap_file_path = await self._send_file_if_needed(file_path_rel) 
    file_size_mb = os.path.getsize(file_path_rel) / (1024 * 1024)
    if file_size_mb > self.max_video_size:
        media_component = Comp.File(file=nap_file_path, name=os.path.basename(nap_file_path))
    else:
        media_component = Comp.Video.fromFileSystem(path = nap_file_path)

    # 构建 info_text
    url_mode = self.bili_url_mode
    info_text = (
        f"📜 视频标题：{result.get('title', '未知标题')}\n"
        f"👀 观看次数：{result.get('view_count', 0)}\n"
        f"👍 点赞次数：{result.get('like_count', 0)}\n"
        f"💰 投币次数：{result.get('coin_count', 0)}\n"
        f"📂 收藏次数：{result.get('favorite_count', 0)}\n"
        f"💬 弹幕量：{result.get('danmaku_count', 0)}\n"
        f"⏳ 视频时长：{int(result.get('duration', 0) / 60)}分{result.get('duration', 0) % 60}秒\n"
    )
    if url_mode: info_text += f"🎥 视频直链：{result.get('direct_url', '无')}\n"
    info_text += f"🧷 原始链接：https://www.bilibili.com/video/{result.get('bvid', 'unknown')}"

    # --- 消息组件集合 (用于发送重试) ---
    reply_mode = self.bili_reply_mode
    zhuanfa = self.Merge_and_forward
    
    # --- 内层重试循环：消息发送 ---
    for send_attempt in range(MAX_SEND_RETRIES + 1):
        try:
            # 根据回复模式，组装消息组件列表
            content_to_send = []
            
            # --- 组装逻辑：通过 yield 语句将消息分步发送 ---
            
            if reply_mode == 0: # 纯文本
                content_to_send = [Comp.Plain(info_text)]
            elif reply_mode == 1: # 带图片
                cover_url = result.get("cover")
                if cover_url:
                    if zhuanfa:
                        ns = Nodes([]); ns.nodes.append(self._create_node(event, [Comp.Image.fromURL(cover_url)])); ns.nodes.append(self._create_node(event, [Comp.Plain(info_text)]))
                        content_to_send = [ns]
                    else:
                        yield event.chain_result([Comp.Image.fromURL(cover_url)]) # 图片独立发送
                        content_to_send = [Comp.Plain(info_text)]
                else:
                    content_to_send = [Comp.Plain("封面图片获取失败\n" + info_text)]
            elif reply_mode == 2: # 带视频
                if media_component:
                    if zhuanfa:
                        yield event.chain_result([Comp.Plain(info_text)]) # 文本独立发送
                        content_to_send = [media_component]
                    else:
                        content_to_send = [media_component]
                else:
                    content_to_send = [Comp.Plain(info_text)]
            elif reply_mode == 3: # 完整
                cover_url = result.get("cover")
                if zhuanfa:
                    if cover_url:
                        ns = Nodes([]); ns.nodes.append(self._create_node(event, [Comp.Image.fromURL(cover_url)])); ns.nodes.append(self._create_node(event, [Comp.Plain(info_text)]))
                        yield event.chain_result([ns]) # 封面+文本合并发送
                    if media_component:
                        content_to_send = [media_component] # 视频独立发送
                else:
                    if cover_url: yield event.chain_result([Comp.Image.fromURL(cover_url)])
                    yield event.chain_result([Comp.Plain(info_text)])
                    if media_component: content_to_send = [media_component] # 视频独立发送
            elif reply_mode == 4: # 纯视频
                if media_component:
                    content_to_send = [media_component]

            # 执行发送
            if content_to_send:
                yield event.chain_result(content_to_send)
                logger.info("消息发送成功。")
            
            logger.info(f"最终消息发送成功 (总尝试次数: {send_attempt + 1})。")
            return # <-- 修复后的退出点：退出异步生成器，不带值
            
        except Exception as e:
            if send_attempt < MAX_SEND_RETRIES:
                logger.warning(f"消息发送失败 (第 {send_attempt + 1} 次)，等待 2 秒后重试... 错误: {e}")
                await asyncio.sleep(2)
            else:
                logger.error(f"消息发送最终失败 ({MAX_SEND_RETRIES + 1} 次重试)。错误: {e}", exc_info=True)
                # 最终失败，不向用户发送包含错误的回复
                yield event.plain_result("警告：视频下载成功，但平台消息发送失败，请稍后查看。")
                return # <-- 修复后的退出点：退出异步生成器，不带值

    # 4. 文件清理 (在所有回复发送完成后执行)
    bili_download_dir_rel = "data/plugins/astrbot_plugin_video_analysis/download_videos/bili"
    logger.info(f"发送完成，开始清理B站旧文件，阈值：{self.delete_time}分钟 (目录: {bili_download_dir_rel})")
    await async_delete_old_files(bili_download_dir_rel, self.delete_time)