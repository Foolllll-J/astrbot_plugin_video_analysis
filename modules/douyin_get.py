from dataclasses import dataclass
from typing import Union, List, Dict, Any

import httpx
import aiofiles
import os
import json
import hashlib
from astrbot.api import logger
from ..douyin_scraper.douyin_parser import DouyinParser

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
                # 返回一个标记，表示可能是图片类型
                raise ParseError(f"视频数据解析失败: {e}")
        
        if data.get("images") or data.get("image_post_info"):
            return DYResult(type=DYType.IMAGE, desc=desc, author=author, platform='douyin')

        raise Exception("无法解析内容类型或视频数据缺失")

async def process_douyin_video(url: str, download_dir: str, api_url: str = None, cookie: str = None):
    """
    获取抖音视频/图片。
    逻辑：
    1. 如果有 cookie，优先尝试本地解析。
    2. 如果本地解析失败或没有 cookie，且有 api_url，则尝试 API 解析。
    3. 如果都失败或都未提供，返回 None。
    
    Args:
        url (str): 抖音分享链接。
        download_dir (str): 文件下载目录。
        api_url (str): 外部解析服务的地址。
        cookie (str): 抖音 Cookie。
    
    Returns:
        dict: 包含视频/图片信息和下载路径，或 None
    """
    
    # 1. 优先尝试本地解析 (如果有 Cookie)
    if cookie:
        logger.info("[INFO] Douyin: 尝试本地解析 (使用 Cookie)")
        try:
            result = await process_douyin_video_local(url, download_dir, cookie)
            if result:
                return result
        except Exception as e:
            logger.error(f"[ERROR] Douyin 本地解析失败: {e}")
            # 继续尝试 API
    
    # 2. 尝试 API 解析
    if api_url:
        logger.info("[INFO] Douyin: 尝试 API 解析")
        # 先尝试原有的 video_data API
        result = await _try_video_data_api(url, download_dir, api_url)
        if result is not None:
            return result
        
        # 如果失败，尝试新的 download API
        logger.info("[INFO] Douyin: 尝试 download API")
        result = await _try_download_api(url, download_dir, api_url)
        if result is not None:
            return result
            
    return None

async def process_douyin_video_local(url: str, download_dir: str, cookie: str):
    """使用本地解析逻辑"""
    parser = DouyinParser(cookie)
    data = await parser.parse(url)
    
    if not data or "error" in data:
        logger.error(f"[ERROR] Douyin 本地解析返回错误: {data.get('error') if data else 'Unknown'}")
        return None
        
    title = data.get("desc", "抖音作品")
    author = data.get("author_nickname", "N/A")
    aweme_id = data.get("aweme_id", hashlib.md5(url.encode()).hexdigest())
    
    os.makedirs(download_dir, exist_ok=True)
    
    media_items = []
    
    for i, item in enumerate(data.get("media_urls", [])):
        m_url = item["url"]
        m_type = item["type"]
        
        if m_type == "video":
            v_file = os.path.join(download_dir, f"{aweme_id}_{i}.mp4")
            if os.path.exists(v_file) or await _download_file(m_url, v_file):
                media_items.append({"path": v_file, "type": "video"})
        else: # image
            file_ext = ".jpg"
            if ".png" in m_url.lower(): file_ext = ".png"
            elif ".webp" in m_url.lower(): file_ext = ".webp"
            elif ".gif" in m_url.lower(): file_ext = ".gif"
            
            img_file = os.path.join(download_dir, f"{aweme_id}_{i}{file_ext}")
            if os.path.exists(img_file) or await _download_file(m_url, img_file):
                media_items.append({"path": img_file, "type": "image"})

    if not media_items:
        return None
        
    # 如果只有一个媒体
    if len(media_items) == 1:
        item = media_items[0]
        if item["type"] == "video":
            return {
                "title": title, "author": author, "url": url,
                "video_path": item["path"], "type": "video"
            }
        else:
            return {
                "title": title, "author": author, "url": url,
                "image_paths": [item["path"]], "type": "image"
            }
            
    # 多个媒体，保持原始顺序
    return {
        "title": title, "author": author, "url": url,
        "ordered_media": media_items,
        "type": "multi_video" if any(i["type"] == "video" for i in media_items) else "images"
    }

