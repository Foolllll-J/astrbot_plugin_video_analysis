import asyncio
import aiohttp
import re
import os
import aiofiles
import json
import qrcode
import base64
from io import BytesIO
from urllib.parse import unquote
from astrbot.api import logger
import subprocess

# 全局变量，由 main.py 初始化时设置
COOKIE_FILE = None
YUTTO_PATH = "/root/.local/bin/yutto"
COOKIE_VALID = None

# 估算码率映射表 (基于 B站实际数据，单位：Mbps)
ESTIMATED_BITRATES_MBPS = {
    120: 5.5,  # 4K
    112: 2.6,  # 1080P+
    80: 1.4,   # 1080P
    64: 0.65,  # 720P
    32: 0.35,  # 480P
    16: 0.25,  # 360P
}

def estimate_size(quality_qn: int, duration_seconds: int) -> float:
    """
    根据 B站估算码率和时长，计算文件大小（MB）。
    
    Args:
        quality_qn: B站质量代码（如 80, 64）。
        duration_seconds: 视频时长（秒）。
    
    Returns:
        估算文件大小（MB）。
    """
    # 码率 (Mbps) / 8 = 码率 (MB/s)
    # 使用 .get() 确保如果质量代码不在表中，默认为 1.0 Mbps
    bitrate_mbps = ESTIMATED_BITRATES_MBPS.get(quality_qn, 1.0) 
    # duration_seconds * (bitrate_mbps / 8)
    return (bitrate_mbps * duration_seconds) / 8


def init_bili_module(cookie_file_path: str):
    """初始化 bili_get 模块，设置 Cookie 文件路径
    
    Args:
        cookie_file_path: Cookie 文件的完整路径
    """
    global COOKIE_FILE
    COOKIE_FILE = cookie_file_path
    os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)
    logger.info(f"bili_get 模块已初始化，Cookie 路径: {COOKIE_FILE}")

CONFIG = {
    "VIDEO": {"enable": True, "send_link": False, "send_video": True}
}

def map_quality_to_height(quality_code: int) -> int:
    """将 B站质量代码映射为 yutto 的质量代码（qn）。"""
    if quality_code >= 120: return 120 # 4K
    if quality_code >= 112: return 112 # 1080P+
    if quality_code >= 80: return 80  # 1080P
    if quality_code >= 64: return 64   # 720P
    if quality_code >= 32: return 32   # 480P
    if quality_code >= 16: return 16   # 360P
    return 80 # 默认 1080P

# 正则表达式 and AV/BV conversion functions
REG_B23 = re.compile(r'(b23\.tv|bili2233\.cn)\/[\w]+')
REG_BV = re.compile(r'BV1\w{9}')
REG_AV = re.compile(r'av\d+', re.I)

AV2BV_TABLE = 'fZodR9XQDSUm21yCkr6zBqiveYah8bt4xsWpHnJE7jL5VG3guMTKNPAwcF'
AV2BV_TR = {c: i for i, c in enumerate(AV2BV_TABLE)}
AV2BV_S = [11, 10, 3, 8, 4, 6]
AV2BV_XOR = 177451812
AV2BV_ADD = 8728348608

def format_number(num):
    """格式化数字显示"""
    num = int(num)
    if num < 1e4: return str(num)
    elif num < 1e8: return f"{num/1e4:.1f}万"
    else: return f"{num/1e8:.1f}亿"

