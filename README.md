<div align="center">

# 🎬 媒体解析助手

<i>🔮 B站抖音解析的最优方案</i>

![License](https://img.shields.io/badge/license-AGPL--3.0-green?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10+-blue?style=flat-square&logo=python&logoColor=white)
![AstrBot](https://img.shields.io/badge/framework-AstrBot-ff6b6b?style=flat-square)

</div>

## 🧾 简介

一款为 [**AstrBot**](https://github.com/AstrBotDevs/AstrBot) 设计的多平台分享链接解析插件，支持自动识别并解析 **B站、抖音、小红书、贴吧、NGA** 的分享链接，以媒体形式智能发送视频、图片、论坛回帖等内容。

---

## ✨ 功能

- **多平台支持**：支持 B站、抖音、小红书、贴吧、NGA 等平台链接的自动解析。
- **论坛帖子解析**：支持贴吧和 NGA 帖子/楼层的完整解析，以合并转发形式发送回帖内容。
- **清晰度智能降级**：可根据设置的最大视频大小来动态调整解析长视频时使用的清晰度。
- **解析限制保护**：支持会话白名单、群等级要求、短时间限频冷却与通过屏蔽关键词跳过指定消息解析。
- **表情回应**：通过贴表情实时反馈解析状态，支持自定义开启/关闭表情互动。

---

## 🎮 指令

- **`/bili_login`** - 触发 B站账号登录流程，接收二维码图片进行扫码登录
- **`/bili_check`** - 检查当前 B站 Cookie 是否有效
---

## 🚀 安装

1. 由于插件依赖于外部命令行工具，请确保您的 Docker 容器内已安装它们：

```bash
# 1. 进入容器终端
docker exec -it [你的容器ID或名称] /bin/bash

# 2. 安装 FFmpeg
apt-get update && apt-get install ffmpeg -y

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

---

## ⚙️ 配置

首次加载后，请在 AstrBot 后台 -> 插件 页面找到本插件进行设置。所有配置项都有详细的说明和提示。

---

## 📅 更新日志

详见 [CHANGELOG](./CHANGELOG.md)

---

## 🙏 参考与致谢

本项目参考或得益于以下项目，特此感谢：

- [yutto](https://github.com/yutto-dev/yutto)
- [ParseHub](https://github.com/z-mio/ParseHub)
- [抖音/TikTok API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API)
- [XHS-Downloader](https://github.com/JoeanAmier/XHS-Downloader)
- [aiotieba](https://github.com/Starry-OvO/aiotieba)
- [视频解析插件](https://github.com/miaoxutao123/astrbot_plugin_videos_analysis)
- [流媒体聚合解析器](https://github.com/drdon1234/astrbot_plugin_media_parser)
---

## ❤️ 支持

- [AstrBot 帮助文档](https://astrbot.app)
- 如果您在使用中遇到问题，欢迎在本仓库提交 [Issue](https://github.com/Foolllll-J/astrbot_plugin_video_analysis/issues)。

---

<div align="center">

**如果本插件对你有帮助，欢迎点个 ⭐ Star 支持一下！**

</div>
