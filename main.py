from astrbot.api.message_components import Node, Plain, Image, Nodes
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.core.star.filter.event_message_type import EventMessageType
from astrbot.api import logger
from astrbot.api.star import Star, Context, StarTools
import astrbot.api.message_components as Comp

import re
import os
import asyncio
import httpx
from typing import List
from datetime import datetime

from .modules.bilibili import (
    process_bili_video,
    REG_B23,
    REG_BV,
    REG_AV,
    av2bv,
    parse_b23,
    parse_video,
    estimate_size,
    init_bili_module,
    bili_login,
    check_cookie_valid,
    UnsupportedBiliLinkError,
)
from .modules.douyin import (
    DouyinParser,
    DouyinDownloader,
    init_douyin_login,
    get_effective_douyin_cookie,
    format_douyin_failure_message,
    send_douyin_with_title_forward,
)
from .modules.xiaohongshu import (
    XiaohongshuParser,
    XiaohongshuDownloader,
)
from .modules.tieba import (
    TiebaParser,
    TiebaDownloader,
)
from .modules.nga import (
    NgaParser,
    NgaDownloader,
)
from .modules.auto_delete import delete_old_files
from .modules.parse_guard import (
    ParseGuard,
    check_group_level_requirement,
    contains_blocked_keyword_in_title,
)

MAX_DOUYIN_PROCESS_RETRIES = 1
MAX_SEND_RETRIES = 2


async def async_delete_old_files(folder_path: str, time_threshold_minutes: int) -> int:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, delete_old_files, folder_path, time_threshold_minutes
    )


def _visible_len(text: str) -> int:
    text = re.sub(r"#[^#\s]+(?:\[[^\]]*\])?#?\s*", "", text)
    return len(re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]", "", text))


