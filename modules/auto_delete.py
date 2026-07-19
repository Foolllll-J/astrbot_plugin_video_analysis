import os
import shutil
import time

from astrbot.api import logger


def delete_old_files(folder_path: str, time_threshold_minutes: int) -> int:
    """删除指定文件夹中超过时间阈值的旧文件和目录"""
    if not os.path.isdir(folder_path):
        return 0

    time_threshold_seconds = time_threshold_minutes * 60
    current_time = time.time()
    deleted_count = 0

    try:
        with os.scandir(folder_path) as entries:
            for entry in entries:
                try:
                    entry_stat = entry.stat()
                    if current_time - entry_stat.st_mtime <= time_threshold_seconds:
                        continue

                    if entry.is_file() or entry.is_symlink():
                        os.remove(entry.path)
                        deleted_count += 1
                    elif entry.is_dir():
                        shutil.rmtree(entry.path)
                        deleted_count += 1
                except OSError as e:
                    logger.error(f"删除项目失败 {entry.path}: {e}")
    except Exception as e:
        logger.error(f"清理文件夹失败 {folder_path}: {e}")
        return deleted_count

    if deleted_count > 0:
        logger.debug(f"清理完成，共删除 {deleted_count} 个过期项目")
    return deleted_count