def av2bv(av):
    """AV号转BV号"""
    av_num = re.search(r'\d+', av)
    if not av_num: return None
    try: x = (int(av_num.group()) ^ AV2BV_XOR) + AV2BV_ADD
    except: return None
    r = list('BV1 0 4 1 7  ')
    for i in range(6):
        idx = (x // (58**i)) % 58
        r[AV2BV_S[i]] = AV2BV_TABLE[idx]
    return ''.join(r).replace(' ', '0')

async def bili_request(url, return_json=True):
    """发送B站API请求"""
    if not url or not isinstance(url, str): return {"code": -400, "message": "Invalid URL"}
    headers = {"referer": "https://www.bilibili.com/", "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36", "Accept-Encoding": "gzip, deflate"}
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                response.raise_for_status()
                if return_json:
                    try:
                        data = await response.json()
                        if not isinstance(data, dict): return {"code": -400, "message": "Invalid JSON format"}
                        return data
                    except aiohttp.ContentTypeError: return {"code": -400, "message": "Invalid content type"}
                    except Exception as e: return {"code": -400, "message": f"JSON parse error: {str(e)}"}
                else: return await response.read()
    except aiohttp.ClientError as e: return {"code": -400, "message": f"Network error: {str(e)}"}
    except asyncio.TimeoutError: return {"code": -400, "message": "Request timeout"}

async def check_cookie_valid():
    """检查Cookie是否有效"""
    global COOKIE_VALID
    COOKIE_VALID = None
    cookies = await load_cookies()
    if not cookies:
        logger.debug("未找到Cookie文件或Cookie文件为空，需要登录")
        return False
    required_fields = {"SESSDATA": lambda v: len(v) > 30 and ',' in v, "bili_jct": lambda v: len(v) == 32, "DedeUserID": lambda v: v.isdigit()}
    for field, validator in required_fields.items():
        if field not in cookies or not validator(str(cookies[field])):
            logger.debug(f"Cookie字段验证失败: {field} = {cookies.get(field)}")
            return False
    url = "https://api.bilibili.com/x/member/web/account"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "Referer": "https://space.bilibili.com/", "Origin": "https://space.bilibili.com", "Cookie": "; ".join([f"{k}={v}" for k, v in cookies.items()]), "Accept-Encoding": "gzip, deflate"}
    try:
        async with aiohttp.ClientSession() as session:
            timeout = aiohttp.ClientTimeout(total=10)
            async with session.get(url, headers=headers, timeout=timeout) as response:
                data = await response.json()
                if data.get("code") == 0:
                    api_mid = str(data.get("data", {}).get("mid", ""))
                    cookie_mid = str(cookies["DedeUserID"])
                    if api_mid == cookie_mid:
                        COOKIE_VALID = True
                        return True
                return False
    except Exception: return False

