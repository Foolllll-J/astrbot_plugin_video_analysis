from astrbot.api.all import *
from astrbot.api.message_components import Node, Plain, Image, Video, Nodes
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger
from astrbot.api.star import StarTools
import astrbot.api.message_components as Comp

import re
import os
import asyncio
from typing import List

from .modules.file_send_server import send_file
from .modules.bili_get import (
    process_bili_video, REG_B23, REG_BV, REG_AV, av2bv, parse_b23, parse_video,
    estimate_size, init_bili_module, bili_login, check_cookie_valid, UnsupportedBiliLinkError
)
from .modules.douyin_get import (
    process_douyin_video,
    init_douyin_login,
    load_douyin_cookies,
    check_douyin_cookie_valid,
    get_effective_douyin_cookie,
    format_douyin_failure_message,
)
from .modules.auto_delete import delete_old_files
from .modules.parse_guard import (
    ParseGuard,
    check_group_level_requirement,
    contains_blocked_keyword,
)

MAX_DOUYIN_PROCESS_RETRIES = 1
MAX_SEND_RETRIES = 2

async def async_delete_old_files(folder_path: str, time_threshold_minutes: int) -> int:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, delete_old_files, folder_path, time_threshold_minutes)


class videoAnalysis(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        bili_config = config.get("bilibili", {})
        if not isinstance(bili_config, dict):
            bili_config = {}
        douyin_config = config.get("douyin", {})
        if not isinstance(douyin_config, dict):
            douyin_config = {}
        parse_throttle_config = config.get("parse_throttle", {})
        if not isinstance(parse_throttle_config, dict):
            parse_throttle_config = {}

        self.nap_server_address = config.get("nap_server_address", "localhost")
        self.nap_server_port = config.get("nap_server_port", 3658)
        self.session_whitelist: List[str] = [str(sid) for sid in config.get("session_whitelist", []) if str(sid).strip()]
        self.delete_time = config.get("delete_time", 60)    
        self.max_video_size = config.get("max_video_size", 200)
        self.bili_quality = bili_config.get("quality", 64)
        self.bili_use_login = bili_config.get("use_login", False)
        self.bili_smart_downgrade = bili_config.get("smart_downgrade", True)
        self._douyin_cookie_from_config = douyin_config.get("cookie", "") or ""
        self._douyin_cookie_from_file = ""
        self._douyin_cookie_loaded = False
        self.douyin_api_url = douyin_config.get("api_url", "")
        self.douyin_max_images = douyin_config.get("max_images", 20)
        self.enable_emoji_reaction = config.get("enable_emoji_reaction", True)
        self.blocked_keywords: List[str] = [str(kw).strip() for kw in parse_throttle_config.get("blocked_keywords", []) if str(kw).strip()]

        self.parse_throttle_window_sec = max(0, int(parse_throttle_config.get("window_sec", 0)))
        self.parse_throttle_max_requests = max(1, int(parse_throttle_config.get("max_requests", 2)))
        self.parse_throttle_cooldown_sec = max(1, int(parse_throttle_config.get("cooldown_sec", 60)))
        self.parse_throttle_block_parallel = parse_throttle_config.get("block_parallel", True)
        self.parse_throttle_min_group_level = max(0, int(parse_throttle_config.get("min_group_level", 0)))
        self.enable_parse_throttle = self.parse_throttle_window_sec > 0
        self.parse_guard = ParseGuard(
            enable=self.enable_parse_throttle,
            window_sec=max(1, self.parse_throttle_window_sec or 1),
            max_requests=self.parse_throttle_max_requests,
            cooldown_sec=self.parse_throttle_cooldown_sec,
            block_parallel=self.parse_throttle_block_parallel,
            logger_obj=logger,
        )
        self._emoji_unsupported_logged_platforms = set()
        self._group_level_unsupported_logged_platforms = set()
        
        # 设置数据目录
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_video_analysis")
        self.download_dir = os.path.join(self.data_dir, "download_videos")
        os.makedirs(self.download_dir, exist_ok=True)
        
        # 初始化 bili_get 模块
        cookie_file = os.path.join(self.data_dir, "bili_cookies.json")
        init_bili_module(cookie_file)
        init_douyin_login(self.data_dir)

    def _build_parse_throttle_key(self, event: AstrMessageEvent):
        """限频作用域固定为群聊成员：group_id + sender_id"""
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        if not group_id or not sender_id:
            return None
        return f"{group_id}:{sender_id}"

    async def _try_acquire_parse_slot(self, event: AstrMessageEvent, platform: str):
        key = self._build_parse_throttle_key(event)
        return await self.parse_guard.acquire(key=key, platform=platform)

    async def _release_parse_slot(self, parse_guard_key):
        await self.parse_guard.release(parse_guard_key)

    def _is_admin_event(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return False


    def _is_session_allowed(self, event: AstrMessageEvent) -> bool:
        if not self.session_whitelist:
            return True
        session_id = event.get_group_id() or event.get_sender_id()
        return str(session_id) in self.session_whitelist

    async def _check_group_level_requirement(self, event: AstrMessageEvent) -> bool:
        return await check_group_level_requirement(
            event=event,
            min_group_level=self.parse_throttle_min_group_level,
            logger_obj=logger,
            unsupported_logged_platforms=self._group_level_unsupported_logged_platforms,
        )

    async def _send_file_if_needed(self, file_path: str) -> str:
        """Helper function to send file through NAP server if needed"""
        if self.nap_server_address != "localhost":
            return await send_file(file_path, HOST=self.nap_server_address, PORT=self.nap_server_port)
        logger.debug(f"检测到本地地址，直接使用文件路径：{file_path}")
        return file_path

    def _create_node(self, event, content):
        """Helper function to create a node with consistent format"""
        return Node(
            uin=event.get_self_id(),
            name="astrbot",
            content=content
        )

    async def _set_emoji(self, event: AstrMessageEvent, emoji_id: int, set_val: bool = True):
        """Helper function to set/unset emoji reaction if enabled"""
        if not self.enable_emoji_reaction:
            return
        if event.get_platform_name() != "aiocqhttp":
            if "non_qq" not in self._emoji_unsupported_logged_platforms:
                self._emoji_unsupported_logged_platforms.add("non_qq")
                logger.debug("当前平台不支持消息表情回应，已自动跳过。")
            return
        try:
            await event.bot.set_msg_emoji_like(
                message_id=event.message_obj.message_id,
                emoji_id=emoji_id,
                set=set_val,
            )
        except Exception as e:
            logger.warning(f"{'设置' if set_val else '取消'}表情回应失败 (emoji_id: {emoji_id}): {e}")
        
    async def _process_and_send(self, event: AstrMessageEvent, result: dict, platform: str):
        """
        统一的消息发送逻辑，处理组件构建、重试、清理。
        目标：如果视频过大，回复文本；否则，只发送视频组件。
        """
        
        file_path_rel = result.get("video_path") if result else None
        media_component = None
        message_to_send = None

        # 0. 检查文件是否存在
        if not (file_path_rel and os.path.exists(file_path_rel)):
            error_msg = result.get("error") if result else None
            
            if error_msg:
                logger.error(f"process_bili_video/douyin_video 失败: {error_msg}")
                if "尚不支持 DASH 格式" in error_msg:
                    user_msg = "抱歉，该视频暂不支持 DASH 格式下载。"
                else:
                    user_msg = f"抱歉，视频处理失败: {error_msg}"
            else:
                logger.error(f"process_bili_video/douyin_video 返回: {result}，文件路径无效或文件不存在: {file_path_rel}")
                user_msg = "抱歉，由于网络或解析问题，无法获取视频文件。"
                
            message_to_send = [Plain(user_msg)]
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 357)
        else:
            file_size_mb = os.path.getsize(file_path_rel) / (1024 * 1024)
            logger.debug(f"文件大小为 {file_size_mb:.2f} MB，最大限制为 {self.max_video_size} MB。")

            # 1. 判断是否超出大小限制
            if file_size_mb > self.max_video_size:
                # 视频过大，不发送视频，只回复文本消息
                message_to_send = [Plain(f"抱歉，该视频文件大小为 {file_size_mb:.2f}MB，超过了 {self.max_video_size}MB 的最大限制，无法发送视频消息。")]
                logger.warning(f"视频大小超出限制，将回复文本消息。")
                await self._set_emoji(event, 424, False)
                await self._set_emoji(event, 357)
            else:
                # 视频在限制内，构建视频组件
                nap_file_path = await self._send_file_if_needed(file_path_rel) 
                
                media_component = Comp.Video.fromFileSystem(path = nap_file_path)
                message_to_send = [media_component]
                logger.debug(f"视频在大小限制内，构建 Video 组件。")
                await self._set_emoji(event, 424, False)
                await self._set_emoji(event, 124)

        
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
                        await self._set_emoji(event, 424, False)
                        await self._set_emoji(event, 357)
                        return
        else:
            # 如果因其他原因导致 message_to_send 为空
            logger.error("未找到有效的文件 or 消息组件，跳过发送。")
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 357)
            return

        # 4. 文件清理
        download_dir_platform = os.path.join(self.download_dir, platform)
        logger.debug(f"发送完成，开始清理 {platform} 旧文件，阈值：{self.delete_time}分钟 (目录: {download_dir_platform})")
        await async_delete_old_files(download_dir_platform, self.delete_time)



    async def _handle_bili_parsing(self, event: AstrMessageEvent, url: str):
        """
        Bilibili 解析与下载核心流程。
        """
        # 清晰度降级映射：当前质量 -> 下一档质量
        DOWNGRADE_MAP = {120: 112, 112: 80, 80: 64, 64: 32, 32: 16, 16: 16}
    
        initial_quality = self.bili_quality
        max_size = self.max_video_size
        use_login = self.bili_use_login
        videos_download = True
    
        result = None
        current_quality = initial_quality
        attempted_qualities = set()
        download_attempts = 0  # 总下载尝试次数
    
        # 步骤 1：预解析视频信息
        bvid_match = REG_BV.search(url)
        av_match = REG_AV.search(url)
        short_url_match = REG_B23.search(url)
    
        video_info = None
        try:
            if short_url_match:
                video_info = await parse_b23(short_url_match.group(0))
            elif bvid_match:
                video_info = await parse_video(bvid_match.group(0))
            elif av_match:
                bvid = av2bv(av_match.group(0))
                video_info = await parse_video(bvid) if bvid else None
        except UnsupportedBiliLinkError:
            return
    
        if not video_info:
            yield event.plain_result("抱歉，无法解析视频信息，无法进行下载。请稍后重试。")
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 357)
            return
    
        await self._set_emoji(event, 424)
    
        duration = video_info.get("duration", 0)
    
        # 步骤 2：智能预估起始清晰度（不使用固定降级次数上限）
        target_quality = initial_quality
        if self.bili_smart_downgrade and duration > 0:
            temp_quality = initial_quality
            while temp_quality >= 16:
                estimated_size_mb = estimate_size(temp_quality, duration)
                if estimated_size_mb <= max_size:
                    break
                next_q = DOWNGRADE_MAP.get(temp_quality)
                if next_q is None or next_q == temp_quality:
                    break
                temp_quality = next_q
            target_quality = temp_quality
            logger.debug(
                f"智能预估：视频时长 {duration}s，初始质量 {initial_quality} 预估降级到 {target_quality}。"
            )
    
        current_quality = target_quality
    
        # 步骤 3：下载 + 后置体积校验循环
        while True:
            if current_quality in attempted_qualities:
                logger.error("已尝试最低清晰度，停止降级重试。")
                if result and result.get("error"):
                    result["error"] = f"{result['error']}（已尝试最低清晰度）"
                else:
                    result = {"error": "已尝试最低清晰度，文件仍不可用"}
                break
    
            attempted_qualities.add(current_quality)
            download_attempts += 1
            logger.debug(f"正在尝试下载 (质量: {current_quality}，总尝试次数: {download_attempts})...")
    
            try:
                result = await process_bili_video(
                    url,
                    download_flag=videos_download,
                    quality=current_quality,
                    use_login=use_login,
                    event=None,
                    download_dir=os.path.join(self.download_dir, "bili"),
                )
            except Exception as e:
                logger.error(f"下载失败（yutto执行异常）: {e}", exc_info=False)
                result = {"error": f"下载失败（yutto执行异常）: {e}"}
                break
    
            file_path_rel = result.get("video_path") if result else None
            if not file_path_rel or not os.path.exists(file_path_rel):
                # 如为 DASH 不支持错误，则不继续降级重试。
                error_msg = result.get("error") if result else None
                if error_msg and "尚不支持 DASH 格式" in error_msg:
                    logger.warning(f"检测到不支持 DASH 格式错误，停止降级重试: {error_msg}")
                else:
                    logger.warning("下载未成功，文件未找到。不进行大小校验，停止降级重试。")
                break
    
            file_size_mb = os.path.getsize(file_path_rel) / (1024 * 1024)
            if file_size_mb <= max_size:
                logger.debug(f"文件大小 {file_size_mb:.2f}MB 满足限制 {max_size}MB。下载成功。")
                break
    
            # 文件超限：若可降级则继续尝试下一档清晰度
            next_quality = DOWNGRADE_MAP.get(current_quality)
            can_downgrade = next_quality is not None and next_quality != current_quality
            if can_downgrade:
                logger.warning(
                    f"后置校验失败！文件实际大小 {file_size_mb:.2f}MB 超出限制 {max_size}MB。删除文件，准备降级重试..."
                )
                try:
                    os.remove(file_path_rel)
                    logger.debug(f"已删除超限文件: {file_path_rel}")
                except Exception as e:
                    logger.error(f"删除超限文件失败: {e}")
                current_quality = next_quality
                continue
    
            # 无法继续降级：保留文件，交给统一发送逻辑返回明确的超限提示
            logger.warning(
                f"后置校验失败！文件实际大小 {file_size_mb:.2f}MB 超出限制 {max_size}MB。已达最低清晰度，保留文件交由后续处理。"
            )
            break
    
        # 步骤 4：统一处理与发送
        async for response in self._process_and_send(event, result, 'bili'):
            yield response
    
    async def _handle_douyin_parsing(self, event: AstrMessageEvent, url: str):
        """
        抖音解析和下载核心逻辑
        """
        download_dir = os.path.join(self.download_dir, "douyin")
        result = None

        for attempt in range(MAX_DOUYIN_PROCESS_RETRIES + 1):
            try:
                logger.debug(f"尝试解析下载 (URL: {url}, 尝试次数: {attempt + 1}/{MAX_DOUYIN_PROCESS_RETRIES + 1})")
                
                cookie, self._douyin_cookie_loaded, self._douyin_cookie_from_file = await get_effective_douyin_cookie(
                    cookie_loaded=self._douyin_cookie_loaded,
                    cookie_from_config=self._douyin_cookie_from_config,
                    cookie_from_file=self._douyin_cookie_from_file,
                    loader=load_douyin_cookies,
                )
                result = await process_douyin_video(url, download_dir=download_dir, api_url=self.douyin_api_url, cookie=cookie, max_images=self.douyin_max_images) 
                
                if not result:
                    if attempt < MAX_DOUYIN_PROCESS_RETRIES: await asyncio.sleep(3); continue
                    else: logger.error("process_douyin_video 连续返回空值，最终失败.")
                
                # 检查是否是多媒体类型（图片或多视频）
                if result.get("type") in ["image", "images", "multi_video"]:
                    has_media = (
                        result.get("image_paths") or 
                        result.get("video_paths") or 
                        result.get("ordered_media")
                    )
                    if has_media:
                        logger.debug(f"第 {attempt + 1} 次尝试成功，获取到 {len(result.get('ordered_media', []) or result.get('image_paths', []) or result.get('video_paths', []))} 个媒体文件。")
                        break
                
                # 检查文件是否存在（单视频）
                if result and result.get("video_path") and os.path.exists(result["video_path"]):
                    logger.debug(f"第 {attempt + 1} 次尝试成功，文件已找到。")
                    break 
                if attempt < MAX_DOUYIN_PROCESS_RETRIES: logger.warning("下载/合成失败，文件未找到。进行重试.")
                
            except Exception as e:
                if attempt < MAX_DOUYIN_PROCESS_RETRIES: logger.error(f"第 {attempt + 1} 次尝试失败，发生异常: {e}. 等待后重试...", exc_info=False)
                else: logger.error(f"第 {attempt + 1} 次尝试失败，发生致命异常: {e}", exc_info=True)
            
            if attempt == MAX_DOUYIN_PROCESS_RETRIES: logger.error(f"核心处理达到最大重试次数 ({MAX_DOUYIN_PROCESS_RETRIES + 1} 次)，最终失败.")
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
            yield event.plain_result(format_douyin_failure_message(result))
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 357)

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
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 357)
            return
        
        if len(ordered_media) == 1:
            item = ordered_media[0]
            media_path = item["path"]
            if not os.path.exists(media_path):
                yield event.plain_result("抱歉，媒体文件不存在。")
                await self._set_emoji(event, 424, False)
                await self._set_emoji(event, 357)
                return
            
            try:
                nap_file_path = await self._send_file_if_needed(media_path)
                if item["type"] == "image":
                    yield event.chain_result([Image.fromFileSystem(path=nap_file_path)])
                else:
                    yield event.chain_result([Comp.Video.fromFileSystem(path=nap_file_path)])
                logger.debug(f"成功直接发送单个媒体文件: {media_path}")
                await self._set_emoji(event, 424, False)
                await self._set_emoji(event, 124)
                return
            except Exception as e:
                logger.error(f"直接发送单个媒体失败: {e}", exc_info=True)
                yield event.plain_result(f"发送失败: {str(e)}")
                await self._set_emoji(event, 424, False)
                await self._set_emoji(event, 357)
                return

        # --- 多个媒体使用合并转发 ---
        if event.get_platform_name() != "aiocqhttp":
            logger.debug("当前平台不支持合并转发，已降级为逐条发送抖音多媒体。")

            success_count = 0
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
                    yield event.chain_result([component])
                    success_count += 1
                except Exception as e:
                    logger.error(f"降级发送第 {idx} 个媒体 ({media_type}) 失败: {e}", exc_info=True)

            if success_count == 0:
                yield event.plain_result("抱歉，当前平台暂不支持该多媒体发送方式。")
                await self._set_emoji(event, 424, False)
                await self._set_emoji(event, 357)
                return

            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 124)
            return

        logger.debug(f"准备发送 {len(ordered_media)} 个媒体文件（保持顺序，合并转发）")
        
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
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 357)
            return
        
        # 发送合并转发消息
        try:
            merged_forward_message = Nodes(nodes=forward_nodes)
            yield event.chain_result([merged_forward_message])
            logger.debug(f"成功发送 {len(forward_nodes)} 个媒体文件（合并转发）")
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 124)
        except Exception as e:
            logger.error(f"发送合并转发消息失败: {e}", exc_info=True)
            yield event.plain_result(f"内容发送失败: {str(e)}")
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 357)

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

    @filter.command("dy_check")
    async def handle_douyin_check(self, event: AstrMessageEvent):
        """
        检查 抖音 Cookie 是否有效
        """
        logger.info("收到抖音 Cookie 检查指令")

        cookie, self._douyin_cookie_loaded, self._douyin_cookie_from_file = await get_effective_douyin_cookie(
            cookie_loaded=self._douyin_cookie_loaded,
            cookie_from_config=self._douyin_cookie_from_config,
            cookie_from_file=self._douyin_cookie_from_file,
            loader=load_douyin_cookies,
        )
        is_valid = await check_douyin_cookie_valid(cookie)

        if is_valid:
            yield event.plain_result("✅ 抖音 Cookie 有效")
        else:
            yield event.plain_result("❌ 抖音 Cookie 无效或不存在，请更新插件配置中的抖音 Cookie")


