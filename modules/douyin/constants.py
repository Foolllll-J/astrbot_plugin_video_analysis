# API 端点
DOUYIN_DOMAIN = "https://www.douyin.com"
POST_DETAIL = f"{DOUYIN_DOMAIN}/aweme/v1/web/aweme/detail/"

# 默认请求头（PC Web）
PC_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/90.0.4430.212 Safari/537.36"
)
PC_HEADERS = {
    "Accept-Language": "zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2",
    "User-Agent": PC_USER_AGENT,
    "Referer": "https://www.douyin.com/",
}

# 第三方 API 兜底默认头
API_FALLBACK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.douyin.com/",
}

# 文件下载头
DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.douyin.com/",
}

# VideoData API 清晰度降级顺序
# 默认超时
DEFAULT_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 300
