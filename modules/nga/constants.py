REG_NGA = r"https?://(?:bbs\.nga\.cn|nga\.178\.com|ngabbs\.com)/read\.php\?tid=(\d+)"

API_READ = "https://bbs.nga.cn/read.php"
API_NUKE = "https://nga.178.com/nuke.php"

NGA_UA = "NGA_WP_JW/(;WINDOWS)"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

HEADERS = {
    "User-Agent": NGA_UA,
}

DOWNLOAD_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Referer": "https://bbs.nga.cn/",
}

TIMEOUT = 30

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
