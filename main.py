from astrbot.api.all import *
from astrbot.api.message_components import Node, Plain, Image, Video, Nodes
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
from astrbot.api.star import StarTools
import astrbot.api.message_components as Comp

import re
import os
import asyncio

from .file_send_server import send_file
from .bili_get import (
    process_bili_video, REG_B23, REG_BV, REG_AV, av2bv, parse_b23, parse_video,
    estimate_size, init_bili_module, bili_login, check_cookie_valid
)
from .douyin_get import process_douyin_video 
from .auto_delete import delete_old_files

MAX_PROCESS_RETRIES = 0
MAX_SEND_RETRIES = 2
MAX_QUALITY_DOWNSCALE = 3

async def async_delete_old_files(folder_path: str, time_threshold_minutes: int) -> int:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, delete_old_files, folder_path, time_threshold_minutes)


@register("astrbot_plugin_video_analysis", "Foolllll", "可以解析B站和抖音视频及图片", "1.1.0", "https://github.com/Foolllll-J/astrbot_plugin_video_analysis")
class videoAnalysis(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.nap_server_address = config.get("nap_server_address", "localhost")
        self.nap_server_port = config.get("nap_server_port", 3658)
        self.group_whitelist: List[int] = [int(gid) for gid in config.get("group_whitelist", [])]
        self.delete_time = config.get("delete_time", 60)    
        self.max_video_size = config.get("max_video_size", 200)
        self.bili_quality = config.get("bili_quality", 32)
        self.bili_use_login = config.get("bili_use_login", False)
        self.bili_smart_downgrade = config.get("bili_smart_downgrade", True) # 【加载新增配置】
        self.douyin_cookie = config.get("douyin_cookie", None)
        self.douyin_api_url = config.get("douyin_api_url", None)
        
        # 设置数据目录
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_video_analysis")
        self.download_dir = os.path.join(self.data_dir, "download_videos")
        os.makedirs(self.download_dir, exist_ok=True)
        
        # 初始化 bili_get 模块
        cookie_file = os.path.join(self.data_dir, "bili_cookies.json")
        init_bili_module(cookie_file)
        
        logger.info(f"插件初始化完成。配置：NAP地址={self.nap_server_address}:{self.nap_server_port}, B站质量={self.bili_quality}, 使用登录={self.bili_use_login}, 智能降级={self.bili_smart_downgrade}, 启用群组: {self.group_whitelist if self.group_whitelist else '全部'}")

    async def _send_file_if_needed(self, file_path: str) -> str:
        """Helper function to send file through NAP server if needed"""
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
        目标：如果视频过大，回复文本；否则，只发送视频组件。
        """
        
        file_path_rel = result.get("video_path")
        media_component = None
        message_to_send = None

        # 0. 检查文件是否存在
        if not (file_path_rel and os.path.exists(file_path_rel)):
            logger.error(f"process_bili_video/douyin_video 返回成功，但文件路径无效或文件不存在: {file_path_rel}")
            message_to_send = [Plain("抱歉，由于网络或解析问题，无法获取视频文件。")]
        else:
            file_size_mb = os.path.getsize(file_path_rel) / (1024 * 1024)
            logger.info(f"文件大小为 {file_size_mb:.2f} MB，最大限制为 {self.max_video_size} MB。")

            # 1. 判断是否超出大小限制
            if file_size_mb > self.max_video_size:
                # 视频过大，不发送视频，只回复文本消息
                message_to_send = [Plain(f"抱歉，该视频文件大小为 {file_size_mb:.2f}MB，超过了 {self.max_video_size}MB 的最大限制，无法发送视频消息。")]
                logger.warning(f"视频大小超出限制，将回复文本消息。")
            else:
                # 视频在限制内，构建视频组件
                nap_file_path = await self._send_file_if_needed(file_path_rel) 
                
                media_component = Comp.Video.fromFileSystem(path = nap_file_path)
                message_to_send = [media_component]
                logger.info(f"视频在大小限制内，构建 Video 组件。")

        
        # 2. 发送逻辑
        if message_to_send:
            for send_attempt in range(MAX_SEND_RETRIES + 1):
                try:
                    yield event.chain_result(message_to_send)
                    logger.info(f"消息发送成功 (总尝试次数: {send_attempt + 1})。")
                    break
                    
                except Exception as e:
                    if send_attempt < MAX_SEND_RETRIES:
                        logger.warning(f"消息发送失败 (第 {send_attempt + 1} 次)，等待 2 秒后重试... 错误: {e}")
                        await asyncio.sleep(2)
                    else:
                        logger.error(f"消息发送最终失败 ({MAX_SEND_RETRIES + 1} 次重试)。错误: {e}", exc_info=True)
                        # 如果是发送文本失败，回复警告文本
                        yield event.plain_result("警告：消息发送失败，请稍后重试。")
                        return
        else:
            # 如果因其他原因导致 message_to_send 为空
            logger.error("未找到有效的文件或消息组件，跳过发送。")
            return

        # 4. 文件清理
        download_dir_platform = os.path.join(self.download_dir, platform)
        logger.info(f"发送完成，开始清理 {platform} 旧文件，阈值：{self.delete_time}分钟 (目录: {download_dir_platform})")
        await async_delete_old_files(download_dir_platform, self.delete_time)

    async def _handle_bili_parsing(self, event: AstrMessageEvent, url: str):
        """
        Bilibili 解析和下载核心逻辑
        """
        # 降级映射表：当前质量 -> 降级质量
        DOWNGRADE_MAP = {120: 112, 112: 80, 80: 64, 64: 32, 32: 16, 16: 16}
        
        initial_quality = self.bili_quality
        max_size = self.max_video_size 
        use_login = self.bili_use_login
        videos_download = True
        
        result = None
        current_quality = initial_quality
        download_attempts = 0 # 记录总下载尝试次数
        
        # --- 步骤 1: 预解析视频信息 ---
        bvid_match = REG_BV.search(url)
        av_match = REG_AV.search(url)
        short_url_match = REG_B23.search(url)
        
        video_info = None
        if short_url_match:
            video_info = await parse_b23(short_url_match.group(0))
        elif bvid_match:
            video_info = await parse_video(bvid_match.group(0))
        elif av_match:
            bvid = av2bv(av_match.group(0))
            video_info = await parse_video(bvid) if bvid else None

        if not video_info:
            yield event.plain_result("抱歉，无法解析视频信息，无法进行下载。请稍后重试。")
            return
            
        duration = video_info.get("duration", 0)
        
        # --- 步骤 2: 清晰度智能降级预估循环 ---
        for downgrade_count in range(MAX_QUALITY_DOWNSCALE + 1): 
            if downgrade_count == 0:
                target_quality = initial_quality
                if self.bili_smart_downgrade and duration > 0:
                    temp_quality = initial_quality
                    while temp_quality >= 16:
                        estimated_size_mb = estimate_size(temp_quality, duration)
                        if estimated_size_mb <= max_size: break
                        next_q = DOWNGRADE_MAP.get(temp_quality)
                        if next_q is None or next_q == temp_quality: break
                        temp_quality = next_q
                    target_quality = temp_quality
                    logger.info(f"智能预估：视频时长 {duration}s，初始质量 {initial_quality} 预估降级到 {target_quality}。")
                
                current_quality = target_quality

            # 如果不是第一次循环 (即前一次下载失败且文件过大)，则必须降级
            elif download_attempts > 0:
                current_quality = DOWNGRADE_MAP.get(current_quality)
                if current_quality is None or current_quality == DOWNGRADE_MAP.get(current_quality, 0): 
                    logger.error("已尝试最低清晰度，或达到降级上限。停止降级重试。")
                    break 
                logger.warning(f"文件超限，启动后置校验降级重试。新质量: {current_quality} (第 {downgrade_count} 次降级)。")

            download_attempts += 1
            logger.info(f"[INFO] 正在尝试下载 (质量: {current_quality}，总尝试次数: {download_attempts})...")

            try:
                result = await process_bili_video(url, download_flag=videos_download, quality=current_quality, use_login=use_login, event=None, download_dir=os.path.join(self.download_dir, "bili"))
            
            except Exception as e:
                logger.error(f"下载失败（yutto执行异常）: {e}", exc_info=False)
                break 

            file_path_rel = result.get("video_path") if result else None
            
            if not file_path_rel or not os.path.exists(file_path_rel):
                logger.warning("下载未成功，文件未找到。不进行大小校验，停止降级重试。")
                break 
                
            file_size_mb = os.path.getsize(file_path_rel) / (1024 * 1024)
            
            if file_size_mb <= max_size:
                logger.info(f"文件大小 {file_size_mb:.2f}MB 满足限制 {max_size}MB。下载成功。")
                break 
            
            # 文件过大，检查是否还能继续降级
            next_quality = DOWNGRADE_MAP.get(current_quality)
            can_downgrade = next_quality is not None and next_quality != current_quality and downgrade_count < MAX_QUALITY_DOWNSCALE
            
            if can_downgrade:
                # 还能降级，删除文件准备重试
                logger.warning(f"后置校验失败！文件实际大小 {file_size_mb:.2f}MB 超出限制 {max_size}MB。删除文件，准备降级重试...")
                try:
                    os.remove(file_path_rel)
                    logger.info(f"已删除超限文件: {file_path_rel}")
                except Exception as e:
                    logger.error(f"删除超限文件失败: {e}")
            else:
                # 无法继续降级，保留文件让后续统一处理
                logger.warning(f"后置校验失败！文件实际大小 {file_size_mb:.2f}MB 超出限制 {max_size}MB。已达降级上限，保留文件交由后续处理。")
                break

        # --- 步骤 3: 统一处理和发送 ---
        async for response in self._process_and_send(event, result, 'bili'):
            yield response


    async def _handle_douyin_parsing(self, event: AstrMessageEvent, url: str):
        """
        抖音解析和下载核心逻辑
        """
        download_dir = os.path.join(self.download_dir, "douyin")
        result = None

        for attempt in range(MAX_PROCESS_RETRIES + 1):
            try:
                logger.info(f"尝试解析下载 (URL: {url}, 尝试次数: {attempt + 1}/{MAX_PROCESS_RETRIES + 1}")
                
                result = await process_douyin_video(url, download_dir=download_dir, api_url=self.douyin_api_url, cookie=self.douyin_cookie) 
                
                if not result:
                    if attempt < MAX_PROCESS_RETRIES: await asyncio.sleep(3); continue
                    else: logger.error("process_douyin_video 连续返回空值，最终失败.")
                
                # 检查是否是多媒体类型（图片或多视频）
                if result.get("type") in ["image", "images", "multi_video"]:
                    if result.get("image_paths") or result.get("video_paths"):
                        logger.info(f"第 {attempt + 1} 次尝试成功，获取到 {len(result.get('image_paths', []) or result.get('video_paths', []))} 个媒体文件。")
                        break
                
                # 检查文件是否存在（单视频）
                if result and result.get("video_path") and os.path.exists(result["video_path"]):
                    logger.info(f"第 {attempt + 1} 次尝试成功，文件已找到。")
                    break 
                if attempt < MAX_PROCESS_RETRIES: logger.warning("下载/合成失败，文件未找到。进行重试.")
                
            except Exception as e:
                if attempt < MAX_PROCESS_RETRIES: logger.error(f"第 {attempt + 1} 次尝试失败，发生异常: {e}. 等待后重试...", exc_info=False)
                else: logger.error(f"第 {attempt + 1} 次尝试失败，发生致命异常: {e}", exc_info=True)
            
            if attempt == MAX_PROCESS_RETRIES: logger.error(f"核心处理达到最大重试次数 ({MAX_PROCESS_RETRIES + 1} 次)，最终失败.")
            await asyncio.sleep(2)
        
        # 处理多媒体类型
        if result and (result.get("type") in ["image", "images", "multi_video"]):
            async for response in self._send_douyin_multimedia(event, result):
                yield response
        
        # 处理单视频类型
        elif result and result.get("video_path") and os.path.exists(result["video_path"]):
            async for response in self._process_and_send(event, result, 'douyin'):
                yield response

        # 处理失败情况
        else:
            yield event.plain_result("抱歉，由于网络或解析问题，无法完成抖音视频处理。")

        # 统一清理文件
        download_dir_douyin = os.path.join(self.download_dir, "douyin")
        await async_delete_old_files(download_dir_douyin, self.delete_time)
    
    async def _send_douyin_multimedia(self, event: AstrMessageEvent, result: dict):
        """发送抖音多媒体（单个直接发送，多个使用合并转发，保持原始顺序）"""
        ordered_media = result.get("ordered_media", [])
        
        # 兼容旧版本或 API 返回的格式
        if not ordered_media:
            image_paths = result.get("image_paths", [])
            video_paths = result.get("video_paths", [])
            for p in image_paths: ordered_media.append({"path": p, "type": "image"})
            for p in video_paths: ordered_media.append({"path": p, "type": "video"})
        
        if not ordered_media:
            logger.error("没有找到媒体文件")
            yield event.plain_result("抱歉，没有找到媒体文件。")
            return
        
        # --- 优化：单个媒体直接发送 ---
        if len(ordered_media) == 1:
            item = ordered_media[0]
            media_path = item["path"]
            if not os.path.exists(media_path):
                yield event.plain_result("抱歉，媒体文件不存在。")
                return
            
            try:
                nap_file_path = await self._send_file_if_needed(media_path)
                if item["type"] == "image":
                    yield event.chain_result([Image.fromFileSystem(path=nap_file_path)])
                else:
                    yield event.chain_result([Comp.Video.fromFileSystem(path=nap_file_path)])
                logger.info(f"成功直接发送单个媒体文件: {media_path}")
                return
            except Exception as e:
                logger.error(f"直接发送单个媒体失败: {e}", exc_info=True)
                yield event.plain_result(f"发送失败: {str(e)}")
                return

        # --- 多个媒体使用合并转发 ---
        logger.info(f"准备发送 {len(ordered_media)} 个媒体文件（保持顺序，合并转发）")
        
        sender_id = event.get_self_id()
        forward_nodes = []
        
        for idx, item in enumerate(ordered_media, 1):
            media_path = item["path"]
            media_type = item["type"]
            
            if not os.path.exists(media_path):
                logger.warning(f"文件不存在: {media_path}")
                continue
            
            try:
                nap_file_path = await self._send_file_if_needed(media_path)
                if media_type == "image":
                    component = Image.fromFileSystem(path=nap_file_path)
                else:
                    component = Comp.Video.fromFileSystem(path=nap_file_path)
                
                forward_nodes.append(Node(uin=sender_id, name="抖音内容", content=[component]))
            except Exception as e:
                logger.error(f"处理第 {idx} 个媒体 ({media_type}) 时出错: {e}", exc_info=True)
        
        if len(forward_nodes) == 0:
            yield event.plain_result("抱歉，无法加载媒体文件。")
            return
        
        # 发送合并转发消息
        try:
            merged_forward_message = Nodes(nodes=forward_nodes)
            yield event.chain_result([merged_forward_message])
            logger.info(f"成功发送 {len(forward_nodes)} 个媒体文件（合并转发）")
        except Exception as e:
            logger.error(f"发送合并转发消息失败: {e}", exc_info=True)
            yield event.plain_result(f"内容发送失败: {str(e)}")

    @filter.command("bili_login")
    async def handle_bili_login(self, event: AstrMessageEvent):
        """
        处理 B站 登录指令
        通过聊天消息发送二维码，等待用户扫码登录。
        """
        logger.info("收到 B站登录指令")
        
        # 生成二维码
        login_task, qr_data = await bili_login()
        
        if not qr_data:
            yield event.plain_result("生成登录二维码失败，请稍后重试。")
            return
        
        temp_image_path = None
        # 发送二维码图片给用户
        try:
            # 将 base64 二维码转换为图片组件
            import base64
            from io import BytesIO
            qr_image_data = base64.b64decode(qr_data["image_base64"])
            
            # 保存临时文件
            temp_image_path = os.path.join(self.data_dir, "bili_login_qrcode.png")
            with open(temp_image_path, "wb") as f:
                f.write(qr_image_data)
            
            # 发送图片和提示信息
            yield event.chain_result([
                Plain("请使用 B站APP 扫描以下二维码登录：\n"),
                Image.fromFileSystem(temp_image_path),
                Plain("\n等待登录中...（最多40秒）")
            ])
            
            # 等待登录完成
            cookies = await login_task
            
            if cookies:
                yield event.plain_result("✅ B站登录成功！Cookie 已保存。")
            else:
                yield event.plain_result("❌ 登录失败或超时，请重试。")
                
        except Exception as e:
            logger.error(f"处理登录流程时出错: {e}", exc_info=True)
            yield event.plain_result("登录过程中出现错误，请查看日志。")
        finally:
            # 安全删除临时二维码文件
            if temp_image_path and os.path.exists(temp_image_path):
                try:
                    os.remove(temp_image_path)
                    logger.debug(f"已删除临时二维码文件: {temp_image_path}")
                except Exception as e:
                    logger.warning(f"删除临时二维码文件失败: {e}")
    
    @filter.command("bili_check")
    async def handle_bili_check(self, event: AstrMessageEvent):
        """
        检查 B站 Cookie 是否有效
        """
        logger.info("收到检查 Cookie 指令")
        
        is_valid = await check_cookie_valid()
        
        if is_valid:
            yield event.plain_result("✅ B站 Cookie 有效")
        else:
            yield event.plain_result("❌ B站 Cookie 无效或不存在，请使用 /bili_login 登录")


@filter.event_message_type(EventMessageType.ALL)
async def auto_parse_dispatcher(self: videoAnalysis, event: AstrMessageEvent, *args, **kwargs):
    """
    【架构总控】自动检测消息中是否包含分享链接，并分发给相应的处理器。
    """
    group_id = event.get_group_id()
    if group_id:
        try:
            group_id = int(group_id)
        except ValueError:
            logger.info(f"无法将群组 ID '{group_id}' 转换为整数，跳过过滤。")
            return
            
        if self.group_whitelist and group_id not in self.group_whitelist:
            return

    message_str = event.message_str
    message_obj_str = str(event.message_obj)

    if re.search(r"reply", message_obj_str):
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
        # 检查是否配置了 API 地址或 Cookie
        if not self.douyin_api_url and not self.douyin_cookie:
            logger.warning("成功匹配到抖音链接，但 douyin_api_url 和 douyin_cookie 均未配置，跳过解析。")
            return
            
        url = match_douyin.group(1)
        logger.info(f"成功匹配到抖音短链接：{url}")
        
        # 调用抖音处理函数
        async for response in self._handle_douyin_parsing(event, url):
            yield response
        return
