from astrbot.api.all import *
from astrbot.api.message_components import Node, Plain, Image, Video, Nodes
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
import astrbot.api.message_components as Comp

import re
import os
import asyncio

from .file_send_server import send_file
from .bili_get import process_bili_video
from .douyin_get import process_douyin_video 
from .auto_delete import delete_old_files

MAX_PROCESS_RETRIES = 0
MAX_SEND_RETRIES = 2

async def async_delete_old_files(folder_path: str, time_threshold_minutes: int) -> int:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, delete_old_files, folder_path, time_threshold_minutes)


@register("astrbot_plugin_video_analysis", "Foolllll", "可以解析B站和抖音视频", "0.1", "https://github.com/Foolllll-J/astrbot_plugin_video_analysis")
class videoAnalysis(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.nap_server_address = config.get("nap_server_address", "localhost")
        self.nap_server_port = config.get("nap_server_port", 3658)
        self.delete_time = config.get("delete_time", 60)
        self.max_video_size = config.get("max_video_size", 200)
        self.bili_quality = config.get("bili_quality", 32)
        self.bili_reply_mode = config.get("bili_reply_mode", 4)
        self.bili_url_mode = config.get("bili_url_mode", True)
        self.Merge_and_forward = config.get("Merge_and_forward", False)
        self.bili_use_login = config.get("bili_use_login", False)
        self.douyin_api_url = config.get("douyin_api_url", None)
        
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
        
    async def _process_and_send(self, event: AstrMessageEvent, result: dict, platform: str):
        """
        统一的消息发送逻辑，处理组件构建、重试、清理。
        """
        
        file_path_rel = result.get("video_path")
        media_component = None
        
        # 1. 构建 media_component
        if file_path_rel and os.path.exists(file_path_rel):
            nap_file_path = await self._send_file_if_needed(file_path_rel) 
            file_size_mb = os.path.getsize(file_path_rel) / (1024 * 1024)
            logger.info(f"文件大小为 {file_size_mb:.2f} MB，最大限制为 {self.max_video_size} MB。")

            if file_size_mb > self.max_video_size:
                media_component = Comp.File(file=nap_file_path, name=os.path.basename(nap_file_path))
            else:
                media_component = Comp.Video.fromFileSystem(path = nap_file_path)
        
        # 2. 构建 info_text (参数根据平台动态获取)
        if platform == 'bili':
            reply_mode = self.bili_reply_mode
            url_mode = self.bili_url_mode
            zhuanfa = self.Merge_and_forward
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
        
        elif platform == 'douyin':
            # 抖音：强制使用纯视频模式 (reply_mode=4)
            reply_mode = 4 
            url_mode = False 
            zhuanfa = False
            info_text = f"📹 抖音视频：{result.get('title', '未知标题')}\n"
            info_text += f"作者：{result.get('author', 'N/A')}\n"
            info_text += f"🔗 原始链接：{result.get('url', 'N/A')}"
            
        else: return

        for send_attempt in range(MAX_SEND_RETRIES + 1):
            try:
                content_to_send = []
                
                if reply_mode == 0: content_to_send = [Comp.Plain(info_text)]
                elif reply_mode == 1: 
                    if platform == 'bili':
                        cover_url = result.get("cover")
                        if cover_url:
                            if zhuanfa:
                                ns = Nodes([]); ns.nodes.append(self._create_node(event, [Comp.Image.fromURL(cover_url)])); ns.nodes.append(self._create_node(event, [Comp.Plain(info_text)]))
                                content_to_send = [ns]
                            else:
                                yield event.chain_result([Comp.Image.fromURL(cover_url)])
                                content_to_send = [Comp.Plain(info_text)]
                        else: content_to_send = [Comp.Plain("封面图片获取失败\n" + info_text)]
                    else: content_to_send = [Comp.Plain(info_text)]
                elif reply_mode == 2 or reply_mode == 3: # 带视频 或 完整
                    if media_component:
                        if zhuanfa and platform == 'bili':
                            if reply_mode == 3 and result.get("cover"):
                                ns = Nodes([]); ns.nodes.append(self._create_node(event, [Comp.Image.fromURL(result["cover"])])); ns.nodes.append(self._create_node(event, [Comp.Plain(info_text)]))
                                yield event.chain_result([ns])
                            elif reply_mode == 2:
                                yield event.chain_result([Comp.Plain(info_text)])
                        
                        content_to_send = [media_component]
                    else: content_to_send = [Comp.Plain(info_text)]
                elif reply_mode == 4: # 纯视频
                    if media_component: content_to_send = [media_component]

                if content_to_send:
                    yield event.chain_result(content_to_send)
                    logger.info("消息发送成功。")
                
                logger.info(f"最终消息发送成功 (总尝试次数: {send_attempt + 1})。")
                break
                
            except Exception as e:
                if send_attempt < MAX_SEND_RETRIES:
                    logger.warning(f"消息发送失败 (第 {send_attempt + 1} 次)，等待 2 秒后重试... 错误: {e}")
                    await asyncio.sleep(2)
                else:
                    logger.error(f"消息发送最终失败 ({MAX_SEND_RETRIES + 1} 次重试)。错误: {e}", exc_info=True)
                    yield event.plain_result("警告：视频下载成功，但平台消息发送失败，请稍后查看。")
                    return

        # 4. 文件清理 (在所有回复发送完成后执行)
        download_dir_rel = f"data/plugins/astrbot_plugin_video_analysis/download_videos/{platform}"
        logger.info(f"发送完成，开始清理 {platform} 旧文件，阈值：{self.delete_time}分钟 (目录: {download_dir_rel})")
        await async_delete_old_files(download_dir_rel, self.delete_time)

    async def _handle_bili_parsing(self, event: AstrMessageEvent, url: str):
        """
        Bilibili 解析和下载核心逻辑
        """
        quality = self.bili_quality; reply_mode = self.bili_reply_mode; url_mode = self.bili_url_mode; use_login = self.bili_use_login
        videos_download = reply_mode in [2, 3, 4]; zhuanfa = self.Merge_and_forward
        
        result = None
        for attempt in range(MAX_PROCESS_RETRIES + 1):
            try:
                logger.info(f"尝试解析下载 (URL: {url}, 尝试次数: {attempt + 1}/{MAX_PROCESS_RETRIES + 1})")
                result = await process_bili_video(url, download_flag=videos_download, quality=quality, use_login=use_login, event=None)
                logger.info(f"process_bili_video 返回结果：{result}")
                
                if not result:
                    if attempt < MAX_PROCESS_RETRIES: await asyncio.sleep(3); continue
                    else: logger.error("process_bili_video 连续返回空值，最终失败."); break
                
                file_path_rel = result.get("video_path")
                if file_path_rel and os.path.exists(file_path_rel): logger.info(f"第 {attempt + 1} 次尝试成功，文件已找到."); break
                if attempt < MAX_PROCESS_RETRIES: logger.warning("下载/合成失败，文件未找到。进行重试.");
            except Exception as e:
                if attempt < MAX_PROCESS_RETRIES: logger.error(f"第 {attempt + 1} 次尝试失败，发生异常: {e}. 等待后重试...", exc_info=False)
                else: logger.error(f"第 {attempt + 1} 次尝试失败，发生致命异常: {e}", exc_info=True); break
            if attempt == MAX_PROCESS_RETRIES: logger.error(f"核心处理达到最大重试次数 ({MAX_PROCESS_RETRIES + 1} 次)，最终失败."); break
            await asyncio.sleep(2)

        if not result or not result.get("video_path") or not os.path.exists(result["video_path"]):
            yield event.plain_result("抱歉，由于网络或解析问题，无法完成视频处理。请稍后重试。")
            download_dir_rel = "data/plugins/astrbot_plugin_video_analysis/download_videos/bili"
            await async_delete_old_files(download_dir_rel, self.delete_time)
            return

        async for response in self._process_and_send(event, result, 'bili'):
            yield response

    async def _handle_douyin_parsing(self, event: AstrMessageEvent, url: str):
        """
        抖音解析和下载核心逻辑
        """
        download_dir = "data/plugins/astrbot_plugin_video_analysis/download_videos/douyin"
        result = None

        for attempt in range(MAX_PROCESS_RETRIES + 1):
            try:
                logger.info(f"尝试解析下载 (URL: {url}, 尝试次数: {attempt + 1}/{MAX_PROCESS_RETRIES + 1})")
                
                # FIX: 将 API 地址传递给 douyin_get.py
                result = await process_douyin_video(url, download_dir=download_dir, api_url=self.douyin_api_url) 
                
                if not result:
                    if attempt < MAX_PROCESS_RETRIES: await asyncio.sleep(3); continue
                    else: logger.error("process_douyin_video 连续返回空值，最终失败."); break
                
                # 检查文件是否存在
                if os.path.exists(result["video_path"]):
                    logger.info(f"第 {attempt + 1} 次尝试成功，文件已找到。")
                    break 
                if attempt < MAX_PROCESS_RETRIES: logger.warning("下载/合成失败，文件未找到。进行重试...");
                
            except Exception as e:
                if attempt < MAX_PROCESS_RETRIES: logger.error(f"第 {attempt + 1} 次尝试失败，发生异常: {e}. 等待后重试...", exc_info=False)
                else: logger.error(f"第 {attempt + 1} 次尝试失败，发生致命异常: {e}", exc_info=True); break
            
            if attempt == MAX_PROCESS_RETRIES: logger.error(f"核心处理达到最大重试次数 ({MAX_PROCESS_RETRIES + 1} 次)，最终失败."); break
            await asyncio.sleep(2)
        
        if not result or not os.path.exists(result["video_path"]):
            yield event.plain_result("抱歉，由于网络或解析问题，无法完成抖音视频处理。请稍后重试。")
            download_dir_rel = "data/plugins/astrbot_plugin_video_analysis/download_videos/douyin"
            await async_delete_old_files(download_dir_rel, self.delete_time)
            return

        async for response in self._process_and_send(event, result, 'douyin'):
            yield response


@filter.event_message_type(EventMessageType.ALL)
async def auto_parse_dispatcher(self: videoAnalysis, event: AstrMessageEvent, *args, **kwargs):
    """
    【架构总控】自动检测消息中是否包含分享链接，并分发给相应的处理器。
    """
    logger.debug(f"接收到新消息，内容：{event.message_str}")
    message_str = event.message_str
    message_obj_str = str(event.message_obj)

    if re.search(r"reply", message_obj_str):
        logger.debug("消息是回复类型，跳过解析。")
        return

    # --- 1. 检查 Bilibili 链接 ---
    match_bili = re.search(r"(https?://b23\.tv/[\w]+|https?://bili2233\.cn/[\w]+|BV1\w{9}|av\d+)", message_str)
    match_bili_json = re.search(r"https:\\\\/\\\\/b23\.tv\\\\/[a-zA-Z0-9]+", message_obj_str)
    
    if match_bili or match_bili_json:
        # 获取 B站 URL
        url = match_bili.group(1) if match_bili else match_bili_json.group(0).replace("\\\\", "\\").replace("\\/", "/")
            
        # 调用 Bilibili 处理函数
        async for response in self._handle_bili_parsing(event, url):
            yield response
        return
        
    # --- 2. 检查 抖音/TikTok 链接 ---
    # 匹配 v.douyin.com 短链接和文本中的短链接
    match_douyin = re.search(r"(https?://v\.douyin\.com/[a-zA-Z0-9\-\/_]+)", message_str)

    if match_douyin:
        # 检查是否配置了 API 地址
        if not self.douyin_api_url:
            logger.warning("成功匹配到抖音链接，但 douyin_api_url 未配置，跳过解析。")
            return
            
        url = match_douyin.group(1)
        logger.info(f"成功匹配到抖音短链接：{url}")
        
        # 调用抖音处理函数
        async for response in self._handle_douyin_parsing(event, url):
            yield response
        return
        
    logger.debug("未匹配到任何支持的视频链接，跳过。")
    