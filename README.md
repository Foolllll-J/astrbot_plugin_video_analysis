# 视频解析助手

![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.11+-blue.svg)
![AstrBot](https://img.shields.io/badge/framework-AstrBot-orange)

一个为 [AstrBot](https://astrbot.app) 设计的视频平台（如 Bilibili 和抖音）分享链接解析插件。通过采用 **yt-dlp** 等工具，稳定、快速地解析视频链接并发送视频给用户。

---


### ✨ 功能

- **多平台支持**：支持 Bilibili (B站) 和 Douyin (抖音) 短链接的自动解析。
- **高性能下载**：调用 **`yt-dlp`** 命令行工具，实现高并发分段下载和自动 FFmpeg 合并。
- **双重重试机制**：自动重试下载/解析失败和消息发送失败，提高稳定性。
- **资源管理**：异步清理超过设定阈值的临时文件，防止占用磁盘空间。

---

### ⚙️ 安装

1. 由于插件依赖于外部命令行工具，请确保您的 Docker 容器内已安装它们：

```bash
# 1. 进入容器终端
docker exec -it [你的容器ID或名称] /bin/bash

# 2. 安装 FFmpeg & yt-dlp
apt-get update && apt-get install ffmpeg yt-dlp -y
```

2. 下载本仓库。
3. 将整个 `astrbot_plugin_file_checker` 文件夹放入 `astrbot` 的 `plugins` 目录中。
4. 重启 AstrBot。

### 📝 版本记录

* **v0.2**
  * 新增 **抖音** 视频分享链接的识别和解析。
* **v0.1**
  * 实现 **BiliBili** 视频分享链接的识别和解析。

### 🙏 致谢与参考

本项目在开发过程中，参考并得益于以下项目，特此感谢：

- **原始代码参考**：`https://github.com/miaoxutao123/astrbot_plugin_videos_analysis`
- **高性能下载核心**：`https://github.com/yt-dlp/yt-dlp`
- **抖音解析服务**：`https://github.com/Evil0ctal/Douyin_TikTok_Download_API`

