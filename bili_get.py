import asyncio
import aiohttp
import re
import os
import aiofiles
import json
import time
import qrcode
from PIL import Image
import base64
from io import BytesIO
from urllib.parse import unquote
from astrbot.api import logger
import requests
import subprocess 
import shutil 

# --- 常量和设置 ---
COOKIE_FILE = "data/plugins/astrbot_plugin_video_analysis/bili_cookies.json"
os.makedirs(os.path.dirname(COOKIE_FILE), exist_ok=True)

log_callback = logger.info
COOKIE_VALID = None

def set_log_callback(callback):
    """设置日志回调函数"""
    global log_callback
    log_callback = callback

CONFIG = {
    "VIDEO": {"enable": True, "send_link": False, "send_video": True}
}

# --- 清晰度映射辅助函数 ---
def map_quality_to_height(quality_code: int) -> int:
    """将 B站质量代码映射为 yt-dlp 的最大高度限制（p）。"""
    if quality_code >= 120: return 2160 # 4K
    if quality_code >= 112: return 1080 # 1080P+
    if quality_code >= 80: return 1080  # 1080P
    if quality_code >= 64: return 720   # 720P
    if quality_code >= 32: return 480   # 480P
    if quality_code >= 16: return 360   # 360P
    return 1080 # 默认最高质量

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
    headers = {"referer": "https://www.bilibili.com/", "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"}
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
        log_callback("[DEBUG] 未找到Cookie文件或Cookie文件为空，需要登录")
        return False
    required_fields = {"SESSDATA": lambda v: len(v) > 30 and ',' in v, "bili_jct": lambda v: len(v) == 32, "DedeUserID": lambda v: v.isdigit()}
    for field, validator in required_fields.items():
        if field not in cookies or not validator(str(cookies[field])):
            log_callback(f"[DEBUG] Cookie字段验证失败: {field} = {cookies.get(field)}")
            return False
    url = "https://api.bilibili.com/x/member/web/account"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "Referer": "https://space.bilibili.com/", "Origin": "https://space.bilibili.com", "Cookie": "; ".join([f"{k}={v}" for k, v in cookies.items()])}
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
        log_callback(f"Cookie已保存到: {COOKIE_FILE}")
        return True
    except Exception as e:
        log_callback(f"保存Cookie失败: {str(e)}")
        return False

async def load_cookies():
    """从文件加载Cookie"""
    if not os.path.exists(COOKIE_FILE):
        log_callback(f"Cookie文件不存在: {COOKIE_FILE}")
        return None
    try:
        async with aiofiles.open(COOKIE_FILE, "r", encoding="utf-8") as f:
            content = await f.read()
            if not content.strip(): log_callback("Cookie文件为空"); return None
            cookies = json.loads(content); return cookies
    except json.JSONDecodeError: log_callback("Cookie文件格式错误"); return None
    except Exception as e: log_callback(f"加载Cookie失败: {str(e)}"); return None

async def generate_qrcode():
    """生成B站登录二维码（新版API）"""
    url = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    data = await bili_request(url)
    if data.get("code") != 0: print(f"获取二维码失败: {data.get('message')}"); return None
    qr_data = data["data"]; qr_url = qr_data["url"]; qrcode_key = qr_data["qrcode_key"]
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=10, border=4)
    qr.add_data(qr_url); qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = BytesIO(); img.save(buffered, format="PNG"); img_str = base64.b64encode(buffered.getvalue()).decode()
    image_dir = "data/plugins/astrbot_plugin_video_analysis/image"; os.makedirs(image_dir, exist_ok=True)
    image_path = os.path.join(image_dir, "bili_login_qrcode.png")
    with open(image_path, "wb") as f: f.write(base64.b64decode(qr_data["image_base64"]))
    print(f"\n如果上方二维码显示异常，请查看二维码文件: {image_path}"); logger.info(f"二维码图片已保存到: {image_path}")
    return {"qrcode_key": qrcode_key, "image_base64": img_str, "url": qr_url}

