REG_TIEBA = r"https?://tieba\.baidu\.com/p/\d+"

API_TBS = "http://tieba.baidu.com/dc/common/tbs"
API_PAGE = "https://tieba.baidu.com/c/f/pb/page"
API_PAGE_PC = "https://tieba.baidu.com/c/f/pb/page_pc"
SIGN_SALT = "tiebaclient!!!"
PAGE_PC_SALT = "36770b1f34c9bbf2e7d1a99d2b82fa9e"

TIEBA_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

HEADERS = {
    "User-Agent": TIEBA_UA,
}

DOWNLOAD_HEADERS = {
    "User-Agent": TIEBA_UA,
    "Referer": "https://tieba.baidu.com/",
}

TIMEOUT = 30

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
