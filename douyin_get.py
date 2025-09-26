import asyncio
import os
import re
import subprocess
from astrbot.api import logger

log_callback = logger.info

# 定义插件目录路径和下载目录，以便 yt-dlp 使用
DOWNLOAD_DIR = "data/plugins/astrbot_plugin_video_analysis/download_videos/douyin"

def check_ytdlp_installed():
    """检查 yt-dlp 是否安装在 PATH 中"""
    try:
        # 检查 yt-dlp 是否存在
        subprocess.run(['yt-dlp', '--version'], check=True, capture_output=True)
        # 检查 ffmpeg 是否存在，合并视频流需要它
        subprocess.run(['ffmpeg', '-version'], check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

def get_video_id(url: str) -> str | None:
    """尝试从抖音URL中提取视频ID"""
    # 抖音短链接最终会重定向到包含 /video/ID 的长链接
    match = re.search(r'(video|v)\/(\d+)', url)
    if match:
        return match.group(2)
    # 对于 v.douyin.com 短链接，yt-dlp 可以自行处理重定向，这里只需返回原始URL
    return url


async def process_douyin_video(url: str, download_flag: bool = True, download_dir: str = DOWNLOAD_DIR):
    """
    使用 yt-dlp 下载抖音视频的核心函数。

    Args:
        url: 抖音视频的分享链接。
        download_flag: 是否执行下载（总是为 True，但保留参数以供兼容）。
        download_dir: 文件的保存目录。

    Returns:
        dict: 包含视频信息和下载路径的字典。
    """
    if not check_ytdlp_installed():
        log_callback("[FATAL] yt-dlp 或 ffmpeg 未安装，无法下载抖音视频。")
        raise Exception("yt-dlp/ffmpeg is not installed.")

    os.makedirs(download_dir, exist_ok=True)
    
    # 提取一个简化ID作为文件名基础 (yt-dlp 通常使用其内部ID)
    simple_id = get_video_id(url)
    
    # yt-dlp 输出的文件名模板。%(.ext)s 确保了扩展名的正确性
    # %(id)s 是 yt-dlp 从链接中提取的唯一 ID (可能是 video ID 或短链接ID)
    output_template = os.path.join(download_dir, f"%(id)s.%(ext)s")

    # --- 1. 构建 yt-dlp 命令 ---
    cmd = [
        'yt-dlp',
        # 格式选择：下载最佳视频和音频
        '-f', 'best', 
        
        # 输出路径和文件名模板
        '--output', output_template,
        '--force-overwrites', 
        
        # --- 优化参数：增强健壮性与模拟 ---
        '--user-agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        '--min-sleep-interval', '5',     # 失败时随机等待 5-15 秒
        '--max-sleep-interval', '15',
        '-N', '8', # 并行下载线程

        url # 目标URL
    ]
    
    log_callback(f"[DEBUG] Douyin CMD: {' '.join(cmd)}")
    log_callback("[INFO] yt-dlp 进程已成功启动。请等待下载...")

    # 2. 运行 yt-dlp 并等待
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    stdout_data, stderr_data = await process.communicate()
    
    if process.returncode != 0:
        error_output = stderr_data.decode(errors='ignore').strip()
        log_callback(f"[ERROR] yt-dlp 下载抖音失败 (Exit Code: {process.returncode})")
        log_callback(f"[ERROR] yt-dlp 错误输出: {error_output[:1000]}...")
        # 抖音下载失败的返回值是 None
        return None 

    # 3. 查找最终文件并返回路径
    # 查找下载目录中以视频ID开头，且扩展名为 mp4/mkv 等的最新文件
    downloaded_files = [f for f in os.listdir(download_dir) if os.path.isfile(os.path.join(download_dir, f))]
    
    # 尝试找到匹配 ID 的最新文件
    final_file = None
    for filename in downloaded_files:
        # yt-dlp可能会生成 video ID 或其他格式 ID，这里用简单的 ID 检查
        if simple_id and simple_id in filename:
            final_file = filename
            break
        # 如果无法提取 simple_id，则尝试寻找最近创建的视频文件
        if not final_file and (filename.endswith('.mp4') or filename.endswith('.mkv')):
             final_file = filename # 随便找一个，直到我们找到更好的匹配

    if final_file:
        final_path = os.path.join(download_dir, final_file)
        log_callback(f"[INFO] yt-dlp 抖音下载成功: {final_path}")
        
        # ⚠️ 警告: yt-dlp 下载抖音视频不会返回完整的元数据（标题、观看次数等）。
        # 我们只能返回一个最简化的结构，防止 main.py 崩溃。
        return {
            "title": f"抖音视频 ({simple_id or '未知'})",
            "cover": None,
            "duration": 0,
            "stats": {"view": 'N/A', "like": 'N/A', "danmaku": 'N/A', "coin": 'N/A', "favorite": 'N/A'},
            "video_path": final_path,
            "bvid": simple_id,
            "view_count": 0, "like_count": 0, "danmaku_count": 0, "coin_count": 0, "favorite_count": 0
        }
    
    log_callback("[ERROR] yt-dlp 运行成功但未找到最终文件。")
    return None