@filter.event_message_type(EventMessageType.ALL)
async def auto_parse_dispatcher(self: videoAnalysis, event: AstrMessageEvent, *args, **kwargs):
    """
    自动检测消息中是否包含分享链接，并分发给相应的处理器。
    """
    if not self._is_session_allowed(event):
        return

    message_str = event.message_str
    message_obj_str = str(event.message_obj)

    if re.search(r"reply", message_obj_str):
        return

    # --- 1. 检查 Bilibili 链接 ---
    match_bili = re.search(r"(https?://b23\.tv/[\w]+|https?://bili2233\.cn/[\w]+|BV1\w{9}|av\d+)", message_str)
    match_bili_json = re.search(r"https?://(?:b23\.tv|bili2233\.cn)/[a-zA-Z0-9]+", message_obj_str)
    if not match_bili_json:
        match_bili_json = re.search(r"https:\\\\/\\\\/(?:b23\.tv|bili2233\.cn)\\\\/[a-zA-Z0-9]+", message_obj_str)
    
    if match_bili or match_bili_json:
        if contains_blocked_keyword(event, self.blocked_keywords, logger):
            return

        if match_bili:
            url = match_bili.group(1)
        else:
            raw = match_bili_json.group(0)
            url = raw.replace("\\\\", "\\").replace("\\/", "/")

        parse_guard_key = None
        if not self._is_admin_event(event):
            if not await self._check_group_level_requirement(event):
                return
            allowed, parse_guard_key = await self._try_acquire_parse_slot(event, "B站")
            if not allowed:
                return

        try:
            # 调用 Bilibili 处理函数
            async for response in self._handle_bili_parsing(event, url):
                yield response
        finally:
            await self._release_parse_slot(parse_guard_key)
        return
        
    # --- 2. 检查 抖音/TikTok 链接 ---
    # 匹配 v.douyin.com 短链接和文本中的短链接
    match_douyin = re.search(r"(https?://v\.douyin\.com/[a-zA-Z0-9\-\/_]+)", message_str)

    if match_douyin:
        if contains_blocked_keyword(event, self.blocked_keywords, logger):
            return

        # 检查是否配置了 API 地址或 Cookie
        cookie, self._douyin_cookie_loaded, self._douyin_cookie_from_file = await get_effective_douyin_cookie(
            cookie_loaded=self._douyin_cookie_loaded,
            cookie_from_config=self._douyin_cookie_from_config,
            cookie_from_file=self._douyin_cookie_from_file,
            loader=load_douyin_cookies,
        )
        if not self.douyin_api_url and not cookie:
            logger.warning("成功匹配到抖音链接，但 douyin_api_url 和 douyin_cookie 均未配置，跳过解析。")
            return
            
        url = match_douyin.group(1)
        logger.info(f"成功匹配到抖音短链接：{url}")
        
        parse_guard_key = None
        if not self._is_admin_event(event):
            if not await self._check_group_level_requirement(event):
                return
            allowed, parse_guard_key = await self._try_acquire_parse_slot(event, "抖音")
            if not allowed:
                return

        try:
            # 触发开始解析表情回应
            await self._set_emoji(event, 424)

            # 调用抖音处理函数
            async for response in self._handle_douyin_parsing(event, url):
                yield response
        finally:
            await self._release_parse_slot(parse_guard_key)
        return
