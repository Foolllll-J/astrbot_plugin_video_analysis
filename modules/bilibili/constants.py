import re

API_BY_AID = "https://api.bilibili.com/x/web-interface/view?aid={}"
API_BY_BVID = "https://api.bilibili.com/x/web-interface/view?bvid={}"
PLAYURL_API = "https://api.bilibili.com/x/player/playurl"
PLAYURL_WBI_API = "https://api.bilibili.com/x/player/wbi/playurl"
VIEW_API = "https://api.bilibili.com/x/web-interface/view"
VIEW_WBI_API = "https://api.bilibili.com/x/web-interface/wbi/view"
NAV_API = "https://api.bilibili.com/x/web-interface/nav"
BUVID_API = "https://api.bilibili.com/x/frontend/finger/spi"

# WBI mixin key 混排表（签名算法见 utils.py::sign_wbi_params）。
MIXIN_KEY_ENC_TAB = [
    46,
    47,
    18,
    2,
    53,
    62,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
]
WBI_KEY_TTL_SECONDS = 6 * 60 * 60

ESTIMATED_BITRATES_MBPS = {
    120: 5.5,  # 4K
    116: 2.6,  # 1080P60（与 112 同级码率近似）
    112: 2.6,  # 1080P+
    80: 1.4,  # 1080P
    64: 0.65,  # 720P
    32: 0.35,  # 480P
    16: 0.25,  # 360P
}

REG_B23 = re.compile(r"(b23\.tv|bili2233\.cn)\/[\w]+")
REG_BV = re.compile(r"(?<![A-Za-z0-9])BV1\w{9}(?![A-Za-z0-9])")
REG_AV = re.compile(r"(?<![A-Za-z0-9])av\d+(?![A-Za-z0-9])", re.I)
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
