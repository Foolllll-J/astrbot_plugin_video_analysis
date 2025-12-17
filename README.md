# 🎬 视频解析助手

![License](https://img.shields.io/badge/license-AGPL--3.0-green?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10+-blue?style=flat-square&logo=python&logoColor=white)
![AstrBot](https://img.shields.io/badge/framework-AstrBot-ff6b6b?style=flat-square)

一个为 [AstrBot](https://astrbot.app) 设计的视频平台（如 Bilibili 和抖音）分享链接解析插件。通过采用 **yutto** 等工具，稳定、快速地解析视频链接并发送视频给用户。

---

### ✨ 功能

- **多平台支持**：支持 Bilibili (B站) 和 Douyin (抖音) 短链接的自动解析。
- **高性能下载**：调用 **`yutto`** 命令行工具，实现高并发下载和自动 FFmpeg 合并。
- **清晰度智能降级**：可根据设置的最大视频大小来动态调整解析长视频时使用的清晰度。
- **资源管理**：异步清理超过设定阈值的临时文件，防止占用磁盘空间。
- **便捷登录**：支持通过指令发送登录二维码，可直接扫码登录 B站账号。

---

### 🎮 指令列表

- **`/bili_login`** - 触发 B站账号登录流程，接收二维码图片进行扫码登录
- **`/bili_check`** - 检查当前 B站 Cookie 是否有效

---

### ⚙️ 安装

1. 由于插件依赖于外部命令行工具，请确保您的 Docker 容器内已安装它们：

```bash
# 1. 进入容器终端
docker exec -it [你的容器ID或名称] /bin/bash

# 2. 安装 FFmpeg
apt-get update && apt-get install ffmpeg-y

# 3. 安装 ppix
pip install pipx

# 4. 添加路径
pipx ensurepath

# 5. 安装 yutto
pipx install yutto
```

2. 下载本仓库。
3. 将整个 `astrbot_plugin_video_analysis` 文件夹放入 `astrbot` 的 `plugins` 目录中。
4. 重启 AstrBot。

### 📝 版本记录

* **v1.0**
  * 新增 **抖音图片作品** 的解析，自动以合并转发形式发送图片
  * 新增 `/bili_login` 和 `/bili_check` 指令
  * 新增 B站未登录场景的视频解析
* **v0.2**
  * 新增 **抖音** 视频分享链接的识别和解析。
* **v0.1**
  * 实现 **BiliBili** 视频分享链接的识别和解析。

### 🙏 致谢与参考

本项目在开发过程中，参考并得益于以下项目，特此感谢：

- **原始代码参考**：[AstrBot 视频解析插件](https://github.com/miaoxutao123/astrbot_plugin_videos_analysis)
- **B站解析核心**：[yutto](https://github.com/yutto-dev/yutto)
- **抖音解析服务**：[抖音/TikTok API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API)