async def _download_file(url: str, save_path: str) -> bool:
    """通用下载函数"""
    try:
        async with httpx.AsyncClient(timeout=300, verify=False) as client: 
            headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36', 'Referer': 'https://www.douyin.com/'}
            async with client.stream('GET', url, headers=headers, follow_redirects=True) as response: 
                response.raise_for_status()
                async with aiofiles.open(save_path, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        await f.write(chunk)
        return True
    except Exception as e:
        logger.error(f"[ERROR] 文件下载失败: {url}, 错误: {e}")
        if os.path.exists(save_path):
            os.remove(save_path)
        return False

async def _try_video_data_api(url: str, download_dir: str, api_url: str):
    """尝试使用 video_data API 解析"""
    # 1. API 解析
    api_endpoint = f"{api_url}/api/hybrid/video_data"
    
    try:
        # 禁用 SSL 验证 (verify=False)
        async with httpx.AsyncClient(timeout=30, verify=False) as client:
            params = {"url": url, "minimal": False}
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
                'Referer': 'https://www.douyin.com/'
            }
            
            response = await client.get(api_endpoint, params=params, headers=headers)
            
            if response.status_code != 200:
                logger.error(f"[ERROR] Douyin API HTTP 错误: {response.status_code}")
                logger.error(f"[ERROR] Douyin API 错误响应内容: {response.text}")
                return None
                
            api_data = response.json()
            
            if api_data.get("code") != 200 and api_data.get("status_code") != 0:
                logger.error(f"[ERROR] Douyin API 业务错误: {api_data.get('msg', '未知业务错误')}")
                logger.error(f"[DEBUG] Douyin API 失败 JSON: {json.dumps(api_data, ensure_ascii=False)}") 
                return None
            
            try:
                result = DYResult.parse(url, api_data)
            except ParseError as e:
                return None
            except Exception as e:
                logger.error(f"[ERROR] Douyin 解析发生未知错误: {e}", exc_info=True)
                return None
            
            if result.type != DYType.VIDEO:
                return {"title": result.desc, "author": result.author, "url": url, "video_path": None}

            if not result.video:
                return None
            
            video_url = result.video.url
            
    except httpx.ReadTimeout:
        logger.error("[ERROR] Douyin API 请求超时 (30秒)。")
        return None
    except Exception as e:
        logger.error(f"[ERROR] Douyin 解析或网络错误: {e}", exc_info=True)
        return None

    # 3. 文件命名和缓存检查 (使用 URL 的 MD5 哈希作为唯一 ID)
    url_bytes = url.encode('utf-8')
    simple_id = hashlib.md5(url_bytes).hexdigest()

    os.makedirs(download_dir, exist_ok=True)
    final_file = os.path.join(download_dir, f"{simple_id}.mp4")
    
    if os.path.exists(final_file):
        return {"title": result.desc, "author": result.author, "url": url, "video_path": final_file}

    try:
        async with httpx.AsyncClient(timeout=300, verify=False) as client: 
            download_headers = {'User-Agent': 'Mozilla/5.0'}
            async with client.stream('GET', video_url, headers=download_headers) as response: 
                response.raise_for_status()
                
                async with aiofiles.open(final_file, "wb") as f:
                    async for chunk in response.aiter_bytes():
                        await f.write(chunk)
                        
        logger.info(f"[INFO] Douyin 视频下载完成")
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