async def check_login_status(qrcode_key):
    """检查登录状态（新版API）"""
    url = f"https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key={qrcode_key}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                result = await response.json(); return result
    except aiohttp.ClientError: return {"code": -1, "message": "检查登录状态失败"}

async def bili_login(event=None):
    """B站扫码登录流程（新版API）"""
    log_callback("正在生成B站登录二维码..."); qr_data = await generate_qrcode()
    if not qr_data: return None
    log_callback("\n请使用B站APP扫描以下二维码登录:")
    qrcode_key = qr_data["qrcode_key"]
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L, box_size=1, border=1)
    qr.add_data(qr_data["url"]); qr.make(fit=True)
    matrix = qr.get_matrix(); qr_text = "\n======= B站登录二维码 =======\n"
    for row in matrix:
        line = "";
        for cell in row: line += "██" if cell else "  "
        qr_text += line + "\n"
    qr_text += "==========================\n"
    print(qr_text); from astrbot.api import logger; logger.info("B站登录二维码已显示在控制台"); logger.info(qr_text)
    image_path = os.path.join("data/plugins/astrbot_plugin_video_analysis/image", "bili_login_qrcode.png")
    print(f"\n如果上方二维码显示异常，请查看二维码文件: {image_path}"); logger.info(f"二维码图片已保存到: {image_path}")
    logger.info("如果无法扫描，可复制下方base64码用在线工具解析:"); logger.info(f"data:image/png;base64,{qr_data['image_base64'][:50]}...")
    login_task = asyncio.create_task(check_login_status_loop(qrcode_key)); return login_task

async def check_login_status_loop(qrcode_key):
    """循环检查登录状态，直到登录成功或超时"""
    logger.info("等待登录...（最多40秒）")
    for _ in range(40):
        await asyncio.sleep(1); status = await check_login_status(qrcode_key)
        if status.get("code") == 0:
            data = status.get("data", {})
            if data.get("code") == 0:
                log_callback("\n登录成功!")
                try:
                    url = data.get("url", ""); cookies = {}
                    if "?" in url:
                        url_params = url.split("?")[1]
                        for param in url_params.split("&"):
                            if "=" in param:
                                key, value = param.split("=", 1)
                                useful_keys = ["_uuid", "DedeUserID", "DedeUserID__ckMd5", "SESSDATA", "bili_jct", "bili_ticket", "bili_ticket_expires", "CURRENT_FNVAL", "CURRENT_QUALITY", "enable_feed_channel", "enable_web_push", "header_theme_version", "home_feed_column", "LIVE_BUVID", "PVID", "browser_resolution", "buvid_fp", "buvid3", "fingerprint"]
                                if key in useful_keys: cookies[key] = unquote(value)
                        if not cookies.get("SESSDATA") or not cookies.get("DedeUserID"): raise ValueError("获取的Cookie格式异常")
                        log_callback(f"获取到的Cookie: {cookies}"); await save_cookies_dict(cookies); return cookies
                    else: raise ValueError("URL格式异常，无法提取参数")
                except Exception as e: log_callback(f"登录异常: {str(e)}"); log_callback(f"原始响应数据: {data}"); return None
            elif data.get("code") == -2: log_callback("\n二维码已过期，请重新获取"); return None
            elif data.get("code") == -4 or data.get("code") == -5: log_callback("请在手机上确认登录")
    log_callback("\n登录超时，请重试"); return None

# --- 【核心下载函数：YT-DLP 逻辑】 ---

