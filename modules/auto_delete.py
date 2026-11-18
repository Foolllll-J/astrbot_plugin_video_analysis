"""插件自有媒体文件的归属标记、使用保护与文件级清理；只逐文件删除，不整目录 rmtree。"""

from __future__ import annotations

import os
import threading
import time
from collections import Counter
from contextlib import contextmanager
from typing import Iterable, Optional

from astrbot.api import logger

MARKER_FILE_NAME = ".astrbot_video_analysis"

# 仅清理本插件产出的媒体与临时文件。
_OWNED_MEDIA_EXTS = {
    ".mp4",
    ".mkv",
    ".mov",
    ".avi",
    ".flv",
    ".webm",
    ".wmv",
    ".ts",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
    ".bmp",
    ".heic",
    ".m4a",
    ".mp3",
    ".aac",
    ".wav",
    ".part",
}


def ensure_owned_dir(directory: str) -> None:
    """写入目录归属标记，标识该目录由本插件创建。"""
    if not directory:
        return
    try:
        os.makedirs(directory, exist_ok=True)
        marker = os.path.join(directory, MARKER_FILE_NAME)
        if not os.path.isfile(marker):
            with open(marker, "w", encoding="utf-8") as f:
                f.write("astrbot_plugin_video_analysis\n")
    except OSError as e:
        logger.warning(f"写入缓存目录归属标记失败 {directory}: {e}")


def has_marker(directory: str) -> bool:
    return bool(directory) and os.path.isfile(os.path.join(directory, MARKER_FILE_NAME))


class FileUseGuard:
    """记录使用中的文件与下载目录（线程安全，清理在线程中执行）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._files: Counter[str] = Counter()
        self._dirs: Counter[str] = Counter()

    @contextmanager
    def protect(self, paths: Iterable[str]):
        normalized = [os.path.abspath(p) for p in paths if p]
        with self._lock:
            for path in normalized:
                self._files[path] += 1
        try:
            yield
        finally:
            with self._lock:
                for path in normalized:
                    self._files[path] -= 1
                    if self._files[path] <= 0:
                        self._files.pop(path, None)

    @contextmanager
    def download_dir(self, directory: str):
        normalized = os.path.abspath(directory) if directory else ""
        with self._lock:
            if normalized:
                self._dirs[normalized] += 1
        try:
            yield
        finally:
            with self._lock:
                if normalized:
                    self._dirs[normalized] -= 1
                    if self._dirs[normalized] <= 0:
                        self._dirs.pop(normalized, None)

    def is_protected(self, path: str) -> bool:
        abs_path = os.path.abspath(path)
        with self._lock:
            if self._files.get(abs_path, 0) > 0:
                return True
            for directory, count in self._dirs.items():
                if count > 0 and abs_path.startswith(directory + os.sep):
                    return True
        return False


_FILE_USE_GUARD = FileUseGuard()


def get_file_use_guard() -> FileUseGuard:
    return _FILE_USE_GUARD


def delete_old_files(
    folder_path: str,
    time_threshold_minutes: int,
    guard: Optional[FileUseGuard] = None,
) -> list[str]:
    """删除本插件标记的目录中已过期、且未被使用的媒体文件。

    返回被删除文件的绝对路径，供缓存条目联动清理。
    """
    if not os.path.isdir(folder_path) or not has_marker(folder_path):
        return []

    threshold_seconds = max(0, time_threshold_minutes) * 60
    cutoff = time.time() - threshold_seconds
    deleted: list[str] = []
    active_guard = guard or get_file_use_guard()

    try:
        with os.scandir(folder_path) as entries:
            for entry in entries:
                # 跳过隐藏/标记文件；子目录不递归删除
                if entry.name.startswith("."):
                    continue
                try:
                    if not (entry.is_file(follow_symlinks=False) or entry.is_symlink()):
                        continue
                    ext = os.path.splitext(entry.name)[1].lower()
                    if ext not in _OWNED_MEDIA_EXTS:
                        continue
                    stat = entry.stat()
                    if stat.st_mtime > cutoff:
                        continue
                    abs_path = os.path.abspath(entry.path)
                    if active_guard.is_protected(abs_path):
                        continue
                    os.remove(entry.path)
                    deleted.append(abs_path)
                except OSError as e:
                    logger.warning(f"清理过期媒体文件失败 {entry.path}: {e}")
    except OSError as e:
        logger.error(f"清理文件夹失败 {folder_path}: {e}")

    if deleted:
        logger.debug(f"清理完成，共删除 {len(deleted)} 个本插件过期文件")
    return deleted
