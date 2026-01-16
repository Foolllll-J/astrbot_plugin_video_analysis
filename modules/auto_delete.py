import os
import time
from typing import Optional
from astrbot.api import logger

def delete_old_files(folder_path: str, time_threshold_minutes: int) -> int:
    """
    删除指定文件夹中超过时间阈值的旧文件和目录
    
    Args:
        folder_path: 要清理的文件夹路径
        time_threshold_minutes: 时间阈值（分钟）
        
    Returns:
        删除的文件和目录数量
    """
    try:
        os.makedirs(folder_path, exist_ok=True)
        time_threshold_seconds = time_threshold_minutes * 60
        current_time = time.time()
        deleted_count = 0
        
        for item_name in os.listdir(folder_path):
            item_path = os.path.join(folder_path, item_name)
            
            try:
                # 获取修改时间
                item_time = os.path.getmtime(item_path)
                
                if current_time - item_time > time_threshold_seconds:
                    if os.path.isfile(item_path):
                        # 删除文件
                        os.remove(item_path)
                        logger.info(f"已删除过期文件: {item_path}")
                        deleted_count += 1
                    elif os.path.isdir(item_path):
                        # 删除目录及其内容
                        import shutil
                        shutil.rmtree(item_path)
                        logger.info(f"已删除过期目录: {item_path}")
                        deleted_count += 1
            except OSError as e:
                logger.error(f"删除项目失败 {item_path}: {e}")
        
        if deleted_count > 0:
            logger.info(f"清理完成，共删除 {deleted_count} 个过期项目")
        
        return deleted_count
        
    except Exception as e:
        logger.error(f"清理文件夹失败 {folder_path}: {e}")
        return 0


if __name__ == "__main__":
    # 测试用的硬编码路径，实际使用时应该从配置中获取
    TEST_FOLDER_PATH = "data/plugins/astrbot_plugin_video_analysis/download_videos/douyin"
    TEST_TIME_THRESHOLD = 60
    delete_old_files(TEST_FOLDER_PATH, TEST_TIME_THRESHOLD)