def check_ytdlp_installed():
    """检查 yt-dlp 是否安装在 PATH 中"""
    try:
        subprocess.run(['yt-dlp', '--version'], check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

async def download_video_ytdlp(bvid, cookies_file, download_dir, quality=80, num_threads=8):
    """
    使用 yt-dlp 命令下载并合并视频。
    """
    if not check_ytdlp_installed():
        log_callback("[FATAL] yt-dlp 未安装或不在系统 PATH 中。无法进行下载。")
        raise Exception("yt-dlp is not installed or not found in PATH.")

    os.makedirs(download_dir, exist_ok=True)
    
    # yt-dlp 下载链接，输出文件名为 BV号.mp4
    output_template = os.path.join(download_dir, f"{bvid}.mp4")
    
    # 1. 转换 Cookie 为 Netscape 格式并写入临时文件
    try:
        async with aiofiles.open(cookies_file, "r", encoding="utf-8") as f:
            json_cookies = json.loads(await f.read())
    except Exception as e:
        log_callback(f"[ERROR] 无法读取或解析 JSON Cookie 文件: {cookies_file}. 错误: {e}")
        raise Exception("无法读取 Cookie 文件，请检查格式。")

    netscape_cookie_path = os.path.join(download_dir, "bili_netscape_temp.txt")
    netscape_header = "# Netscape HTTP Cookie File\n"
    netscape_format = "{domain}\t{flag}\t{path}\t{secure}\t{expiration}\t{name}\t{value}\n"
    netscape_entries = netscape_header
    cookies_to_convert = ['SESSDATA', 'bili_jct', 'DedeUserID', 'DedeUserID__ckMd5']

    for name in cookies_to_convert:
        if name in json_cookies:
            netscape_entries += netscape_format.format(
                domain='.bilibili.com', flag='TRUE', path='/', secure='TRUE', expiration='0',
                name=name, value=json_cookies[name]
            )
        
    async with aiofiles.open(netscape_cookie_path, "w", encoding="utf-8") as f:
        await f.write(netscape_entries)
    
    # 2. 清理旧的缓存文件
    if os.path.exists(output_template):
        os.remove(output_template)

    # 3. 动态构建格式选择器
    max_height = map_quality_to_height(quality)
    log_callback(f"[DEBUG] 目标质量代码 {quality} 映射到最大高度 {max_height}p。")
    format_selector = f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/best"


    # 4. 构建 yt-dlp 命令 (包含加速和模拟参数)
    cmd = [
        'yt-dlp',
        '--cookies', netscape_cookie_path, # <-- 使用 Netscape 格式的临时文件
        '-f', format_selector, # 格式选择
        '--merge-output-format', 'mp4', 
        '-N', str(num_threads), # 并行下载线程
        '--output', output_template,
        '--force-overwrites', # 覆盖已有文件
        
        # --- 优化参数：增强健壮性与输出 ---
        '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        '--min-sleep-interval', '5', # 失败时随机等待 5-15 秒
        '--max-sleep-interval', '15', # 随机等待的最大值 (5 到 15 秒)
        f'https://www.bilibili.com/video/{bvid}'
    ]
    
    log_callback(f"[DEBUG] yt-dlp CMD: {' '.join(cmd)}")
    
    # 5. 运行 yt-dlp (使用标准的 asyncio 捕获)
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    # 6. 立即输出“处理中”日志
    log_callback("[INFO] yt-dlp 进程已成功启动。请等待下载和合成...")

    # 7. 捕获输出和等待
    # 实时打印 yt-dlp 的输出
    # 仅在 process.communicate() 阻塞时，外部程序才能进行 I/O
    # 我们信任 yt-dlp 会自己处理进度，这里只在结束后读取
    stdout_data, stderr_data = await process.communicate()
    
    # 8. 检查退出码和清理
    os.remove(netscape_cookie_path) # 始终清理临时文件

    if process.returncode != 0:
        error_output = stderr_data.decode(errors='ignore').strip()
        log_callback(f"[ERROR] yt-dlp 命令行执行完毕。退出码: {process.returncode}。")
        log_callback(f"[ERROR] yt-dlp 错误输出: {error_output[:1000]}...")
        raise Exception(f"yt-dlp 下载失败，请检查 FFmpeg 和 yt-dlp 日志。")

    log_callback(f"[INFO] yt-dlp 命令行执行完毕。退出码: {process.returncode}。正在检查文件。")

    # 9. 检查最终文件是否存在
    if os.path.exists(output_template):
        # --- 核心修复：强制更新文件时间戳为当前时间 ---
        try:
            os.utime(output_template, None) 
            log_callback(f"[INFO] 文件时间戳已更新至当前时间，防止被自动清理。")
        except Exception as utime_e:
            log_callback(f"[WARN] 无法更新文件时间戳 (os.utime 失败): {utime_e}")
        # --- 核心修复结束 ---
        
        log_callback(f"[INFO] yt-dlp 下载并合成成功: {output_template}")
        return output_template
    else:
        log_callback(f"[ERROR] yt-dlp 运行成功但未生成文件：{output_template}")
        raise Exception("yt-dlp 运行成功但未能生成最终文件。")


# --- 核心入口函数 process_bili_video 替换下载逻辑 ---
# 它现在是唯一处理下载的函数
async def process_bili_video(url, download_flag=True, quality=80, use_login=True, event=None):
    """主处理函数 (现在调用 yt-dlp) """
    log_callback(f"[INFO] process_bili_video: 开始处理B站链接: {url}")
    
    # 确保链接解析逻辑存在并成功获取 video_info, bvid
    video_info = None
    try:
        if REG_B23.search(url): video_info = await parse_b23(REG_B23.search(url).group())
        elif REG_BV.search(url): video_info = await parse_video(REG_BV.search(url).group())
        elif REG_AV.search(url): bvid = av2bv(REG_AV.search(url).group()); video_info = await parse_video(bvid) if bvid else None
        else: log_callback("不支持的链接格式"); return None
    except Exception as e: log_callback(f"解析链接时发生错误: {str(e)}"); return None
    
    if not video_info: log_callback("解析视频信息失败"); return None
    stats = video_info.get("stats", {}); bvid = video_info.get("bvid")
    
    download_dir = "data/plugins/astrbot_plugin_video_analysis/download_videos/bili"
    cookies_file = COOKIE_FILE
    
    # 1. 检查本地缓存 (yt-dlp生成的格式为 BVID.mp4)
    cached_file = os.path.join(download_dir, f"{bvid}.mp4")
    if os.path.exists(cached_file):
        log_callback(f"本地已存在视频文件：{cached_file}，跳过下载")
        return {"video_path": cached_file, "title": video_info["title"], "cover": video_info["cover"], "duration": video_info["duration"], "stats": stats, "bvid": bvid, "view_count": stats["view"], "like_count": stats["like"], "danmaku_count": stats["danmaku"], "coin_count": stats["coin"], "favorite_count": stats["favorite"]}

    # 2. 调用 yt-dlp 下载 (如果需要下载)
    filename = None
    if download_flag and use_login:
        log_callback("[INFO] 调用 yt-dlp 进行并行下载和合成 (需登录凭证)...")
        try:
            # 传递 quality 参数
            filename = await download_video_ytdlp(bvid, cookies_file, download_dir, quality=quality)
        except Exception as e:
            # yt-dlp 下载失败，可能是反爬虫，直接返回 None 让 main.py 重试
            log_callback(f"[WARN] yt-dlp 高清下载失败。错误: {e}")
            return None 

    # 3. 如果 yt-dlp 失败，或者 use_login=False，返回 None
    if not filename and download_flag:
        log_callback("[WARN] 未开启登录或下载失败，无法获取视频文件。")
        return None
        
    return {
        "direct_url": None, "title": video_info["title"], "cover": video_info["cover"],
        "duration": video_info["duration"], "stats": video_info["stats"], "video_path": filename,
        "view_count": stats["view"], "like_count": stats["like"], "danmaku_count": stats["danmaku"],
        "coin_count": stats["coin"], "favorite_count": stats["favorite"], "bvid": video_info["bvid"],
    }