class videoAnalysis(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        parse_throttle_config = config.get("parse_throttle", {})
        if not isinstance(parse_throttle_config, dict):
            parse_throttle_config = {}
        platform_parse_config = config.get("platform_parse", {}) or {}
        delivery_config = config.get("delivery", {}) or {}

        self.session_whitelist: List[str] = [
            str(sid) for sid in config.get("session_whitelist", []) if str(sid).strip()
        ]
        self.platform_whitelist: List[str] = [
            p.strip().lower()
            for p in platform_parse_config.get(
                "platform_whitelist", ["bilibili", "douyin", "nga", "tieba"]
            )
            if isinstance(p, str) and p.strip()
        ]
        self.enable_emoji_reaction = platform_parse_config.get(
            "enable_emoji_reaction", True
        )
        self.smart_downgrade = platform_parse_config.get("smart_downgrade", True)

        bili_config = platform_parse_config.get("bilibili", {}) or {}
        self.bili_quality = bili_config.get("quality", 64)
        self.bili_use_login = bili_config.get("use_login", False)

        douyin_config = platform_parse_config.get("douyin", {}) or {}
        self._douyin_cookie_from_config = douyin_config.get("cookie", "") or ""
        self._douyin_cookie_from_file = ""
        self._douyin_cookie_loaded = False
        self.douyin_api_url = douyin_config.get("api_url", "")
        xhs_config = platform_parse_config.get("xhs", {}) or {}
        self._xhs_cookie = xhs_config.get("cookie", "") or ""
        self._xhs_image_quality = (
            xhs_config.get("image_quality", "original") or "original"
        )
        self.tieba_sort = platform_parse_config.get("tieba_sort", "time")

        nga_config = platform_parse_config.get("nga", {}) or {}
        self.nga_cookie = nga_config.get("cookie", "")
        self.nga_sort = nga_config.get("sort", "time")

        self.max_video_size = delivery_config.get("max_video_size", 200)
        self.delete_time = delivery_config.get("delete_time", 60)
        self.media_max_images = delivery_config.get("max_images", 20)
        self.media_max_replies = delivery_config.get("max_replies", 20)
        self.text_forward_threshold = delivery_config.get("text_forward_threshold", 50)
        self.max_duration = max(0, int(parse_throttle_config.get("max_duration", 0)))
        self.blocked_keywords: List[str] = [
            str(kw).strip()
            for kw in parse_throttle_config.get("blocked_keywords", [])
            if str(kw).strip()
        ]
        self.admin_bypass_content_restrictions = parse_throttle_config.get(
            "admin_bypass_content_restrictions", True
        )

        self.parse_throttle_window_sec = max(
            0, int(parse_throttle_config.get("window_sec", 0))
        )
        self.parse_throttle_max_requests = max(
            1, int(parse_throttle_config.get("max_requests", 2))
        )
        self.parse_throttle_cooldown_sec = max(
            1, int(parse_throttle_config.get("cooldown_sec", 60))
        )
        self.parse_throttle_block_parallel = parse_throttle_config.get(
            "block_parallel", True
        )
        self.parse_throttle_min_group_level = max(
            0, int(parse_throttle_config.get("min_group_level", 0))
        )
        self.parse_throttle_whitelist: List[str] = [
            str(sid)
            for sid in parse_throttle_config.get("whitelist", [])
            if str(sid).strip()
        ]
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
        self.download_dir = os.path.join(self.data_dir, "downloads")
        os.makedirs(self.download_dir, exist_ok=True)

        # 初始化 bilibili 模块
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
        return file_path

    def _create_node(self, event, content):
        """Helper function to create a node with consistent format"""
        return Node(uin=event.get_self_id(), name="astrbot", content=content)

    async def _set_emoji(
        self, event: AstrMessageEvent, emoji_id: int, set_val: bool = True
    ):
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
            logger.warning(
                f"{'设置' if set_val else '取消'}表情回应失败 (emoji_id: {emoji_id}): {e}"
            )

    async def _check_pre_conditions(
        self,
        event: AstrMessageEvent,
        title: str,
        duration: float = 0,
        is_video: bool = False,
    ) -> bool:
        """
        统一的前置条件检查：屏蔽词 + 时长限制。

        返回 True 表示拦截，返回 False 表示放行。
        """
        # 管理员跳过内容级限制
        if self.admin_bypass_content_restrictions and self._is_admin_event(event):
            return False

        # 拦截规则 1：屏蔽词
        if contains_blocked_keyword_in_title(title, self.blocked_keywords, logger):
            logger.info(f"视频「{title}」命中屏蔽词，拦截解析。")
            await self._set_emoji(event, 123)
            return True

        # 拦截规则 2：时长限制（仅对视频类型生效）
        if is_video and self.max_duration > 0 and duration > self.max_duration:
            logger.info(
                f"视频时长 {duration}s 超出限制 {self.max_duration}s，拦截解析。"
            )
            await self._set_emoji(event, 325)
            return True

        return False

    async def _process_and_send(
        self, event: AstrMessageEvent, result: dict, platform: str
    ):
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
                if not self.enable_emoji_reaction:
                    if "尚不支持 DASH 格式" in error_msg:
                        message_to_send = [
                            Plain("抱歉，该视频暂不支持 DASH 格式下载。")
                        ]
                    else:
                        message_to_send = [Plain(f"抱歉，视频处理失败: {error_msg}")]
            else:
                logger.error(
                    f"process_bili_video/douyin_video 返回: {result}，文件路径无效或文件不存在: {file_path_rel}"
                )
                if not self.enable_emoji_reaction:
                    message_to_send = [
                        Plain("抱歉，由于网络或解析问题，无法获取视频文件。")
                    ]

            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 357)
            return
        else:
            file_size_mb = os.path.getsize(file_path_rel) / (1024 * 1024)
            logger.debug(
                f"文件大小为 {file_size_mb:.2f} MB，最大限制为 {self.max_video_size} MB。"
            )

            # 1. 判断是否超出大小限制
            if (
                not (
                    self.admin_bypass_content_restrictions
                    and self._is_admin_event(event)
                )
                and file_size_mb > self.max_video_size
            ):
                logger.warning(
                    f"视频大小超出限制。文件: {file_path_rel}，大小: {file_size_mb:.2f}MB，最大限制: {self.max_video_size}MB。"
                )
                result["error"] = "video_size_exceeded"
                result["file_size_mb"] = file_size_mb
                if not self.enable_emoji_reaction:
                    message_to_send = [
                        Plain(
                            f"抱歉，视频大小 {file_size_mb:.2f}MB 超出限制 {self.max_video_size}MB，无法发送。"
                        )
                    ]
            else:
                # 视频在限制内，构建视频组件
                nap_file_path = await self._send_file_if_needed(file_path_rel)

                media_component = Comp.Video.fromFileSystem(path=nap_file_path)
                message_to_send = [media_component]
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
                        logger.warning(
                            f"消息发送失败 (第 {send_attempt + 1} 次)，等待 2 秒后重试... 错误: {e}"
                        )
                        await asyncio.sleep(2)
                    else:
                        logger.error(
                            f"消息发送最终失败 ({MAX_SEND_RETRIES + 1} 次重试)。错误: {e}",
                            exc_info=True,
                        )
                        if not self.enable_emoji_reaction:
                            yield event.plain_result("警告：消息发送失败，请稍后重试。")
                        await self._set_emoji(event, 424, False)
                        await self._set_emoji(event, 357)
                        return
        else:
            emoji_id = 357
            if result and result.get("error") == "video_size_exceeded":
                emoji_id = 325
            else:
                logger.warning("未找到有效的文件或消息组件，跳过发送。")
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, emoji_id)
            return

        return

    async def _handle_bili_parsing(self, event: AstrMessageEvent, url: str):
        """
        Bilibili 解析与下载核心流程。
        """
        # 清晰度降级映射：当前质量 -> 下一档质量
        DOWNGRADE_MAP = {120: 112, 112: 80, 80: 64, 64: 32, 32: 16, 16: 16}

        initial_quality = self.bili_quality
        max_size = self.max_video_size
        if self.admin_bypass_content_restrictions and self._is_admin_event(event):
            logger.debug("管理员跳过内容级限制：B站解析跳过智能降级和大小校验")
            max_size = float("inf")
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
            logger.warning("无法解析 Bilibili 视频信息。")
            if not self.enable_emoji_reaction:
                yield event.plain_result(
                    "抱歉，无法解析视频信息，无法进行下载。请稍后重试。"
                )
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 357)
            return

        # 前置条件检查：屏蔽词 + 时长限制
        video_title = video_info.title
        video_duration = video_info.duration
        if await self._check_pre_conditions(
            event, video_title, video_duration, is_video=True
        ):
            return  # 被拦截，直接结束

        # 通过检查，贴上正在解析的表情
        await self._set_emoji(event, 424)

        # 步骤 2：智能预估起始清晰度（不使用固定降级次数上限）
        target_quality = initial_quality
        if self.smart_downgrade and video_duration > 0:
            temp_quality = initial_quality
            while temp_quality >= 16:
                estimated_size_mb = estimate_size(temp_quality, video_duration)
                if estimated_size_mb <= max_size:
                    break
                next_q = DOWNGRADE_MAP.get(temp_quality)
                if next_q is None or next_q == temp_quality:
                    break
                temp_quality = next_q
            target_quality = temp_quality
            logger.debug(
                f"智能预估：视频时长 {video_duration}s，初始质量 {initial_quality} 预估降级到 {target_quality}。"
            )

        current_quality = target_quality

        # 步骤 3：下载 + 后置体积校验循环
        while True:
            if current_quality in attempted_qualities:
                logger.warning("已尝试最低清晰度，停止降级重试。")
                if result and result.get("error"):
                    result["error"] = f"{result['error']}（已尝试最低清晰度）"
                else:
                    result = {"error": "已尝试最低清晰度，文件仍不可用"}
                break

            attempted_qualities.add(current_quality)
            download_attempts += 1
            logger.debug(
                f"正在尝试下载 (质量: {current_quality}，总尝试次数: {download_attempts})..."
            )

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
                logger.warning(f"下载失败（yutto执行异常）: {e}")
                result = {"error": f"下载失败（yutto执行异常）: {e}"}
                break

            file_path_rel = result.get("video_path") if result else None
            if not file_path_rel or not os.path.exists(file_path_rel):
                # 如为 DASH 不支持错误，则不继续降级重试。
                error_msg = result.get("error") if result else None
                if error_msg and "尚不支持 DASH 格式" in error_msg:
                    logger.warning(
                        f"检测到不支持 DASH 格式错误，停止降级重试: {error_msg}"
                    )
                else:
                    logger.warning(
                        "下载未成功，文件未找到。不进行大小校验，停止降级重试。"
                    )
                break

            file_size_mb = os.path.getsize(file_path_rel) / (1024 * 1024)
            if file_size_mb <= max_size:
                logger.debug(
                    f"文件大小 {file_size_mb:.2f}MB 满足限制 {max_size}MB。下载成功。"
                )
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
                    logger.debug(f"已删除超限文件（降级重试）: {file_path_rel}")
                except Exception as e:
                    logger.warning(f"删除超限文件失败: {e}")
                current_quality = next_quality
                continue

            # 无法继续降级：保留文件，交给统一发送逻辑返回明确的超限提示
            logger.warning(
                f"后置校验失败！文件实际大小 {file_size_mb:.2f}MB 超出限制 {max_size}MB。已达最低清晰度，保留文件交由后续处理。"
            )
            break

        # 步骤 4：统一处理与发送
        async for response in self._process_and_send(event, result, "bili"):
            yield response
        await async_delete_old_files(
            os.path.join(self.download_dir, "bili"), self.delete_time
        )

    async def _handle_douyin_parsing(self, event: AstrMessageEvent, url: str):
        """
        抖音解析和下载核心逻辑。
        """
        download_dir = os.path.join(self.download_dir, "douyin")

        # 获取 Cookie（用于本地解析）
        (
            cookie,
            self._douyin_cookie_loaded,
            self._douyin_cookie_from_file,
        ) = await get_effective_douyin_cookie(
            cookie_loaded=self._douyin_cookie_loaded,
            cookie_from_config=self._douyin_cookie_from_config,
            cookie_from_file=self._douyin_cookie_from_file,
        )

        # 步骤 1：解析（获取元数据 + 原始数据）
        parser = DouyinParser(
            cookie=cookie, api_url=self.douyin_api_url, data_dir=self.data_dir
        )
        parse_result = await parser.parse(url)

        if not parse_result.success:
            logger.error(f"抖音解析失败: {parse_result.error}")
            if not self.enable_emoji_reaction:
                yield event.plain_result("抱歉，无法获取视频信息，请稍后重试。")
            await self._set_emoji(event, 357)
            return

        # 步骤 2：前置条件检查
        meta_title = parse_result.title
        meta_duration = parse_result.duration
        is_video = parse_result.media_type == "video"

        if await self._check_pre_conditions(
            event, meta_title, meta_duration, is_video=is_video
        ):
            return  # 被拦截，直接结束

        # 步骤 3：通过检查，贴上正在解析的表情
        await self._set_emoji(event, 424)

        # 步骤 4：开始下载
        result = None
        for attempt in range(MAX_DOUYIN_PROCESS_RETRIES + 1):
            try:
                logger.debug(
                    f"尝试下载 (URL: {url}, 尝试次数: {attempt + 1}/{MAX_DOUYIN_PROCESS_RETRIES + 1})"
                )

                max_size = self.max_video_size
                if self.admin_bypass_content_restrictions and self._is_admin_event(
                    event
                ):
                    logger.debug("管理员跳过内容级限制：抖音解析跳过智能降级和大小校验")
                    max_size = float("inf")

                downloader = DouyinDownloader(
                    download_dir=download_dir,
                    max_images=self.media_max_images,
                    max_size=max_size,
                    smart_downgrade=self.smart_downgrade,
                )
                result = await downloader.download(parse_result, url)

                if result.get("error"):
                    if attempt < MAX_DOUYIN_PROCESS_RETRIES:
                        await asyncio.sleep(3)
                        continue
                    else:
                        logger.warning(f"下载最终失败: {result['error']}")

                if result.get("type") in ["image", "images", "multi_video"]:
                    has_media = (
                        result.get("image_paths")
                        or result.get("video_paths")
                        or result.get("ordered_media")
                    )
                    if has_media:
                        logger.debug(f"第 {attempt + 1} 次尝试成功，获取到媒体文件。")
                        break

                if result.get("video_path") and os.path.exists(result["video_path"]):
                    logger.debug(f"第 {attempt + 1} 次尝试成功，文件已找到。")
                    break

                if attempt < MAX_DOUYIN_PROCESS_RETRIES:
                    logger.warning("下载失败，文件未找到。进行重试。")
                    await asyncio.sleep(3)

            except Exception as e:
                if attempt < MAX_DOUYIN_PROCESS_RETRIES:
                    logger.warning(
                        f"第 {attempt + 1} 次尝试失败，发生异常: {e}. 等待后重试..."
                    )
                else:
                    logger.error(
                        f"第 {attempt + 1} 次尝试失败，发生致命异常: {e}", exc_info=True
                    )

            if attempt == MAX_DOUYIN_PROCESS_RETRIES:
                logger.warning(
                    f"核心处理达到最大重试次数 ({MAX_DOUYIN_PROCESS_RETRIES + 1} 次)，最终失败。"
                )
            await asyncio.sleep(2)

        # 检查结果的有效性
        if isinstance(result, dict) and result.get("error"):
            logger.warning(f"抖音解析失败或结果无效: {result['error']}")
            if not self.enable_emoji_reaction:
                yield event.plain_result(format_douyin_failure_message(result))
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 357)
            return

        # 步骤 5：检查是否需要将标题作为文章合并转发
        if (
            self.text_forward_threshold > 0
            and _visible_len(meta_title) > self.text_forward_threshold
        ):
            async for response in send_douyin_with_title_forward(
                event,
                meta_title,
                result,
                set_emoji_fn=lambda emoji_id, set_val=True: self._set_emoji(
                    event, emoji_id, set_val
                ),
            ):
                yield response
            return

        # 处理多媒体类型
        result_type = result.get("type", "")
        if result_type in ["image", "images", "multi_video"]:
            async for response in self._send_douyin_multimedia(event, result):
                yield response
            return

        # 处理单视频类型
        elif result.get("video_path") and os.path.exists(result["video_path"]):
            async for response in self._process_and_send(event, result, "douyin"):
                yield response

        # 处理失败情况
        else:
            logger.warning("抖音解析失败或结果无效。")
            if not self.enable_emoji_reaction:
                yield event.plain_result(format_douyin_failure_message(result))
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 357)
            return

        # 统一清理文件
        download_dir_douyin = os.path.join(self.download_dir, "douyin")
        await async_delete_old_files(download_dir_douyin, self.delete_time)

    async def _handle_xhs_parsing(self, event: AstrMessageEvent, url: str):
        """小红书解析和下载核心逻辑"""
        download_dir = os.path.join(self.download_dir, "xhs")

        parser = XiaohongshuParser(
            cookie=self._xhs_cookie,
            prefer_original=(self._xhs_image_quality == "original"),
        )
        parse_result = await parser.parse(url)

        if not parse_result.success:
            logger.error(f"小红书解析失败: {parse_result.error}")
            if not self.enable_emoji_reaction:
                yield event.plain_result("抱歉，无法获取笔记信息，请稍后重试。")
            await self._set_emoji(event, 357)
            return

        meta_title = parse_result.title or parse_result.desc
        meta_duration = parse_result.duration
        is_video = parse_result.media_type == "video"
        if await self._check_pre_conditions(
            event, meta_title, meta_duration, is_video=is_video
        ):
            return

        await self._set_emoji(event, 424)

        downloader = XiaohongshuDownloader(
            download_dir=download_dir,
            max_images=self.media_max_images,
        )
        result = await downloader.download(parse_result, url)

        if result.get("error"):
            logger.warning(f"NGA 下载失败: {result['error']}")
            if not self.enable_emoji_reaction:
                yield event.plain_result("抱歉，无法下载内容。")
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 357)
            return

        meta_title = result.get("title", "") or meta_title
        meta_desc = parse_result.desc
        has_title = parse_result.has_title

        # 全文（标题 + 正文）过长时使用合并转发
        # 有真正标题时用「」包裹，正文空行分隔
        meta_text = ""
        if has_title:
            meta_text = f"「{meta_title}」"
        if meta_desc:
            if meta_text:
                meta_text += "\n\u200b\n" + meta_desc
            else:
                meta_text = meta_desc
        if not meta_text:
            meta_text = meta_title or ""
        if (
            self.text_forward_threshold > 0
            and _visible_len(meta_text) > self.text_forward_threshold
        ):
            async for response in send_douyin_with_title_forward(
                event,
                meta_text,
                result,
                set_emoji_fn=lambda emoji_id, set_val=True: self._set_emoji(
                    event, emoji_id, set_val
                ),
                text_sender_name="小红书正文",
                media_sender_name="小红书内容",
            ):
                yield response
            await async_delete_old_files(download_dir, self.delete_time)
            return

        # 单视频走已有发送逻辑
        if result.get("type") == "video":
            async for response in self._process_and_send(event, result, "xhs"):
                yield response
        else:
            # 多媒体（多图 / 图文混合）复用多媒体发送
            async for response in self._send_douyin_multimedia(
                event, result, "小红书内容"
            ):
                yield response

        await async_delete_old_files(download_dir, self.delete_time)

    async def _handle_tieba_parsing(self, event: AstrMessageEvent, url: str):
        """贴吧解析和下载核心逻辑"""
        if "m.q.qq.com" in url:
            try:
                async with httpx.AsyncClient(timeout=10, follow_redirects=True) as cli:
                    resp = await cli.get(url)
                    url = str(resp.url)
                logger.info(f"贴吧 QQ 小程序重定向至：{url}")
            except Exception as e:
                logger.warning(f"贴吧 QQ 小程序重定向失败: {e}")
            # m.q.qq.com 短链需 QQ 登录态才能解析，外部 Bot 无法获取真实帖吧 URL
            if "m.q.qq.com" in url:
                logger.warning("贴吧 QQ 小程序短链解析失败，跳过")
                if not self.enable_emoji_reaction:
                    yield event.plain_result(
                        "该贴吧分享链接来自QQ小程序，Bot 无法解析，请在QQ内直接打开。"
                    )
                return

        download_dir = os.path.join(self.download_dir, "tieba")

        parser = TiebaParser(max_replies=self.media_max_replies, sort=self.tieba_sort)
        parse_result = await parser.parse(url)

        if not parse_result.success:
            logger.warning(f"贴吧解析失败: {parse_result.error}")
            if not self.enable_emoji_reaction:
                yield event.plain_result("抱歉，无法获取帖子信息，请稍后重试。")
            await self._set_emoji(event, 357)
            return

        meta_title = parse_result.title
        if await self._check_pre_conditions(event, meta_title, 0, is_video=False):
            return

        await self._set_emoji(event, 424)

        downloader = TiebaDownloader(
            download_dir=download_dir,
            max_images=self.media_max_images,
        )
        result = await downloader.download(parse_result, url)

        if result.get("error"):
            logger.warning(f"小红书下载失败: {result['error']}")
            if not self.enable_emoji_reaction:
                yield event.plain_result("抱歉，无法下载内容。")
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 357)
            return

        sender_id = event.get_self_id()

        op_time = (
            datetime.fromtimestamp(parse_result.create_time).strftime("%Y-%m-%d %H:%M")
            if parse_result.create_time
            else ""
        )
        forum_display = parse_result.forum_name + "吧"
        op_header = (
            f"【{forum_display}】 {parse_result.author}"
            if parse_result.forum_name
            else parse_result.author
        )
        if op_time:
            op_header += f" | {op_time}"
        op_header += "："
        title = parse_result.title
        content = parse_result.content
        title_is_auto = bool(content) and content.startswith(title) and len(title) >= 15
        if title_is_auto:
            op_text = f"{op_header}"
            if content:
                op_text += f"\n\u200b\n{content}"
        else:
            op_text = f"{op_header}\n\u200b\n「{title}」"
            if content:
                op_text += f"\n\u200b\n{content}"
        op_agree = result.get("agree_num", 0)
        if op_agree > 0:
            op_text += f"\n\u200b\n\u200f👍{op_agree}"

        nodes: list = []
        op_contents = [Plain(op_text)]
        ordered_media = result.get("ordered_media", [])
        for item in ordered_media:
            media_path = item["path"]
            if not os.path.exists(media_path):
                continue
            try:
                if item["type"] == "image":
                    op_contents.append(Image.fromFileSystem(path=media_path))
                else:
                    op_contents.append(Comp.Video.fromFileSystem(path=media_path))
            except Exception as e:
                logger.warning(f"贴吧媒体处理出错: {e}")
        nodes.append(Node(uin=sender_id, name="贴吧内容", content=op_contents))

        if result.get("video_path") and os.path.exists(result["video_path"]):
            try:
                component = Comp.Video.fromFileSystem(path=result["video_path"])
                nodes.append(Node(uin=sender_id, name="贴吧内容", content=[component]))
            except Exception as e:
                logger.warning(f"贴吧处理视频文件出错: {e}")

        for reply in result.get("replies", []):
            agree = reply.get("agree_num", 0)
            if agree > 0:
                reply_text = f"#{reply['floor']} {reply['author']}：\n{reply['content']}\n\u200b\n\u200f👍{agree}"
            else:
                reply_text = (
                    f"#{reply['floor']} {reply['author']}：\n{reply['content']}"
                )
            reply_contents = [Plain(reply_text)]
            for media in reply.get("media", []):
                media_path = media["path"]
                if not os.path.exists(media_path):
                    continue
                try:
                    if media["type"] == "image":
                        reply_contents.append(Image.fromFileSystem(path=media_path))
                    else:
                        reply_contents.append(
                            Comp.Video.fromFileSystem(path=media_path)
                        )
                except Exception as e:
                    logger.warning(f"贴吧回复媒体处理出错: {e}")
            reply_node = Node(
                uin=sender_id,
                name="贴吧内容",
                content=reply_contents,
            )
            nodes.append(reply_node)

        if len(nodes) <= 1:
            logger.warning("贴吧：没有构建出任何消息节点")
            if not self.enable_emoji_reaction:
                yield event.plain_result("抱歉，无法构建消息内容。")
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 357)
            return

        if event.get_platform_name() == "aiocqhttp":
            try:
                merged = Nodes(nodes=nodes)
                yield event.chain_result([merged])
                logger.debug(f"贴吧合并转发成功：{len(nodes)} 个节点")
            except Exception as e:
                logger.warning(f"贴吧合并转发失败: {e}")
                for node in nodes:
                    yield event.chain_result(node.content)
        else:
            logger.debug("当前平台不支持合并转发，贴吧降级为依次发送")
            for node in nodes:
                yield event.chain_result(node.content)

        await self._set_emoji(event, 424, False)
        await self._set_emoji(event, 124)
        await async_delete_old_files(download_dir, self.delete_time)

    async def _handle_nga_parsing(self, event: AstrMessageEvent, url: str):
        url = re.sub(r"https?://[^/]+", "https://bbs.nga.cn", url)
        download_dir = os.path.join(self.download_dir, "nga")
        parser = NgaParser(max_replies=self.media_max_replies, sort=self.nga_sort)
        parser.cookie = self.nga_cookie
        nga_uid, nga_token = "", ""
        if self.nga_cookie:
            for part in self.nga_cookie.split(";"):
                part = part.strip()
                if part.startswith("ngaPassportUid="):
                    nga_uid = part.split("=", 1)[1]
                elif part.startswith("ngaPassportCid="):
                    nga_token = part.split("=", 1)[1]
            if nga_uid and nga_token:
                parser.access_uid = nga_uid
                parser.access_token = nga_token

        parse_result = await parser.parse(url)
        if not parse_result.success:
            hint = ""
            if not nga_uid or not nga_token:
                hint = "（可能该帖子需要登录才能查看，请在NGA配置中填入Cookie）"
            logger.warning(f"NGA 解析失败: {parse_result.error}{hint}")
            if not self.enable_emoji_reaction:
                yield event.plain_result(f"NGA: {parse_result.error}{hint}")
            await self._set_emoji(event, 357)
            return

        meta_title = parse_result.title
        if await self._check_pre_conditions(event, meta_title, 0, is_video=False):
            return
        await self._set_emoji(event, 424)

        downloader = NgaDownloader(
            download_dir=download_dir, max_images=self.media_max_images
        )
        result = await downloader.download(parse_result)
        if result.get("error"):
            logger.warning(f"贴吧下载失败: {result['error']}")
            if not self.enable_emoji_reaction:
                yield event.plain_result("抱歉，无法下载内容。")
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 357)
            return

        sender_id = event.get_self_id()

        op_time = ""
        if parse_result.create_time:
            op_time = datetime.fromtimestamp(parse_result.create_time).strftime(
                "%Y-%m-%d %H:%M"
            )
        header = f"【{parse_result.forum_name}】 {parse_result.author}"
        if op_time:
            header += f" | {op_time}"
        header += "："
        op_text = f"{header}\n\u200b\n「{parse_result.title}」"
        if parse_result.content:
            op_text += f"\n\u200b\n{parse_result.content}"
        op_score = result.get("op_score", 0)
        if op_score > 0:
            op_text += f"\n\u200b\n\u200f👍{op_score}"

        nodes: list = []
        op_contents = [Plain(op_text)]
        for item in result.get("ordered_media", []):
            media_path = item["path"]
            if not os.path.exists(media_path):
                continue
            try:
                if item["type"] == "image":
                    op_contents.append(Image.fromFileSystem(path=media_path))
                else:
                    op_contents.append(Comp.Video.fromFileSystem(path=media_path))
            except Exception as e:
                logger.warning(f"NGA 媒体处理出错: {e}")
        nodes.append(Node(uin=sender_id, name="NGA内容", content=op_contents))

        for reply in result.get("replies", []):
            score = reply.get("score", 0)
            reply_text = f"#{reply['floor']} {reply['author']}："
            reply_text += f"\n{reply['content']}"
            pid_map = result.get("pid_map", {})
            for rpid in reply.get("reply_to_pids", []):
                if rpid not in pid_map:
                    continue
                orig_floor, orig_author, orig_content = pid_map[rpid]
                excerpt = orig_content[:120] + (
                    "..." if len(orig_content) > 120 else ""
                )
                reply_text += "\n────"
                if orig_author:
                    reply_text += f"\n#{orig_floor} {orig_author}：\n{excerpt}"
                else:
                    reply_text += f"\n{excerpt}"
                reply_text += "\n────"
            if score > 0:
                reply_text += f"\n\u200b\n\u200f👍{score}"
            reply_contents = [Plain(reply_text)]
            for media in reply.get("media", []):
                media_path = media["path"]
                if not os.path.exists(media_path):
                    continue
                try:
                    if media["type"] == "image":
                        reply_contents.append(Image.fromFileSystem(path=media_path))
                    else:
                        reply_contents.append(
                            Comp.Video.fromFileSystem(path=media_path)
                        )
                except Exception as e:
                    logger.warning(f"NGA 回复媒体处理出错: {e}")
            nodes.append(Node(uin=sender_id, name="NGA内容", content=reply_contents))

        if len(nodes) <= 1:
            logger.warning("NGA：没有构建出任何消息节点")

        if event.get_platform_name() == "aiocqhttp":
            try:
                merged = Nodes(nodes=nodes)
                yield event.chain_result([merged])
            except Exception as e:
                logger.warning(f"NGA 合并转发失败: {e}")
                for node in nodes:
                    yield event.chain_result(node.content)
        else:
            logger.debug("当前平台不支持合并转发，NGA 降级为依次发送")
            for node in nodes:
                yield event.chain_result(node.content)

        await self._set_emoji(event, 424, False)
        await self._set_emoji(event, 124)
        await async_delete_old_files(download_dir, self.delete_time)

    async def _send_douyin_multimedia(
        self, event: AstrMessageEvent, result: dict, sender_name: str = "抖音内容"
    ):
        """发送抖音多媒体（单个直接发送，多个使用合并转发，保持原始顺序）"""
        ordered_media = result.get("ordered_media", [])

        # 兼容旧版本或 API 返回的格式
        if not ordered_media:
            image_paths = result.get("image_paths", [])
            video_paths = result.get("video_paths", [])
            for p in image_paths:
                ordered_media.append({"path": p, "type": "image"})
            for p in video_paths:
                ordered_media.append({"path": p, "type": "video"})

        if not ordered_media:
            logger.warning("没有找到媒体文件")
            if not self.enable_emoji_reaction:
                yield event.plain_result("抱歉，没有找到媒体文件。")
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 357)
            return

        if len(ordered_media) == 1:
            item = ordered_media[0]
            media_path = item["path"]
            if not os.path.exists(media_path):
                logger.warning(f"媒体文件不存在: {media_path}")
                if not self.enable_emoji_reaction:
                    yield event.plain_result("抱歉，媒体文件不存在。")
                await self._set_emoji(event, 424, False)
                await self._set_emoji(event, 357)
                return

            try:
                nap_file_path = await self._send_file_if_needed(media_path)
                if item["type"] == "image":
                    yield event.chain_result([Image.fromFileSystem(path=nap_file_path)])
                else:
                    yield event.chain_result(
                        [Comp.Video.fromFileSystem(path=nap_file_path)]
                    )
                logger.debug(f"成功直接发送单个媒体文件: {media_path}")
                await self._set_emoji(event, 424, False)
                await self._set_emoji(event, 124)
                return
            except Exception as e:
                logger.warning(f"直接发送单个媒体失败: {e}")
                if not self.enable_emoji_reaction:
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
                    logger.warning(f"降级发送第 {idx} 个媒体 ({media_type}) 失败: {e}")

            if success_count == 0:
                logger.warning("降级逐条发送全部失败")
                if not self.enable_emoji_reaction:
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

                forward_nodes.append(
                    Node(uin=sender_id, name=sender_name, content=[component])
                )
            except Exception as e:
                logger.warning(f"处理第 {idx} 个媒体 ({media_type}) 时出错: {e}")

        if len(forward_nodes) == 0:
            logger.warning("无法加载媒体文件（合并转发构建为空）")
            if not self.enable_emoji_reaction:
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
            if not self.enable_emoji_reaction:
                yield event.plain_result(f"内容发送失败: {str(e)}")
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 357)

    @filter.command("bili_login")
    async def handle_bili_login(self, event: AstrMessageEvent):
        """
        发送 B站登录二维码，等待用户扫码登录
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

            qr_image_data = base64.b64decode(qr_data["image_base64"])

            # 保存临时文件
            temp_image_path = os.path.join(self.data_dir, "bili_login_qrcode.png")
            with open(temp_image_path, "wb") as f:
                f.write(qr_image_data)

            # 发送图片和提示信息
            yield event.chain_result(
                [
                    Plain("请使用 B站APP 扫描以下二维码登录：\n"),
                    Image.fromFileSystem(temp_image_path),
                    Plain("\n等待登录中...（最多40秒）"),
                ]
            )

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
            yield event.plain_result(
                "❌ B站 Cookie 无效或不存在，请使用 /bili_login 登录"
            )


@filter.event_message_type(EventMessageType.ALL)
async def auto_parse_dispatcher(
    self: videoAnalysis, event: AstrMessageEvent, *args, **kwargs
):
    """
    自动检测消息中是否包含分享链接，并分发给相应的解析器
    """
    if not self._is_session_allowed(event):
        return

    _enabled_platforms = set(self.platform_whitelist)
    if not _enabled_platforms:
        return

    message_str = event.message_str
    message_obj_str = str(event.message_obj)

    if re.search(r"reply", message_obj_str):
        return

    # 解析限制白名单检查
    _throttle_whitelisted = False
    if self.parse_throttle_whitelist:
        _gid = event.get_group_id() or ""
        _sid = event.get_sender_id() or ""
        _throttle_whitelisted = (
            _gid in self.parse_throttle_whitelist
            or _sid in self.parse_throttle_whitelist
        )

    # --- 1. 检查 Bilibili 链接 ---
    if "bilibili" in _enabled_platforms:
        match_bili = re.search(
            r"(https?://b23\.tv/[\w]+|https?://bili2233\.cn/[\w]+|BV1\w{9}|av\d+)",
            message_str,
        )
        match_bili_json = re.search(
            r"https?://(?:b23\.tv|bili2233\.cn)/[a-zA-Z0-9]+", message_obj_str
        )
        if not match_bili_json:
            match_bili_json = re.search(
                r"https:\\\\/\\\\/(?:b23\.tv|bili2233\.cn)\\\\/[a-zA-Z0-9]+",
                message_obj_str,
            )

        if match_bili or match_bili_json:
            if match_bili:
                url = match_bili.group(1)
            else:
                raw = match_bili_json.group(0)
                url = raw.replace("\\\\", "\\").replace("\\/", "/")

            parse_guard_key = None
            if not _throttle_whitelisted and not self._is_admin_event(event):
                if not await self._check_group_level_requirement(event):
                    await self._set_emoji(event, 179)
                    return
                allowed, parse_guard_key = await self._try_acquire_parse_slot(
                    event, "B站"
                )
                if not allowed:
                    return

            try:
                # 调用 Bilibili 处理函数
                async for response in self._handle_bili_parsing(event, url):
                    yield response
            finally:
                await self._release_parse_slot(parse_guard_key)
            return

    # --- 2. 检查 NGA 链接 ---
    if "nga" in _enabled_platforms:
        match_nga = re.search(
            r"(https?://(?:bbs\.nga\.cn|nga\.178\.com|ngabbs\.com)/read\.php\?tid=\d+)",
            message_str,
        )
        match_nga_json = None
        if not match_nga:
            match_nga_json = re.search(
                r"(https?://(?:bbs\.nga\.cn|nga\.178\.com|ngabbs\.com)/read\.php\?tid=\d+)",
                message_obj_str,
            )
        if not match_nga and not match_nga_json:
            match_nga_json = re.search(
                r"https:\\\\/\\\\/(?:bbs\.nga\.cn|nga\.178\.com|ngabbs\.com)\\\\/read\.php\?tid=\d+",
                message_obj_str,
            )
        if match_nga or match_nga_json:
            url = (
                match_nga.group(1)
                if match_nga
                else match_nga_json.group(0).replace("\\", "")
            )
            logger.info(f"成功匹配到 NGA 链接：{url}")
            parse_guard_key = None
            if not _throttle_whitelisted and not self._is_admin_event(event):
                if not await self._check_group_level_requirement(event):
                    await self._set_emoji(event, 179)
                    return
                allowed, parse_guard_key = await self._try_acquire_parse_slot(
                    event, "NGA"
                )
                if not allowed:
                    return
            try:
                async for response in self._handle_nga_parsing(event, url):
                    yield response
            finally:
                await self._release_parse_slot(parse_guard_key)
            return

    # --- 4. 检查 贴吧 链接 ---
    if "tieba" in _enabled_platforms:
        match_tieba = re.search(r"((?:https?://)?tieba\.baidu\.com/p/\d+)", message_str)

        match_tieba_json = None
        if not match_tieba:
            match_tieba_json = re.search(
                r"(?:https?://)?tieba\.baidu\.com/p/\d+", message_obj_str
            )
        if not match_tieba and not match_tieba_json:
            match_tieba_json = re.search(
                r"(?:https?:)?\\\\/\\\\/tieba\.baidu\.com\\\\/p\\\\/\d+",
                message_obj_str,
            )
        if not match_tieba and not match_tieba_json:
            m = re.search(r"m\.q\.qq\.com/a/s/\w+", message_obj_str)
            if m and "贴吧" in message_obj_str:
                match_tieba_json = m

        if match_tieba or match_tieba_json:
            if match_tieba:
                url = match_tieba.group(1)
            else:
                raw = match_tieba_json.group(0)
                url = raw.replace("\\\\", "\\").replace("\\/", "/")
            if not url.startswith("http"):
                url = "https://" + url

            logger.info(f"成功匹配到贴吧链接：{url}")

            parse_guard_key = None
            if not _throttle_whitelisted and not self._is_admin_event(event):
                if not await self._check_group_level_requirement(event):
                    await self._set_emoji(event, 179)
                    return
                allowed, parse_guard_key = await self._try_acquire_parse_slot(
                    event, "贴吧"
                )
                if not allowed:
                    return

            try:
                async for response in self._handle_tieba_parsing(event, url):
                    yield response
            finally:
                await self._release_parse_slot(parse_guard_key)
            return

    # --- 3. 检查 小红书 链接 ---
    if "xiaohongshu" in _enabled_platforms:
        _XHS_RE = r"[a-zA-Z0-9\-_/]+(?:\?[^\s<>\"'()]*)?"
        match_xhs = re.search(
            r"(https?://xhslink\.com/" + _XHS_RE + r")", message_str
        ) or re.search(
            r"(https?://(?:www\.)?(?:xiaohongshu|rednote)\.com/(?:explore|discovery/item)/"
            + _XHS_RE
            + r")",
            message_str,
        )
        match_xhs_json = None
        if not match_xhs:
            _XHS_RE_ESC = r"[a-zA-Z0-9\-_\\\\/]+(?:\?[^\s<>\"'()\\\\]*)?"
            match_xhs_json = re.search(
                r"https?://xhslink\.com/" + _XHS_RE_ESC, message_obj_str
            ) or re.search(
                r"https?://(?:www\.)?(?:xiaohongshu|rednote)\.com/(?:explore|discovery/item)/"
                + _XHS_RE_ESC,
                message_obj_str,
            )
        if not match_xhs and not match_xhs_json:
            match_xhs_json = re.search(
                r"https?:\\\\/\\\\/xhslink\.com\\\\/" + _XHS_RE_ESC, message_obj_str
            ) or re.search(
                r"https?:\\\\/\\\\/(?:www\.)?(?:xiaohongshu|rednote)\\.com\\\\/(?:explore|discovery\\\\/item)\\\\/"
                + _XHS_RE_ESC,
                message_obj_str,
            )

        if match_xhs or match_xhs_json:
            if match_xhs:
                url = match_xhs.group(1)
            else:
                raw = match_xhs_json.group(0)
                url = raw.replace("\\\\", "\\").replace("\\/", "/")
            logger.info(f"成功匹配到小红书链接：{url}")

            parse_guard_key = None
            if not _throttle_whitelisted and not self._is_admin_event(event):
                if not await self._check_group_level_requirement(event):
                    await self._set_emoji(event, 179)
                    return
                allowed, parse_guard_key = await self._try_acquire_parse_slot(
                    event, "小红书"
                )
                if not allowed:
                    return

            try:
                async for response in self._handle_xhs_parsing(event, url):
                    yield response
            finally:
                await self._release_parse_slot(parse_guard_key)
            return

    # --- 2. 检查 抖音 链接 ---
    if "douyin" in _enabled_platforms:
        match_douyin = (
            re.search(r"(https?://v\.douyin\.com/[a-zA-Z0-9\-\/_]+)", message_str)
            or re.search(
                r"(https?://(?:www\.)?douyin\.com/(?:video|note|slides)/\d+)",
                message_str,
            )
            or re.search(
                r"(https?://(?:www\.)?iesdouyin\.com/share/(?:video|note|slides)/\d+)",
                message_str,
            )
            or re.search(
                r"(https?://(?:www\.)?douyin\.com/(?:discover|user)/?\S*modal_id=\d+)",
                message_str,
            )
        )

        if match_douyin:
            (
                cookie,
                self._douyin_cookie_loaded,
                self._douyin_cookie_from_file,
            ) = await get_effective_douyin_cookie(
                cookie_loaded=self._douyin_cookie_loaded,
                cookie_from_config=self._douyin_cookie_from_config,
                cookie_from_file=self._douyin_cookie_from_file,
            )

            url = match_douyin.group(1)
            logger.info(f"成功匹配到抖音链接：{url}")

            parse_guard_key = None
            if not _throttle_whitelisted and not self._is_admin_event(event):
                if not await self._check_group_level_requirement(event):
                    await self._set_emoji(event, 179)
                    return
                allowed, parse_guard_key = await self._try_acquire_parse_slot(
                    event, "抖音"
                )
                if not allowed:
                    return

            try:
                # 调用抖音处理函数
                async for response in self._handle_douyin_parsing(event, url):
                    yield response
            finally:
                await self._release_parse_slot(parse_guard_key)
            return
