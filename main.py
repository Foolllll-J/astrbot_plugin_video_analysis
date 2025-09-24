from astrbot.api.all import *
from astrbot.api.message_components import Plain, Video
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger

import re
import os
import asyncio
from parsehub import ParseHub, VideoParseResult
from parsehub.config import ParseConfig, DownloadConfig

@register("astrbot_plugin_video_analysis", "Foolllll", "解析并发送视频文件", "0.1","https://github.com/Foolllll-J/astrbot_plugin_video_analysis")
class VideoAnalysis(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.nap_server_address = config.get("nap_server_address", "localhost")
        self.nap_server_port = config.get("nap_server_port", 3658)
        self.show_progress_messages = True
        self.parser_proxy = None
        self.downloader_proxy = None

    async def _send_file_if_needed(self, file_path: str) -> str:
        """Helper function to send file through NAP server if needed"""
        logger.info(f"Checking NAP server configuration... Address: {self.nap_server_address}, Port: {self.nap_server_port}")
        if self.nap_server_address != "localhost":
            logger.info(f"Non-local address detected, attempting to send file via NAP server: {file_path}")
            return await send_file(file_path, HOST=self.nap_server_address, PORT=self.nap_server_port)
        logger.info(f"Local address detected, using direct file path: {file_path}")
        return file_path

@filter.event_message_type(EventMessageType.ALL)
async def auto_parse_bili(self: VideoAnalysis, event: AstrMessageEvent, *args, **kwargs):
    """
    Automatically detects Bilibili share links in messages and parses them using ParseHub.
    """
    message_str = event.message_str
    
    match = re.search(r"(https?://(?:www\.|m\.)?bilibili\.com/video/(?:BV\w+|av\d+)|https?://b23\.tv/\w+)", message_str)
    
    if not match:
        logger.debug("No Bilibili link matched in the message, skipping parsing.")
        return

    url = match.group(0)
    logger.info(f"Successfully matched Bilibili link: {url}")

    if self.show_progress_messages:
        yield event.plain_result("正在解析并下载B站视频...")
        logger.info("Sent a parsing progress message.")

    try:
        logger.info("Instantiating ParseHub and setting up proxies.")
        ph = ParseHub(
            parse_config=ParseConfig(proxy=self.parser_proxy),
            download_config=DownloadConfig(proxy=self.downloader_proxy)
        )
        
        logger.info(f"Starting to parse URL: {url}")
        result = await ph.parse(url)
        logger.info("URL parsing is complete.")

        if not isinstance(result, VideoParseResult):
            logger.warning(f"Parsing result is not a video type. Actual type: {type(result)}")
            yield event.plain_result("Sorry, this link's content is not a video.")
            return
        
        logger.info("Parsing result is a video type. Starting video file download.")
        download_result = await result.download()
        logger.info("Video download task is complete.")
        
        if not download_result or not download_result.media:
            logger.error("Download result is empty or contains no media files.")
            yield event.plain_result("Sorry, the video file could not be downloaded.")
            return

        video_path = download_result.media[0].path
        logger.info(f"Video file downloaded successfully, local path: {video_path}")
        
        logger.info("Starting to send the video file.")
        nap_file_path = await self._send_file_if_needed(video_path)
        yield event.chain_result([Video.fromFileSystem(path=nap_file_path)])
        
        logger.info("Bilibili video sent successfully.")

    except Exception as e:
        logger.error(f"An error occurred while processing the video: {e}", exc_info=True)
        yield event.plain_result(f"Sorry, a problem occurred while processing the video: {str(e)}")