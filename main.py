from astrbot.api.message_components import Node, Plain, Image, Nodes
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.core.star.filter.event_message_type import EventMessageType
from astrbot.api import logger
from astrbot.api.star import Star, Context, StarTools
import astrbot.api.message_components as Comp

import re
import os
import json
import asyncio
import time
import httpx
from typing import Any, List, Optional
from datetime import datetime

from .modules.download_io import DownloadLimitExceeded, SizePrecheckExceeded
from .modules.send_utils import (
    deliver_items,
    finish_delivery_report,
    send_chain_with_retry,
    send_plain,
)
from .modules.auto_delete import (
    delete_old_files,
    ensure_owned_dir,
    get_file_use_guard,
)

from .modules.bilibili import (
    process_bili_video,
    REG_B23,
    REG_BV,
    REG_AV,
    REG_BILI_LIVE,
    REG_BILI_DYNAMIC,
    REG_BILI_SPACE,
    parse_av,
    parse_b23,
    parse_video,
    estimate_size_with_plan,
    probe_quality_plan,
    init_bili_module,
    bili_login,
    check_cookie_valid_detailed,
    cancel_bili_login_tasks,
    LoginOutcome,
    UnsupportedBiliLinkError,
    _best_qn,
    _same_tier,
)
from .modules.douyin import (
    DouyinParser,
    DouyinDownloader,
    init_douyin_login,
    get_effective_douyin_cookie,
    format_douyin_failure_message,
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
from .modules.media_cache import (
    MediaCache,
    build_xhs_meta_text,
    forward_long_text,
    lookup_cached_media,
    media_files_from_result,
    send_cached_result,
    visible_len,
)
from .modules.parse_guard import (
    ParseGuard,
    check_group_level_requirement,
    contains_blocked_keyword_in_title,
)

MAX_DOUYIN_PROCESS_RETRIES = 1
MAX_SEND_RETRIES = 2


async def async_delete_old_files(
    folder_path: str, time_threshold_minutes: int, guard=None
) -> list[str]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, delete_old_files, folder_path, time_threshold_minutes, guard
    )


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
                "platform_whitelist",
                ["bilibili", "douyin", "xiaohongshu", "nga", "tieba"],
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
        self.media_cache = MediaCache(self)
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
        # 标记平台目录归属，供清理识别本插件文件
        for _platform in ("bilibili", "douyin", "xhs", "tieba", "nga"):
            ensure_owned_dir(os.path.join(self.download_dir, _platform))
        self._file_guard = get_file_use_guard()

        # 初始化 bilibili 模块
        cookie_file = os.path.join(self.data_dir, "bili_cookies.json")
        init_bili_module(cookie_file)
        init_douyin_login(self.data_dir)

    async def terminate(self) -> None:
        """插件卸载时取消并回收登录任务。"""
        try:
            await cancel_bili_login_tasks()
        except Exception as e:
            logger.warning(f"取消 B站登录任务失败: {e}")

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
            retcode = getattr(e, "retcode", None)
            if not set_val and (retcode == 1200 or "未设置过该表情" in str(e)):
                return
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
    ) -> None:
        """统一发送逻辑：await 实际发送后再记录成功或重试。"""
        file_path_rel = result.get("video_path") if result else None
        message_to_send = None

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
            if message_to_send:
                await send_chain_with_retry(
                    event,
                    message_to_send,
                    attempts=1,
                    delay=0,
                    label="错误提示",
                    logger_obj=logger,
                )
            return

        file_size_mb = os.path.getsize(file_path_rel) / (1024 * 1024)
        size_limit_bypassed = (
            self.admin_bypass_content_restrictions and self._is_admin_event(event)
        )
        if not size_limit_bypassed and file_size_mb > self.max_video_size:
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
            if size_limit_bypassed:
                logger.debug(
                    f"下载完成：文件大小={file_size_mb:.2f}MB，已由管理员跳过 {self.max_video_size}MB 限制。"
                )
            else:
                logger.debug(
                    f"下载完成：文件大小={file_size_mb:.2f}MB，限制={self.max_video_size}MB，大小校验通过。"
                )
            nap_file_path = await self._send_file_if_needed(file_path_rel)
            message_to_send = [Comp.Video.fromFileSystem(path=nap_file_path)]
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 124)

        if not message_to_send:
            emoji_id = 357
            if result and result.get("error") == "video_size_exceeded":
                emoji_id = 325
            else:
                logger.warning("未找到有效的文件或消息组件，跳过发送。")
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, emoji_id)
            return

        ok = await send_chain_with_retry(
            event,
            message_to_send,
            attempts=MAX_SEND_RETRIES + 1,
            delay=2,
            label="消息",
            logger_obj=logger,
        )
        if ok:
            return
        if not self.enable_emoji_reaction:
            await send_plain(
                event,
                "警告：消息发送失败，请稍后重试。",
                attempts=1,
                delay=0,
                label="发送失败提示",
                logger_obj=logger,
            )
        await self._set_emoji(event, 424, False)
        await self._set_emoji(event, 357)

    async def _cleanup_media(self, download_dir: str) -> None:
        """只清理本插件拥有且未被使用的过期文件，并联动清理缓存条目。"""
        ensure_owned_dir(download_dir)
        deleted = await async_delete_old_files(
            download_dir, self.delete_time, self._file_guard
        )
        if deleted:
            await self.media_cache.remove_paths(deleted)
        await self.media_cache.cleanup(self.delete_time * 60)

    async def _handle_bili_parsing(self, event: AstrMessageEvent, url: str):
        """
        Bilibili 解析与下载核心流程。
        """
        for pattern, label in [
            (REG_BILI_LIVE, "直播间"),
            (REG_BILI_DYNAMIC, "动态"),
            (REG_BILI_SPACE, "个人空间"),
        ]:
            if pattern.search(url):
                if not self.enable_emoji_reaction:
                    yield event.plain_result(
                        f"该链接为 Bilibili {label}，当前不支持解析下载"
                    )
                await self._set_emoji(event, 123)
                return

        # 缓存直发：本地已解析过的内容直接发送，免解析免下载
        status, cached_entry = await lookup_cached_media(self, event, url, "bilibili")
        if status == "hit":
            await send_cached_result(self, event, cached_entry, "bilibili")
            return
        if status == "blocked":
            return

        # 清晰度降级映射：当前质量 -> 下一档质量
        DOWNGRADE_MAP = {120: 112, 112: 80, 80: 64, 64: 32, 32: 16, 16: 16}

        def downgrade_target(qn: int):
            """沿用现有选档语义：优先真实档位降序，其次固定映射。"""
            if real_qualities_desc and qn in real_qualities_desc:
                idx = real_qualities_desc.index(qn)
                for cand in real_qualities_desc[idx + 1 :]:
                    if not _same_tier(cand, qn):
                        return cand
                return None
            return DOWNGRADE_MAP.get(qn)

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
                video_info = await parse_av(av_match.group(0))
        except UnsupportedBiliLinkError as e:
            if not self.enable_emoji_reaction:
                yield event.plain_result(str(e))
            await self._set_emoji(event, 123)
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

        # 步骤 2：智能预估起始清晰度（使用真实码率探测）
        target_quality = initial_quality
        lowest_still_over = False
        real_qualities_desc: list[int] = []  # 真实可用档位（降序），供后置降级使用
        if self.smart_downgrade and video_duration > 0:
            quality_plan = await probe_quality_plan(
                video_info.bvid, video_info.cid, use_login=use_login
            )
            # 优先使用探测到的真实档位（降序）逐档估算；探测失败时回退固定表
            real_qualities_desc = sorted(
                set((quality_plan.get("qualities") or {}).keys())
                | set(quality_plan.get("accept_quality") or []),
                reverse=True,
            )
            if real_qualities_desc:
                # 起点：真实档位中最接近初始档的档（精确优先，112↔116 同级互备）
                start_qn = _best_qn(real_qualities_desc, initial_quality)
                temp_quality = start_qn
                has_exact = initial_quality in real_qualities_desc
                for qn in real_qualities_desc:
                    if qn > start_qn and not _same_tier(qn, start_qn):
                        continue
                    # 存在精确档时，同级档（112↔116）不参与预估，避免绕过精确档
                    if has_exact and _same_tier(qn, start_qn) and qn != start_qn:
                        continue
                    estimated_size_mb = estimate_size_with_plan(
                        qn, video_duration, quality_plan
                    )
                    if estimated_size_mb <= max_size:
                        temp_quality = qn
                        break
                    temp_quality = qn
                else:
                    temp_quality = real_qualities_desc[-1]
                    lowest_still_over = (
                        estimate_size_with_plan(
                            temp_quality, video_duration, quality_plan
                        )
                        > max_size
                    )
                target_quality = temp_quality
            else:
                temp_quality = initial_quality
                while temp_quality >= 16:
                    estimated_size_mb = estimate_size_with_plan(
                        temp_quality, video_duration, quality_plan
                    )
                    if estimated_size_mb <= max_size:
                        break
                    next_q = DOWNGRADE_MAP.get(temp_quality)
                    if next_q is None or next_q == temp_quality:
                        lowest_still_over = temp_quality == 16
                        break
                    temp_quality = next_q
                target_quality = temp_quality
            if target_quality == initial_quality:
                logger.debug(
                    f"智能预估：视频时长={video_duration}s，目标清晰度=qn{target_quality}，无需降级。"
                )
            else:
                logger.debug(
                    f"智能预估：视频时长={video_duration}s，清晰度 qn{initial_quality} → qn{target_quality}，执行降级。"
                )

        if lowest_still_over:
            logger.warning(
                f"智能预估：最低清晰度(360P)预估仍超过上限 {max_size}MB，跳过下载。"
            )
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 325)
            return

        current_quality = target_quality
        bili_dir = os.path.join(self.download_dir, "bilibili")

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
            logger.debug(f"下载尝试 #{download_attempts}：qn={current_quality}。")

            over_limit_before_file = False
            over_limit_reason = ""
            try:
                with self._file_guard.download_dir(bili_dir):
                    result = await process_bili_video(
                        url,
                        download_flag=videos_download,
                        quality=current_quality,
                        use_login=use_login,
                        event=None,
                        download_dir=bili_dir,
                        max_size_mb=max_size,
                    )
            except asyncio.CancelledError:
                raise
            except (SizePrecheckExceeded, DownloadLimitExceeded) as e:
                # 下载前/过程超限：无半成品，直接进降档流程
                over_limit_before_file = True
                over_limit_reason = str(e)
                result = None
                logger.warning(f"B站 qn{current_quality} 体积超限: {e}")
            except Exception as e:
                logger.warning(f"下载失败: {e}")
                result = {"error": f"下载失败: {e}"}
                break

            if over_limit_before_file:
                next_quality = downgrade_target(current_quality)
                if next_quality is not None and next_quality != current_quality:
                    logger.debug(
                        f"体积预检/过程限额：qn{current_quality} 超限，降级到 qn{next_quality}。"
                    )
                    current_quality = next_quality
                    continue
                result = {"error": f"视频大小超出限制：{over_limit_reason}"}
                break

            file_path_rel = result.get("video_path") if result else None
            if not file_path_rel or not os.path.exists(file_path_rel):
                logger.warning("下载未成功，文件未找到。不进行大小校验，停止降级重试。")
                break

            file_size_mb = os.path.getsize(file_path_rel) / (1024 * 1024)
            if file_size_mb <= max_size:
                break

            # 文件超限：若可降级则继续尝试下一档清晰度
            next_quality = downgrade_target(current_quality)
            can_downgrade = next_quality is not None and next_quality != current_quality
            if can_downgrade:
                logger.debug(
                    f"后置大小校验：{file_size_mb:.2f}MB 超出限制 {max_size}MB，删除文件并降级到 qn{next_quality}。"
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
        if result and result.get("video_path") and os.path.exists(result["video_path"]):
            try:
                await self.media_cache.put(
                    "bilibili",
                    video_info.bvid,
                    {
                        "title": video_title,
                        "duration": video_duration,
                        "media_type": "video",
                        "media_files": [
                            {"path": result["video_path"], "type": "video"}
                        ],
                        "saved_at": time.time(),
                    },
                )
            except Exception as e:
                logger.debug(f"写入 B站缓存失败: {e}")
        await self._process_and_send(event, result, "bili")
        await self._cleanup_media(bili_dir)

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

        # 缓存直发：本地已解析过的内容直接发送，免解析免下载
        status, cached_entry = await lookup_cached_media(self, event, url, "douyin")
        if status == "hit":
            await send_cached_result(self, event, cached_entry, "douyin")
            return
        if status == "blocked":
            return

        # 步骤 1：解析（获取元数据 + 原始数据）
        parser = DouyinParser(
            cookie=cookie, api_url=self.douyin_api_url, data_dir=self.data_dir
        )
        parse_result = await parser.parse(url)

        if not parse_result.success:
            logger.error(f"抖音解析失败: {parse_result.error}")
            nosupport = "暂不支持下载" in parse_result.error
            if not self.enable_emoji_reaction:
                user_msg = (
                    parse_result.error
                    if nosupport
                    else "抱歉，无法获取视频信息，请稍后重试。"
                )
                yield event.plain_result(user_msg)
            await self._set_emoji(event, 123 if nosupport else 357)
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

        # 步骤 3.5：智能预估（data_size 预判最低清晰度仍超限则跳过下载）
        max_size = self.max_video_size
        if self.admin_bypass_content_restrictions and self._is_admin_event(event):
            logger.debug("管理员跳过内容级限制：抖音解析跳过智能降级和大小校验")
            max_size = float("inf")
        downloader = DouyinDownloader(
            download_dir=download_dir,
            max_images=self.media_max_images,
            max_size=max_size,
            smart_downgrade=self.smart_downgrade,
        )
        if downloader.video_all_qualities_over_limit(parse_result):
            logger.warning(
                f"抖音智能预估：最低清晰度仍超过上限 {self.max_video_size}MB，跳过下载。"
            )
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 325)
            return

        # 步骤 4：开始下载
        result = None
        for attempt in range(MAX_DOUYIN_PROCESS_RETRIES + 1):
            try:
                logger.debug(
                    f"尝试下载 (URL: {url}, 尝试次数: {attempt + 1}/{MAX_DOUYIN_PROCESS_RETRIES + 1})"
                )

                with self._file_guard.download_dir(download_dir):
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

        # 文件落地后写入缓存（供后续免解析直发）
        if isinstance(result, dict) and parse_result.aweme_id:
            try:
                cached_files = media_files_from_result(result)
                if cached_files:
                    await self.media_cache.put(
                        "douyin",
                        parse_result.aweme_id,
                        {
                            "title": meta_title,
                            "duration": meta_duration,
                            "media_type": result.get("type", ""),
                            "media_files": cached_files,
                            "saved_at": time.time(),
                        },
                    )
            except Exception as e:
                logger.debug(f"写入抖音缓存失败: {e}")

        # 步骤 5：检查是否需要将标题作为文章合并转发
        if (
            self.text_forward_threshold > 0
            and visible_len(meta_title) > self.text_forward_threshold
        ):
            await forward_long_text(
                event,
                meta_title,
                result,
                set_emoji_fn=lambda emoji_id, set_val=True: self._set_emoji(
                    event, emoji_id, set_val
                ),
            )
            return

        # 处理多媒体类型
        result_type = result.get("type", "")
        if result_type in ["image", "images", "multi_video"]:
            await self._send_douyin_multimedia(event, result)
            return

        # 处理单视频类型
        elif result.get("video_path") and os.path.exists(result["video_path"]):
            await self._process_and_send(event, result, "douyin")

        # 处理失败情况
        else:
            logger.warning("抖音解析失败或结果无效。")
            if not self.enable_emoji_reaction:
                yield event.plain_result(format_douyin_failure_message(result))
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 357)
            return

        # 统一清理文件与缓存
        download_dir_douyin = os.path.join(self.download_dir, "douyin")
        await self._cleanup_media(download_dir_douyin)

    async def _handle_xhs_parsing(self, event: AstrMessageEvent, url: str):
        """小红书解析和下载核心逻辑"""
        download_dir = os.path.join(self.download_dir, "xhs")

        # 缓存直发：本地已解析过的内容直接发送，免解析免下载
        status, cached_entry = await lookup_cached_media(self, event, url, "xhs")
        if status == "hit":
            await send_cached_result(self, event, cached_entry, "xhs")
            return
        if status == "blocked":
            return

        parser = XiaohongshuParser(
            cookie=self._xhs_cookie,
            prefer_original=(self._xhs_image_quality == "original"),
        )
        parse_result = await parser.parse(url)

        if not parse_result.success:
            logger.error(f"小红书解析失败: {parse_result.error}")
            if not self.enable_emoji_reaction:
                user_msg = (
                    parse_result.error
                    if "直播" in parse_result.error
                    else "抱歉，无法获取笔记信息，请稍后重试。"
                )
                yield event.plain_result(user_msg)
            await self._set_emoji(event, 123 if "直播" in parse_result.error else 357)
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
        with self._file_guard.download_dir(download_dir):
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

        # 文件落地后写入缓存（供后续免解析直发）
        if parse_result.note_id:
            try:
                cached_files = media_files_from_result(result)
                if cached_files:
                    await self.media_cache.put(
                        "xhs",
                        parse_result.note_id,
                        {
                            "title": meta_title,
                            "duration": parse_result.duration,
                            "media_type": result.get("type", ""),
                            "media_files": cached_files,
                            "desc": meta_desc,
                            "has_title": has_title,
                            "saved_at": time.time(),
                        },
                    )
            except Exception as e:
                logger.debug(f"写入小红书缓存失败: {e}")

        # 全文（标题 + 正文）过长时使用合并转发
        meta_text = build_xhs_meta_text(meta_title, meta_desc, has_title)
        if (
            self.text_forward_threshold > 0
            and visible_len(meta_text) > self.text_forward_threshold
        ):
            await forward_long_text(
                event,
                meta_text,
                result,
                set_emoji_fn=lambda emoji_id, set_val=True: self._set_emoji(
                    event, emoji_id, set_val
                ),
                text_sender_name="小红书正文",
                media_sender_name="小红书内容",
            )
            await self._cleanup_media(download_dir)
            return

        # 单视频走已有发送逻辑
        if result.get("type") == "video":
            await self._process_and_send(event, result, "xhs")
        else:
            # 多媒体（多图 / 图文混合）复用多媒体发送
            await self._send_douyin_multimedia(event, result, "小红书内容")

        await self._cleanup_media(download_dir)

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
        with self._file_guard.download_dir(download_dir):
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
            merged = Nodes(nodes=nodes)
            ok = await send_chain_with_retry(
                event,
                [merged],
                attempts=2,
                delay=1,
                label="贴吧合并转发",
                logger_obj=logger,
            )
            if ok:
                logger.debug(f"贴吧合并转发成功：{len(nodes)} 个节点")
                report = "ok"
            else:
                logger.warning("贴吧合并转发失败，降级为逐节点发送")
                succeeded, _ = await deliver_items(
                    event,
                    [node.content for node in nodes],
                    attempts=2,
                    delay=1,
                    label="贴吧内容",
                    logger_obj=logger,
                )
                report = await finish_delivery_report(
                    event,
                    succeeded=succeeded,
                    total=len(nodes),
                    label="贴吧内容",
                    logger_obj=logger,
                )
        else:
            logger.debug("当前平台不支持合并转发，贴吧降级为依次发送")
            succeeded, _ = await deliver_items(
                event,
                [node.content for node in nodes],
                attempts=2,
                delay=1,
                label="贴吧内容",
                logger_obj=logger,
            )
            report = await finish_delivery_report(
                event,
                succeeded=succeeded,
                total=len(nodes),
                label="贴吧内容",
                logger_obj=logger,
            )

        await self._set_emoji(event, 424, False)
        await self._set_emoji(event, 357 if report == "failed" else 124)
        await self._cleanup_media(download_dir)

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
        with self._file_guard.download_dir(download_dir):
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
            merged = Nodes(nodes=nodes)
            ok = await send_chain_with_retry(
                event,
                [merged],
                attempts=2,
                delay=1,
                label="NGA 合并转发",
                logger_obj=logger,
            )
            if ok:
                report = "ok"
            else:
                logger.warning("NGA 合并转发失败，降级为逐节点发送")
                succeeded, _ = await deliver_items(
                    event,
                    [node.content for node in nodes],
                    attempts=2,
                    delay=1,
                    label="NGA内容",
                    logger_obj=logger,
                )
                report = await finish_delivery_report(
                    event,
                    succeeded=succeeded,
                    total=len(nodes),
                    label="NGA内容",
                    logger_obj=logger,
                )
        else:
            logger.debug("当前平台不支持合并转发，NGA 降级为依次发送")
            succeeded, _ = await deliver_items(
                event,
                [node.content for node in nodes],
                attempts=2,
                delay=1,
                label="NGA内容",
                logger_obj=logger,
            )
            report = await finish_delivery_report(
                event,
                succeeded=succeeded,
                total=len(nodes),
                label="NGA内容",
                logger_obj=logger,
            )

        await self._set_emoji(event, 424, False)
        await self._set_emoji(event, 357 if report == "failed" else 124)
        await self._cleanup_media(download_dir)

    async def _send_douyin_multimedia(
        self, event: AstrMessageEvent, result: dict, sender_name: str = "抖音内容"
    ) -> None:
        """发送抖音多媒体：部分失败继续发送剩余项，只重试失败项。"""
        ordered_media = list(result.get("ordered_media", []) or [])

        # 兼容旧版本或 API 返回的格式
        if not ordered_media:
            for p in result.get("image_paths", []) or []:
                ordered_media.append({"path": p, "type": "image"})
            for p in result.get("video_paths", []) or []:
                ordered_media.append({"path": p, "type": "video"})

        async def content_for(item):
            path = await self._send_file_if_needed(item["path"])
            if item["type"] == "image":
                return [Image.fromFileSystem(path=path)]
            return [Comp.Video.fromFileSystem(path=path)]

        if not ordered_media:
            logger.warning("没有找到媒体文件")
            if not self.enable_emoji_reaction:
                await send_plain(
                    event,
                    "抱歉，没有找到媒体文件。",
                    attempts=1,
                    delay=0,
                    logger_obj=logger,
                )
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 357)
            return

        if len(ordered_media) == 1:
            item = ordered_media[0]
            if not os.path.exists(item["path"]):
                logger.warning(f"媒体文件不存在: {item['path']}")
                if not self.enable_emoji_reaction:
                    await send_plain(
                        event,
                        "抱歉，媒体文件不存在。",
                        attempts=1,
                        delay=0,
                        logger_obj=logger,
                    )
                await self._set_emoji(event, 424, False)
                await self._set_emoji(event, 357)
                return
            try:
                content = await content_for(item)
            except Exception as e:
                logger.warning(f"构建单个媒体组件失败: {e}")
                await self._set_emoji(event, 424, False)
                await self._set_emoji(event, 357)
                return
            ok = await send_chain_with_retry(
                event,
                content,
                attempts=MAX_SEND_RETRIES + 1,
                delay=2,
                label="单个媒体",
                logger_obj=logger,
            )
            if ok:
                logger.debug(f"成功直接发送单个媒体文件: {item['path']}")
                await self._set_emoji(event, 424, False)
                await self._set_emoji(event, 124)
            else:
                if not self.enable_emoji_reaction:
                    await send_plain(
                        event,
                        "发送失败，请稍后重试。",
                        attempts=1,
                        delay=0,
                        logger_obj=logger,
                    )
                await self._set_emoji(event, 424, False)
                await self._set_emoji(event, 357)
            return

        contents: list[list] = []
        for idx, item in enumerate(ordered_media, 1):
            if not os.path.exists(item["path"]):
                logger.warning(f"文件不存在: {item['path']}")
                continue
            try:
                contents.append(await content_for(item))
            except Exception as e:
                logger.warning(f"处理第 {idx} 个媒体时出错: {e}")

        if not contents:
            logger.warning("无法加载媒体文件")
            if not self.enable_emoji_reaction:
                await send_plain(
                    event,
                    "抱歉，无法加载媒体文件。",
                    attempts=1,
                    delay=0,
                    logger_obj=logger,
                )
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 357)
            return

        if event.get_platform_name() != "aiocqhttp":
            logger.debug("当前平台不支持合并转发，已降级为逐条发送抖音多媒体。")
            succeeded, _ = await deliver_items(
                event,
                contents,
                attempts=MAX_SEND_RETRIES + 1,
                delay=2,
                label="图集",
                logger_obj=logger,
            )
            report = await finish_delivery_report(
                event,
                succeeded=succeeded,
                total=len(contents),
                label="图集",
                logger_obj=logger,
            )
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 357 if report == "failed" else 124)
            return

        logger.debug(f"准备发送 {len(contents)} 个媒体文件（保持顺序，合并转发）")
        sender_id = event.get_self_id()
        forward_nodes = [
            Node(uin=sender_id, name=sender_name, content=content)
            for content in contents
        ]
        merged = Nodes(nodes=forward_nodes)
        ok = await send_chain_with_retry(
            event,
            [merged],
            attempts=2,
            delay=1,
            label="图集合并转发",
            logger_obj=logger,
        )
        if ok:
            logger.debug(f"成功发送 {len(forward_nodes)} 个媒体文件（合并转发）")
            await self._set_emoji(event, 424, False)
            await self._set_emoji(event, 124)
            return

        # 合并转发失败：逐项发送，避免整组丢失；只重试失败项
        logger.warning("图集合并转发失败，降级为逐项发送")
        succeeded, _ = await deliver_items(
            event,
            contents,
            attempts=2,
            delay=1,
            label="图集",
            logger_obj=logger,
        )
        report = await finish_delivery_report(
            event,
            succeeded=succeeded,
            total=len(contents),
            label="图集",
            logger_obj=logger,
        )
        await self._set_emoji(event, 424, False)
        await self._set_emoji(event, 357 if report == "failed" else 124)

    @filter.command("bili_login")
    async def handle_bili_login(self, event: AstrMessageEvent):
        """
        发送 B站登录二维码，等待用户扫码登录
        """
        logger.info("收到 B站登录指令")

        # bili_login 会取消旧流程，同一时刻仅一个登录任务
        login_task, qr_data = await bili_login()

        if not qr_data or login_task is None:
            await send_plain(
                event,
                "生成登录二维码失败，请稍后重试。",
                attempts=1,
                delay=0,
                logger_obj=logger,
            )
            return

        try:
            # 二维码改用内存图片
            qr_component = Image.fromBytes(qr_data["image_bytes"])
            await send_chain_with_retry(
                event,
                [
                    Plain("请使用 B站APP 扫描以下二维码登录：\n"),
                    qr_component,
                    Plain("\n等待登录中...（最多150秒）"),
                ],
                attempts=2,
                delay=1,
                label="登录二维码",
                logger_obj=logger,
            )

            outcome: LoginOutcome = await login_task
            if outcome.saved:
                message = "✅ B站登录成功！Cookie 已保存。"
            elif outcome.cookies:
                message = (
                    "❌ 登录已成功，但 Cookie 保存失败，"
                    "请检查数据目录权限后重试 /bili_login。"
                )
            else:
                message = "❌ 登录失败或超时，请重试。"
            await send_plain(event, message, attempts=1, delay=0, logger_obj=logger)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"处理登录流程时出错: {e}", exc_info=True)
            await send_plain(
                event,
                "登录过程中出现错误，请查看日志。",
                attempts=1,
                delay=0,
                logger_obj=logger,
            )
        finally:
            if not login_task.done():
                login_task.cancel()
                await asyncio.gather(login_task, return_exceptions=True)

    @filter.command("bili_check")
    async def handle_bili_check(self, event: AstrMessageEvent):
        """
        检查 B站 Cookie 是否有效
        """
        logger.info("收到检查 Cookie 指令")

        is_valid = await check_cookie_valid_detailed()

        if is_valid is True:
            yield event.plain_result("✅ B站 Cookie 有效")
        elif is_valid is False:
            yield event.plain_result(
                "❌ B站 Cookie 无效或不存在，请使用 /bili_login 登录"
            )
        else:
            yield event.plain_result(
                "⚠️ Cookie 校验遇到临时网络失败，未能确认凭据状态，请稍后重试 /bili_check"
            )