async def _try_download_api(url: str, download_dir: str, api_url: str):
    """尝试使用 download API 下载"""
    logger.info(f"[INFO] Douyin: 开始使用 download API 解析")
    
    api_endpoint = f"{api_url}/api/download"
    
    try:
        async with httpx.AsyncClient(timeout=60, verify=False, follow_redirects=True) as client:
            params = {
                "url": url,
                "prefix": "true",
                "with_watermark": "false"
            }
            
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            }
            
            response = await client.get(api_endpoint, params=params, headers=headers)
            
            if response.status_code != 200:
                logger.error(f"[ERROR] Douyin download API HTTP 错误: {response.status_code}")
                logger.error(f"[ERROR] 响应内容前500字符: {response.text[:500]}")
                return None
            
            # 检查 Content-Type 来判断是图片还是视频
            content_type = response.headers.get('content-type', '')
            
            # 如果是 JSON 响应，可能是错误信息
            if 'application/json' in content_type:
                try:
                    error_data = response.json()
                    logger.error(f"[ERROR] Douyin download API 返回 JSON 错误: {json.dumps(error_data, ensure_ascii=False)}")
                    return None
                except:
                    pass
            
            # 生成唯一ID
            url_bytes = url.encode('utf-8')
            simple_id = hashlib.md5(url_bytes).hexdigest()
            os.makedirs(download_dir, exist_ok=True)
            
            # 判断文件类型
            if 'image' in content_type:
                logger.info("[INFO] Douyin download API: 检测到图片类型")
                
                # 保存文件
                file_ext = '.jpg' if 'jpeg' in content_type or 'jpg' in content_type else '.png' if 'png' in content_type else '.jpg'
                final_file = os.path.join(download_dir, f"{simple_id}{file_ext}")
                
                async with aiofiles.open(final_file, 'wb') as f:
                    await f.write(response.content)
                
                logger.info(f"[INFO] Douyin 单张图片下载完成")
                return {
                    "title": "抖音图片",
                    "author": "N/A",
                    "url": url,
                    "image_paths": [final_file],
                    "type": "image"
                }
                
            elif 'video' in content_type or 'octet-stream' in content_type:
                final_file = os.path.join(download_dir, f"{simple_id}.mp4")
                
                if os.path.exists(final_file):
                    return {
                        "title": "抖音视频",
                        "author": "N/A",
                        "url": url,
                        "video_path": final_file
                    }
                
                async with aiofiles.open(final_file, 'wb') as f:
                    await f.write(response.content)
                
                logger.info(f"[INFO] Douyin 视频下载完成")
                return {
                    "title": "抖音视频",
                    "author": "N/A",
                    "url": url,
                    "video_path": final_file
                }
            
            elif 'application/zip' in content_type or 'application/x-zip' in content_type:
                logger.info("[INFO] Douyin: 检测到图集（ZIP）")
                # 下载并解压 ZIP
                zip_file = os.path.join(download_dir, f"{simple_id}.zip")
                
                async with aiofiles.open(zip_file, 'wb') as f:
                    await f.write(response.content)
                
                # 解压 ZIP
                import zipfile
                extract_dir = os.path.join(download_dir, f"{simple_id}_images")
                os.makedirs(extract_dir, exist_ok=True)
                
                with zipfile.ZipFile(zip_file, 'r') as zip_ref:
                    zip_ref.extractall(extract_dir)
                
                # 获取所有图片文件
                image_files = []
                for root, dirs, files in os.walk(extract_dir):
                    for file in files:
                        if file.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
                            image_files.append(os.path.join(root, file))
                
                logger.info(f"[INFO] 从ZIP中提取了 {len(image_files)} 张图片")
                
                # 删除 ZIP 文件
                os.remove(zip_file)
                
                return {
                    "title": "抖音图集",
                    "author": "N/A",
                    "url": url,
                    "image_paths": sorted(image_files),
                    "type": "images"
                }
            
            else:
                logger.warning(f"[WARN] Douyin download API: 未知的 Content-Type: {content_type}")
                logger.warning(f"[WARN] 响应内容前500字符: {response.text[:500]}")
                return None
                
    except httpx.ReadTimeout:
        logger.error("[ERROR] Douyin download API 请求超时")
        return None
    except Exception as e:
        logger.error(f"[ERROR] Douyin download API 错误: {e}", exc_info=True)
        return None
