import re

API_BY_AID = "https://api.bilibili.com/x/web-interface/view?aid={}"
API_BY_BVID = "https://api.bilibili.com/x/web-interface/view?bvid={}"

ESTIMATED_BITRATES_MBPS = {
    120: 5.5,  # 4K
    112: 2.6,  # 1080P+
    80: 1.4,  # 1080P
    64: 0.65,  # 720P
    32: 0.35,  # 480P
    16: 0.25,  # 360P
}

REG_B23 = re.compile(r"(b23\.tv|bili2233\.cn)\/[\w]+")
REG_BV = re.compile(r"BV1\w{9}")
REG_AV = re.compile(r"av\d+", re.I)
REG_BILI_LIVE = re.compile(r"(?:^https?://)?(?:m\.)?live\.bilibili\.com(?:/|$)", re.I)
REG_BILI_DYNAMIC = re.compile(
    r"(?:^https?://)?(?:"
    r"t\.bilibili\.com/\d+|"
    r"m\.bilibili\.com/(?:dynamic|opus)/\d+|"
    r"www\.bilibili\.com/(?:opus|h5/dynamic/detail)/\d+"
    r")(?:[/?#]|$)",
    re.I,
)
REG_BILI_SPACE = re.compile(
    r"(?:^https?://)?(?:"
    r"space\.bilibili\.com/\d+|"
    r"m\.bilibili\.com/space/\d+"
    r")(?:[/?#]|$)",
    re.I,
)

DEFAULT_HEADERS = {
    "referer": "https://www.bilibili.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Accept-Encoding": "gzip, deflate",
}

COOKIE_CHECK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://space.bilibili.com/",
    "Origin": "https://space.bilibili.com",
    "Accept-Encoding": "gzip, deflate",
}