async def parse_b23(short_url):
    """解析b23短链接"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(f"https://{short_url}", allow_redirects=True) as response:
                real_url = str(response.url)
                if REG_BV.search(real_url): return await parse_video(REG_BV.search(real_url).group())
                elif REG_AV.search(real_url): return await parse_video(av2bv(REG_AV.search(real_url).group()))
                return None
    except aiohttp.ClientError: return None

async def parse_video(bvid):
    """解析视频信息"""
    api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    data = await bili_request(api_url)
    if data.get("code") != 0: return None
    info = data["data"]
    return {"aid": info["aid"], "cid": info["cid"], "bvid": bvid, "title": info["title"], "cover": info["pic"], "duration": info["duration"], "stats": {"view": format_number(info["stat"]["view"]), "like": format_number(info["stat"]["like"]), "danmaku": format_number(info["stat"]["danmaku"]), "coin": format_number(info["stat"]["coin"]), "favorite": format_number(info["stat"]["favorite"])}}
        
async def save_cookies_dict(cookies):
    """保存Cookie到文件"""
    try:
        async with aiofiles.open(COOKIE_FILE, "w", encoding="utf-8") as f:
            await f.write(json.dumps(cookies, ensure_ascii=False, indent=2))
        logger.info(f"Cookie已保存到: {COOKIE_FILE}")
        return True
    except Exception as e:
        logger.error(f"保存Cookie失败: {str(e)}")
        return False

async def load_cookies():
    """从文件加载Cookie"""
    if not os.path.exists(COOKIE_FILE):
        logger.warning(f"Cookie文件不存在: {COOKIE_FILE}")
        return None
    try:
        async with aiofiles.open(COOKIE_FILE, "r", encoding="utf-8") as f:
            content = await f.read()
            if not content.strip(): logger.warning("Cookie文件为空"); return None
            cookies = json.loads(content); return cookies
    except json.JSONDecodeError: logger.error("Cookie文件格式错误"); return None
    except Exception as e: logger.error(f"加载Cookie失败: {str(e)}"); return None

async def generate_qrcode():
    """生成B站登录二维码
    
    Returns:
        包含二维码信息的字典，格式：
        {
            "qrcode_key": str,  # 用于轮询登录状态
            "image_base64": str,  # base64 编码的二维码图片
            "url": str  # 二维码包含的登录URL
        }
    """
    url = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    data = await bili_request(url)
    if data.get("code") != 0:
        logger.error(f"获取二维码失败: {data.get('message')}")
        return None
    qr_data = data["data"]
    qr_url = qr_data["url"]
    qrcode_key = qr_data["qrcode_key"]
    
    # 生成二维码图片
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
    qr.add_data(qr_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    # 转换为 base64
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode()
    
    logger.info("B站登录二维码已生成")
    return {"qrcode_key": qrcode_key, "image_base64": img_str, "url": qr_url}

async def check_login_status(qrcode_key):
    """检查登录状态"""
    url = f"https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={qrcode_key}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36", "Accept-Encoding": "gzip, deflate"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                result = await response.json(); return result
    except aiohttp.ClientError: return {"code": -1, "message": "检查登录状态失败"}

async def bili_login():
    """B站扫码登录流程（新版API）
    
    此函数用于通过指令触发登录，返回二维码数据供调用者处理。
    不再在控制台打印二维码，而是返回数据给上层应用，
    以便通过聊天消息发送给管理员。
    
    Returns:
        登录任务和二维码数据的元组 (login_task, qr_data)
        login_task: asyncio.Task 对象，用于等待登录完成
        qr_data: 包含二维码信息的字典
    """
    logger.info("正在生成B站登录二维码...")
    qr_data = await generate_qrcode()
    if not qr_data:
        return None, None
    
    logger.info("B站登录二维码已生成，等待扫码...")
    qrcode_key = qr_data["qrcode_key"]
    
    # 创建登录状态检查任务
    login_task = asyncio.create_task(check_login_status_loop(qrcode_key))
    
    return login_task, qr_data

async def check_login_status_loop(qrcode_key):
    """循环检查登录状态，直到登录成功或超时"""
    logger.info("等待登录...（最多40秒）")
    for _ in range(40):
        await asyncio.sleep(1)
        status = await check_login_status(qrcode_key)
        if status.get("code") == 0:
            data = status.get("data", {})
            if data.get("code") == 0:
                logger.info("登录成功!")
                try:
                    url = data.get("url", "")
                    cookies = {}
                    if "?" in url:
                        url_params = url.split("?")[1]
                        for param in url_params.split("&"):
                            if "=" in param:
                                key, value = param.split("=", 1)
                                useful_keys = ["_uuid", "DedeUserID", "DedeUserID__ckMd5", "SESSDATA", "bili_jct", "bili_ticket", "bili_ticket_expires", "CURRENT_FNVAL", "CURRENT_QUALITY", "enable_feed_channel", "enable_web_push", "header_theme_version", "home_feed_column", "LIVE_BUVID", "PVID", "browser_resolution", "buvid_fp", "buvid3", "fingerprint"]
                                if key in useful_keys:
                                    cookies[key] = unquote(value)
                        if not cookies.get("SESSDATA") or not cookies.get("DedeUserID"):
                            raise ValueError("获取的Cookie格式异常")
                        logger.info(f"获取到的Cookie: {cookies}")
                        await save_cookies_dict(cookies)
                        return cookies
                    else:
                        raise ValueError("URL格式异常，无法提取参数")
                except Exception as e:
                    logger.error(f"登录异常: {str(e)}")
                    logger.debug(f"原始响应数据: {data}")
                    return None
            elif data.get("code") == -2:
                logger.warning("二维码已过期，请重新获取")
                return None
            elif data.get("code") == -4 or data.get("code") == -5:
                logger.debug("等待手机上确认登录")
    logger.warning("登录超时，请重试")
    return None

def check_yutto_installed():
    """检查 yutto 是否安装在 PATH 中，或检查绝对路径"""
    if os.path.exists(YUTTO_PATH):
        return True
    
    try:
        subprocess.run(['yutto', '--version'], check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

async def download_video_yutto(bvid, cookies_file, download_dir, quality=80, num_workers=8):
    """
    使用 yutto 命令下载视频。
    """
    yutto_cmd = YUTTO_PATH if os.path.exists(YUTTO_PATH) else 'yutto'
    if not check_yutto_installed():
        logger.error("yutto 未安装或不在系统 PATH 中。无法进行下载。")
        raise Exception("yutto is not installed or not found in PATH.")

    os.makedirs(download_dir, exist_ok=True)
    
    output_filename = f"{bvid}.mp4"
    output_path = os.path.join(download_dir, output_filename)
    
    # 1. 读取 Cookie 并提取 SESSDATA
    try:
        async with aiofiles.open(cookies_file, "r", encoding="utf-8") as f:
            json_cookies = json.loads(await f.read())
            sessdata = json_cookies.get("SESSDATA")
            if not sessdata:
                raise ValueError("Cookie 文件中缺少 SESSDATA 字段。")
    except Exception as e:
        logger.error(f"无法读取或解析 JSON Cookie 文件: {cookies_file}. 错误: {e}")
        raise Exception("无法获取 SESSDATA Cookie，请检查格式或登录状态。")

    # 2. 清理旧的缓存文件
    if os.path.exists(output_path):
        os.remove(output_path)

    # 3. 动态构建质量参数
    quality_qn = map_quality_to_height(quality)
    logger.debug(f"目标质量代码 {quality} 映射到 yutto qn: {quality_qn}。")

    # 4. 构建 yutto 命令
    cmd = [
        yutto_cmd,
        'https://www.bilibili.com/video/' + bvid,
        '-c', sessdata,                       # 直接传递 SESSDATA
        '-d', download_dir,                   # 存放根目录
        '-q', str(quality_qn),                # 视频质量等级 (qn)
        '-n', str(num_workers),               # 最大并行 Worker 数量
        '-w',                                 # 强制覆盖
        '--no-color',                         # 禁用颜色
        '--no-progress',                      # 禁用进度条 (便于日志输出)
        '--subpath-template', bvid,           # 文件名为 BVID (避免目录嵌套，仅文件名)
        '--no-danmaku',                       # 通常下载视频不需要弹幕
        '--no-subtitle',                      # 通常下载视频不需要字幕
    ]
    
    logger.debug(f"yutto CMD: {' '.join(cmd)}")
    
    # 5. 运行 yutto (使用标准的 asyncio 捕获)
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    logger.info("yutto 进程已成功启动。请等待下载和合并...")

    # 6. 捕获输出和等待
    stdout_data, stderr_data = await process.communicate()
    
    # 7. 检查退出码
    if process.returncode != 0:
        error_output = stderr_data.decode(errors='ignore').strip()
        logger.error(f"yutto 命令行执行完毕。退出码: {process.returncode}。")
        logger.error(f"yutto 错误输出: {error_output[:1000]}...")
        raise Exception(f"yutto 下载失败，请检查 yutto 日志。")

    logger.info(f"yutto 命令行执行完毕。退出码: {process.returncode}。正在检查文件。")

    # 8. 检查最终文件是否存在
    if os.path.exists(output_path):
        try:
            os.utime(output_path, None) 
            logger.info(f"文件时间戳已更新至当前时间，防止被自动清理。")
        except Exception as utime_e:
            logger.warning(f"无法更新文件时间戳 (os.utime 失败): {utime_e}")
        
        logger.info(f"yutto 下载成功: {output_path}")
        return output_path
    else:
        logger.error(f"yutto 运行成功但未生成文件：{output_path}。")
        stdout_str = stdout_data.decode(errors='ignore').strip()
        logger.info(f"yutto 标准输出: {stdout_str[:500]}...")
        
        if "尚不支持 DASH 格式" in stdout_str:
             raise Exception("该视频尚不支持 DASH 格式。")
        
        raise Exception(f"yutto 运行成功但未能生成最终文件。Stdout: {stdout_str[:100]}")

async def download_video_yutto_no_login(bvid, download_dir, quality=16, num_workers=8):
    """
    使用 yutto 命令下载视频（不需要Cookie，只能下载360p及以下）。
    """
    yutto_cmd = YUTTO_PATH if os.path.exists(YUTTO_PATH) else 'yutto'
    if not check_yutto_installed():
        logger.error("yutto 未安装或不在系统 PATH 中。无法进行下载。")
        raise Exception("yutto is not installed or not found in PATH.")

    os.makedirs(download_dir, exist_ok=True)
    
    output_filename = f"{bvid}.mp4"
    output_path = os.path.join(download_dir, output_filename)
    
    # 清理旧的缓存文件
    if os.path.exists(output_path):
        os.remove(output_path)

    # 动态构建质量参数（360p对应qn=16）
    quality_qn = map_quality_to_height(quality)
    logger.debug(f"目标质量代码 {quality} 映射到 yutto qn: {quality_qn}。")

    # 构建 yutto 命令（不使用 -c 参数）
    cmd = [
        yutto_cmd,
        'https://www.bilibili.com/video/' + bvid,
        '-d', download_dir,                   # 存放根目录
        '-q', str(quality_qn),                # 视频质量等级 (qn)
        '-n', str(num_workers),               # 最大并行 Worker 数量
        '-w',                                 # 强制覆盖
        '--no-color',                         # 禁用颜色
        '--no-progress',                      # 禁用进度条
        '--subpath-template', bvid,           # 文件名为 BVID
        '--no-danmaku',                       # 不下载弹幕
        '--no-subtitle',                      # 不下载字幕
    ]
    
    logger.debug(f"yutto CMD (no login): {' '.join(cmd)}")
    
    # 运行 yutto
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    logger.info("yutto 进程已成功启动（无登录模式）。请等待下载和合并...")

    # 捕获输出和等待
    stdout_data, stderr_data = await process.communicate()
    
    # 检查退出码
    if process.returncode != 0:
        error_output = stderr_data.decode(errors='ignore').strip()
        logger.error(f"yutto 命令行执行完毕。退出码: {process.returncode}。")
        logger.error(f"yutto 错误输出: {error_output[:1000]}...")
        raise Exception(f"yutto 下载失败（无登录模式），请检查 yutto 日志。")

    logger.info(f"yutto 命令行执行完毕。退出码: {process.returncode}。正在检查文件。")

    # 检查最终文件是否存在
    if os.path.exists(output_path):
        try:
            os.utime(output_path, None) 
            logger.info(f"文件时间戳已更新至当前时间，防止被自动清理。")
        except Exception as utime_e:
            logger.warning(f"无法更新文件时间戳 (os.utime 失败): {utime_e}")
        
        logger.info(f"yutto 下载成功（无登录模式）: {output_path}")
        return output_path
    else:
        logger.error(f"yutto 运行成功但未生成文件：{output_path}。")
        stdout_str = stdout_data.decode(errors='ignore').strip()
        logger.info(f"yutto 标准输出: {stdout_str[:500]}...")
        
        if "尚不支持 DASH 格式" in stdout_str:
             raise Exception("该视频尚不支持 DASH 格式。")
        
        raise Exception(f"yutto 运行成功但未能生成最终文件。Stdout: {stdout_str[:100]}")

async def process_bili_video(url, download_flag=True, quality=80, use_login=True, event=None, download_dir=None):
    """主处理函数 (现在调用 yutto) """
    logger.info(f"开始处理B站链接: {url}")
    
    video_info = None
    try:
        if REG_B23.search(url): video_info = await parse_b23(REG_B23.search(url).group())
        elif REG_BV.search(url): video_info = await parse_video(REG_BV.search(url).group())
        elif REG_AV.search(url): bvid = av2bv(REG_AV.search(url).group()); video_info = await parse_video(bvid) if bvid else None
        else: logger.warning("不支持的链接格式"); return {"error": "不支持的链接格式"}
    except Exception as e: logger.error(f"解析链接时发生错误: {str(e)}"); return {"error": f"解析链接时发生错误: {str(e)}"}
    
    if not video_info: logger.warning("解析视频信息失败"); return {"error": "解析视频信息失败"}
    stats = video_info.get("stats", {}); bvid = video_info.get("bvid")
    
    if download_dir is None:
        download_dir = "data/plugins/astrbot_plugin_video_analysis/download_videos/bili"
    cookies_file = COOKIE_FILE
    
    # 1. 检查本地缓存 (yutto生成的格式为 BVID.mp4)
    cached_file = os.path.join(download_dir, f"{bvid}.mp4")
    if os.path.exists(cached_file):
        logger.info(f"本地已存在视频文件：{cached_file}，跳过下载")
        return {"video_path": cached_file, "title": video_info["title"], "cover": video_info["cover"], "duration": video_info["duration"], "stats": stats, "bvid": bvid, "view_count": stats["view"], "like_count": stats["like"], "danmaku_count": stats["danmaku"], "coin_count": stats["coin"], "favorite_count": stats["favorite"]}

    # 2. 调用 yutto 下载 (如果需要下载)
    filename = None
    if download_flag:
        if use_login:
            logger.info("调用 yutto 进行下载 (需登录凭证)...")
            try:
                filename = await download_video_yutto(bvid, cookies_file, download_dir, quality=quality, num_workers=8)
            except Exception as e:
                # 检查是否因为 DASH 格式不支持而失败
                error_str = str(e)
                if "尚不支持 DASH 格式" in error_str:
                     logger.warning(f"yutto 高清下载失败 (DASH不支持)。不再尝试降级。错误: {e}")
                     return {"error": f"下载失败: {e}"}

                logger.warning(f"yutto 高清下载失败。错误: {e}")
                logger.info("尝试降级到360p无需登录模式...")
                # 降级到360p（质量代码16），不使用Cookie
                try:
                    filename = await download_video_yutto_no_login(bvid, download_dir, quality=16, num_workers=8)
                    logger.info(f"360p 降级下载成功: {filename}")
                except Exception as fallback_e:
                    logger.error(f"360p 降级下载也失败: {fallback_e}")
                    return {"error": f"360p 降级下载也失败: {fallback_e}"}
        else:
            # 不使用登录，直接下载360p
            logger.info("未启用登录，尝试下载360p...")
            try:
                filename = await download_video_yutto_no_login(bvid, download_dir, quality=16, num_workers=8)
            except Exception as e:
                logger.warning(f"360p下载失败。错误: {e}")
                return {"error": f"360p下载失败: {e}"}

    # 3. 检查下载结果
    if not filename and download_flag:
        logger.warning("下载失败，无法获取视频文件。")
        return {"error": "下载失败，无法获取视频文件 (未知错误)"}
        
    return {
        "title": video_info["title"], "cover": video_info["cover"],
        "duration": video_info["duration"], "stats": video_info["stats"], "video_path": filename,
        "view_count": stats["view"], "like_count": stats["like"], "danmaku_count": stats["danmaku"],
        "coin_count": stats["coin"], "favorite_count": stats["favorite"], "bvid": video_info["bvid"],
    }
    