# ---------------------------------------------------------------------------
# QQ 结构化卡片链接提取与引用判断（仅本 dispatcher 使用）：
# 只读结构化字段，不扫调试字符串；卡片 URL 原样保留。
# ---------------------------------------------------------------------------

# QQ 分享卡片常见结构：meta.detail_1.qqdocurl / meta.news.jumpUrl
_NESTED_URL_PATHS = (
    ("meta", "detail_1", "qqdocurl"),
    ("meta", "news", "jumpUrl"),
    ("meta", "news", "url"),
)
_FLAT_URL_KEYS = ("qqdocurl", "jumpUrl", "url")


def _coerce_mapping(value: Any) -> Optional[dict]:
    """把 dict 或 JSON 字符串归一为 dict；损坏数据返回 None。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text or not text.startswith("{"):
            return None
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _dig(mapping: dict, path: tuple) -> Any:
    current: Any = mapping
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if isinstance(current, str) and key in ("meta", "detail_1", "news"):
            current = _coerce_mapping(current)
    return current


def _valid_url(value: Any) -> Optional[str]:
    """校验提取结果是可直接交给平台匹配的普通字符串 URL。"""
    if not isinstance(value, str):
        return None
    url = value.strip()
    if not url:
        return None
    low = url.lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        return None
    if any(ch.isspace() for ch in url) or "\x00" in url:
        return None
    return url


def extract_card_urls_from_data(msg_data: Any) -> list[str]:
    """从单个消息段的 data 字段提取卡片 URL（字典或 JSON 字符串）。"""
    if msg_data is None:
        return []

    candidates: list[Any] = []
    if isinstance(msg_data, dict):
        candidates.append(msg_data)
        nested = msg_data.get("data")
        if isinstance(nested, (dict, str)):
            candidates.append(nested)
    elif isinstance(msg_data, str):
        candidates.append(msg_data)
    else:
        return []

    urls: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        mapping = _coerce_mapping(candidate)
        if not mapping:
            continue
        found: list[Optional[str]] = [
            _valid_url(_dig(mapping, path)) for path in _NESTED_URL_PATHS
        ]
        found.extend(_valid_url(mapping.get(key)) for key in _FLAT_URL_KEYS)
        for url in found:
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


def _reply_class():
    try:
        from astrbot.api.message_components import Reply

        return Reply
    except Exception:
        return None


def is_reply_component(comp: Any) -> bool:
    """用真实 Reply 组件判断引用，不做字符串包含匹配。"""
    reply_cls = _reply_class()
    if reply_cls is not None and isinstance(comp, reply_cls):
        return True
    comp_type = getattr(comp, "type", None)
    type_value = getattr(comp_type, "value", comp_type)
    if isinstance(type_value, str) and type_value.lower() == "reply":
        return True
    return type(comp).__name__ == "Reply" and hasattr(comp, "id")


def get_current_components(event) -> list:
    try:
        messages = event.get_messages()
    except Exception:
        return []
    return list(messages) if isinstance(messages, (list, tuple)) else []


def has_reply_component(event) -> bool:
    """消息是否带引用（沿用插件原有「跳过引用消息」行为）。"""
    return any(is_reply_component(comp) for comp in get_current_components(event))


def extract_current_message_urls(event) -> list[str]:
    """只从当前消息的结构化组件提取卡片链接；跳过 Reply 及其内嵌消息链。"""
    urls: list[str] = []
    seen: set[str] = set()
    for comp in get_current_components(event):
        if is_reply_component(comp):
            continue
        data = getattr(comp, "data", None)
        if data is None:
            continue
        try:
            extracted = extract_card_urls_from_data(data)
        except Exception:
            # 单个损坏组件不影响正文解析
            continue
        for url in extracted:
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls


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

    # 用真实 Reply 组件判断引用，沿用「跳过引用消息」行为
    if has_reply_component(event):
        return

    message_str = event.message_str or ""
    # 平台匹配源 = 正文 + 结构化卡片链接；卡片 URL 原样保留
    card_urls = extract_current_message_urls(event)
    match_text = "\n".join([message_str, *card_urls]) if card_urls else message_str

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
            r"(https?://b23\.tv/[\w]+|"
            r"https?://bili2233\.cn/[\w]+|"
            r"(?<![A-Za-z0-9])(?:BV1\w{9}|av\d+)(?![A-Za-z0-9])|"
            r"https?://(?:live|t|space|www|m)\.bilibili\.com/[\w/]+)",
            match_text,
        )
        if match_bili:
            url = match_bili.group(1)

            # bvid/av 可能位于 query 参数中
            if not (REG_BV.search(url) or REG_AV.search(url) or REG_B23.search(url)):
                _fallback_id = REG_BV.search(match_text) or REG_AV.search(match_text)
                if _fallback_id:
                    url = _fallback_id.group(0)
                    logger.info(f"从消息中提取到 B站视频号：{url}")

            parse_guard_key = None
            if not _throttle_whitelisted and not self._is_admin_event(event):
                if not await self._check_group_level_requirement(event):
                    await self._set_emoji(event, 179)
                    return
                allowed, parse_guard_key = await self._try_acquire_parse_slot(
                    event, "B站"
                )
                if not allowed:
                    status, cached_entry = await lookup_cached_media(
                        self, event, url, "bilibili"
                    )
                    if status == "hit":
                        await send_cached_result(self, event, cached_entry, "bilibili")
                        return
                    if status == "blocked":
                        return
                    await self._set_emoji(event, 179)
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
            match_text,
        )
        if match_nga:
            url = match_nga.group(1)
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
                    await self._set_emoji(event, 179)
                    return
            try:
                async for response in self._handle_nga_parsing(event, url):
                    yield response
            finally:
                await self._release_parse_slot(parse_guard_key)
            return

    # --- 4. 检查 贴吧 链接 ---
    if "tieba" in _enabled_platforms:
        match_tieba = re.search(r"((?:https?://)?tieba\.baidu\.com/p/\d+)", match_text)
        mini_app_match = None
        if not match_tieba:
            m = re.search(r"m\.q\.qq\.com/a/s/\w+", match_text)
            if m and "贴吧" in match_text:
                mini_app_match = m

        if match_tieba or mini_app_match:
            url = match_tieba.group(1) if match_tieba else mini_app_match.group(0)
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
                    await self._set_emoji(event, 179)
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
            r"(https?://xhslink\.(?:com|cn)/" + _XHS_RE + r")", match_text
        ) or re.search(
            r"(https?://(?:www\.)?(?:xiaohongshu|rednote)\.com/(?:explore|discovery/item)/"
            + _XHS_RE
            + r")",
            match_text,
        )

        if match_xhs:
            url = match_xhs.group(1)
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
                    status, cached_entry = await lookup_cached_media(
                        self, event, url, "xhs"
                    )
                    if status == "hit":
                        await send_cached_result(self, event, cached_entry, "xhs")
                        return
                    if status == "blocked":
                        return
                    await self._set_emoji(event, 179)
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
            re.search(r"(https?://v\.douyin\.com/[a-zA-Z0-9\-\/_]+)", match_text)
            or re.search(
                r"(https?://(?:www\.)?douyin\.com/(?:video|note|slides)/\d+)",
                match_text,
            )
            or re.search(
                r"(https?://(?:www\.)?iesdouyin\.com/share/(?:video|note|slides)/\d+)",
                match_text,
            )
            or re.search(
                r"(https?://(?:www\.)?douyin\.com/(?:discover|user)/?\S*modal_id=\d+)",
                match_text,
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
                    status, cached_entry = await lookup_cached_media(
                        self, event, url, "douyin"
                    )
                    if status == "hit":
                        await send_cached_result(self, event, cached_entry, "douyin")
                        return
                    if status == "blocked":
                        return
                    await self._set_emoji(event, 179)
                    return

            try:
                # 调用抖音处理函数
                async for response in self._handle_douyin_parsing(event, url):
                    yield response
            finally:
                await self._release_parse_slot(parse_guard_key)
            return
