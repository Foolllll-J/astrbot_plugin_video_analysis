from dataclasses import dataclass
from typing import Union, List, Dict, Any

import httpx
import aiofiles
import asyncio
import os
import re
import json
from astrbot.api import logger

# --- 辅助类：模拟外部库的结构 ---
class ParseError(Exception):
    """自定义解析错误"""
    pass

@dataclass
class Video:
    url: str
    thumb_url: str | None = None
    width: int = 0
    height: int = 0
    duration: int = 0

@dataclass
class Image:
    url: str

class DYType:
    VIDEO = "video"
    IMAGE = "image"
    Multimedia = "multimedia"

@dataclass
class DYResult:
    type: str
    platform: str
    video: Video | None = None
    desc: str = ""
    author: str = "N/A"
    image_list: List[Image] | None = None
    multimedia: List[Union[Video, Image]] | None = None

    @staticmethod
    def parse(url: str, json_dict: Dict[str, Any]) -> 'DYResult':
        """解析外部 API 返回的 JSON 数据"""
        data = json_dict.get("data", {})
        desc = data.get("desc", "无标题")
        author = data.get("author", {}).get("nickname", "N/A")
        
        def v_p(video_data: Dict[str, Any]) -> Dict[str, Any]:
            bit_rate = video_data.get("bit_rate")
            if not bit_rate:
                raise ParseError("抖音解析失败: 未获取到视频下载地址 (API可能未返回所需数据)") 
                
            bit_rate.sort(key=lambda x: x["quality_type"], reverse=True)
            best_bit_rate = bit_rate[0]

            video_url = best_bit_rate["play_addr"]["url_list"][0]
            thumb_url = video_data["cover"]["url_list"][-1]
            
            return {
                "video_url": video_url,
                "thumb_url": thumb_url,
                "duration": best_bit_rate.get("duration", 0),
                "width": best_bit_rate["play_addr"]["width"],
                "height": best_bit_rate["play_addr"]["height"],
            }
            
        if video_data := data.get("video"):
            try:
                vpi = v_p(video_data)
                return DYResult(
                    type=DYType.VIDEO, desc=desc, author=author,
                    video=Video(vpi["video_url"], thumb_url=vpi["thumb_url"], width=vpi["width"], height=vpi["height"], duration=vpi["duration"]),
                    platform='douyin',
                )
            except Exception as e:
                logger.error(f"解析视频数据失败: {e}")
                return DYResult(type=DYType.VIDEO, desc=desc, author=author, platform='douyin')
        
        if data.get("images") or data.get("image_post_info"):
            return DYResult(type=DYType.IMAGE, desc=desc, author=author, platform='douyin')

        raise Exception("无法解析内容类型或视频数据缺失")

# --- 核心下载逻辑 ---

# 外部 API 地址
DOUYIN_API_URL = "https://douyin.foolsclub.site" 

# ⚠️ WARNING: 请确保 DOUYIN_COOKIE_STRING 包含您从 F12 复制的全部 Cookie 字符串！
DOUYIN_COOKIE_STRING = "" 


def _get_douyin_cookie_header() -> str:
    """将硬编码的 Cookie 字符串转换为 HTTP 请求头格式。"""
    return DOUYIN_COOKIE_STRING


async def process_douyin_video(url: str, download_dir: str):
    """
    使用外部 API 获取抖音视频直链，并下载到本地。
    """
    logger.info(f"[INFO] Douyin: 开始通过外部API解析链接: {url}")
    
    # 1. API 解析
    api_endpoint = f"{DOUYIN_API_URL}/api/hybrid/video_data"
    
    try:
        # 修复：将 verify=False 提升到 Client 初始化阶段
        async with httpx.AsyncClient(timeout=30, verify=False) as client:
            params = {"url": url, "minimal": False}
            
            # 构建请求头（不含 Cookie，因为外部 API 已经部署了）
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
                'Referer': 'https://www.douyin.com/'
            }
            
            response = await client.get(api_endpoint, params=params, headers=headers)
            
            logger.info(f"[DEBUG] Douyin API 响应状态码: {response.status_code}")
            
            if response.status_code != 200:
                logger.error(f"[ERROR] Douyin API HTTP 错误: {response.status_code}")
                logger.error(f"[ERROR] Douyin API 错误响应内容: {response.text}")
                return None
                
            api_data = response.json()
            
            logger.info(f"[DEBUG] Douyin API 原始 JSON: {json.dumps(api_data, ensure_ascii=False)[:300]}...")

            # 检查API返回码 (业务码)
            if api_data.get("code") != 200 and api_data.get("status_code") != 0:
                logger.error(f"[ERROR] Douyin API 业务错误: {api_data.get('msg', '未知业务错误')}")
                logger.error(f"[DEBUG] Douyin API 失败 JSON: {json.dumps(api_data, ensure_ascii=False)}") 
                return None
            
            result = DYResult.parse(url, api_data)
            
            if result.type != DYType.VIDEO:
                logger.warning("[WARN] Douyin 内容不是视频，跳过下载。")
                return {"title": result.desc, "author": result.author, "url": url, "video_path": None}

            video_url = result.video.url
            match = re.search(r'video/(\d+)', video_url)
            simple_id = match.group(1) if match else re.search(r'/([a-zA-Z0-9]+)/', url).group(1) if re.search(r'/([a-zA-Z0-9]+)/', url) else "unknown_douyin"
            
    except httpx.ReadTimeout:
        logger.error("[ERROR] Douyin API 请求超时 (30秒)。")
        return None
    except Exception as e:
        logger.error(f"[ERROR] Douyin 解析或网络错误: {e}", exc_info=True)
        return None

    # 3. 检查缓存（使用简化 ID）
    os.makedirs(download_dir, exist_ok=True)
    final_file = os.path.join(download_dir, f"{simple_id}.mp4")
    
    if os.path.exists(final_file):
        logger.info(f"[INFO] Douyin 文件已存在，跳过下载: {final_file}")
        return {"title": result.desc, "author": result.author, "url": url, "video_path": final_file}

    # 4. 直链下载
    logger.info(f"[INFO] Douyin: 开始下载直链文件到: {final_file}")
    try:
        # 修复：使用新的 Client 实例进行直链下载，并继承 verify=False
        async with httpx.AsyncClient(timeout=300, verify=False) as client: 
            download_headers = {'User-Agent': 'Mozilla/5.0'}
            async with client.stream('GET', video_url, headers=download_headers) as response: 
                response.raise_for_status()
                
                # 流式写入
                async with aiofiles.open(final_file, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        await f.write(chunk)
                        
        logger.info(f"[INFO] Douyin 文件下载完成: {final_file}")
        return {
            "title": result.desc,
            "author": result.author,
            "url": url,
            "video_path": final_file,
            "view_count": 0, "like_count": 0, "danmaku_count": 0, "coin_count": 0, "favorite_count": 0
        }
        
    except Exception as e:
        logger.error(f"[ERROR] Douyin 文件下载失败: {e}")
        if os.path.exists(final_file):
            os.remove(final_file)
        return None
