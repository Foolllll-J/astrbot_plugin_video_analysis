import asyncio
import base64
import json
import os
import random
import re
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, unquote

import httpx

from astrbot.api import logger

try:
    from xhshow import Xhshow
except Exception:  # 可选依赖，缺失或不可用时跳过游客 session，退回匿名请求
    Xhshow = None
    logger.debug("xhshow 不可用，小红书游客 session 功能已跳过")

from .model import XiaohongshuParseResult
from .constants import ANDROID_UA, PC_UA, BASE_HEADERS, DEFAULT_TIMEOUT


_INIT_STATE_RE = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", re.DOTALL
)

_PICASSO_RE = re.compile(r"picasso-static|fe-platform")

# 登录墙/风控页判定特征。正常笔记页 JS 亦含 "login"，不可作为判定依据。
LOGIN_URL_MARKERS = ("/login", "website-login", "/captcha")
LOGIN_HTML_MARKERS = ("fe-login", "verifyuuid", "登录后查看", "请输入验证码")

# 笔记路径中的 24 位 note id：/explore/、/discovery/item/、/note/
NOTE_ID_RE = re.compile(r"/(?:explore|discovery/item|note)/([0-9a-fA-F]{24})")

# __INITIAL_STATE__ 中位于值位置的 JS undefined，如 "firstVisitUrl":undefined
_JS_UNDEFINED_RE = re.compile(r"(?<=[:,\[])\s*undefined\b")

# 值位置的 JS 集合字面量，如 "AiNoteDetailStore":{"noteDetailMap":new Map([])}
_JS_COLLECTION_RE = re.compile(r"new (?:Map|Set)\([^)]*\)")

# 游客 session 获取接口与缓存时长（秒）
_GUEST_ACTIVATE_URI = "/api/sns/web/v1/login/activate"
_GUEST_TTL = 1800
_guest_lock = asyncio.Lock()
_guest_cookies: dict[str, str] = {}
_guest_ts = 0.0


def _loads_state(raw: str) -> dict:
    """解析 __INITIAL_STATE__，兼容 undefined 与 new Map([])/new Set([]) 等 JS 字面量。"""
    text = _JS_UNDEFINED_RE.sub(" null", raw)
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        sanitized = _JS_COLLECTION_RE.sub("{}", text)
        if sanitized == text:
            raise
        return json.loads(sanitized, strict=False)


# 页面请求最小间隔：匿名抓取的限额约为 10 秒内 5 次请求，超出即跳登录页
_FETCH_LOCK = asyncio.Lock()
_MIN_FETCH_INTERVAL = 2.0
_last_fetch_ts = 0.0


async def _throttle_fetch() -> None:
    global _last_fetch_ts
    async with _FETCH_LOCK:
        gap = _MIN_FETCH_INTERVAL - (time.monotonic() - _last_fetch_ts)
        if gap > 0:
            await asyncio.sleep(gap)
        _last_fetch_ts = time.monotonic()


async def _get_guest_cookies() -> dict[str, str]:
    """获取游客 web_session：由 xhshow 生成 a1/webId 并签名，无需用户 Cookie。

    xhshow 为可选依赖，未安装或获取失败时返回空字典，调用方退回匿名请求。
    """
    global _guest_cookies, _guest_ts
    if Xhshow is None:
        return {}
    async with _guest_lock:
        if _guest_cookies and time.monotonic() - _guest_ts < _GUEST_TTL:
            return _guest_cookies
        try:
            xh = Xhshow()
            a1 = xh.generate_a1()
            cookies = {
                "a1": a1,
                "webId": xh.generate_web_id(a1),
                "xsecappid": "xhs-pc-web",
            }
            payload = {
                "client_public_key_base64": base64.b64encode(os.urandom(32)).decode()
            }
            headers = xh.sign_headers_post(
                _GUEST_ACTIVATE_URI, cookies, payload=payload
            )
            headers.update(
                {
                    "User-Agent": PC_UA,
                    "Content-Type": "application/json;charset=UTF-8",
                    "Origin": "https://www.xiaohongshu.com",
                    "Referer": "https://www.xiaohongshu.com/",
                }
            )
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:
                resp = await client.post(
                    f"https://edith.xiaohongshu.com{_GUEST_ACTIVATE_URI}",
                    headers=headers,
                    content=xh.build_json_body(payload),
                    cookies=cookies,
                )
            session = resp.cookies.get("web_session")
            if not session:
                try:
                    session = ((resp.json() or {}).get("data") or {}).get("session")
                except ValueError:
                    session = None
            if not session:
                logger.warning(f"XHS 游客 session 获取失败: {resp.status_code}")
                return {}
            _guest_cookies = {**cookies, "web_session": session}
            _guest_ts = time.monotonic()
            logger.debug("XHS 游客 session 获取成功")
            return _guest_cookies
        except Exception as e:
            logger.warning(f"XHS 游客 session 获取异常: {e}")
            return {}


class XiaohongshuParser:
    def __init__(self, cookie: str = "", prefer_original: bool = True):
        self.cookie = cookie
        self.prefer_original = prefer_original
        self._cookie_suspect = False

    _LIVE_URL_RE = re.compile(r"/livestream/|/live/")

    async def parse(self, url: str) -> XiaohongshuParseResult:
        if self._LIVE_URL_RE.search(url):
            return XiaohongshuParseResult(
                success=False, error="该链接为小红书直播间，暂不支持下载"
            )

        resolved_url = await self._resolve_url(url)
        if not resolved_url:
            return XiaohongshuParseResult(
                success=False, error="无法解析小红书链接：链接格式无效"
            )

        if self._LIVE_URL_RE.search(resolved_url):
            return XiaohongshuParseResult(
                success=False, error="该链接为小红书直播间，暂不支持下载"
            )

        html, final_url = await self._fetch_page(resolved_url)
        if not html:
            return XiaohongshuParseResult(
                success=False,
                error="无法获取页面内容（请求被拦截或网络异常），请稍后重试",
            )

        if "/404/sec" in final_url:
            return XiaohongshuParseResult(
                success=False,
                error="小红书风控拦截：本次访问被判定为异常流量，请稍后重试",
            )

        if self._is_login_wall(final_url, html):
            hint = (
                "（配置的 Cookie 疑似失效，请重新获取）"
                if self._cookie_suspect
                else "（可配置 Cookie 提升成功率）"
            )
            return XiaohongshuParseResult(
                success=False,
                error=f"小红书风控拦截：页面被重定向到登录/验证页，请稍后重试{hint}",
            )

        state = self._extract_state(html)
        if not state:
            return XiaohongshuParseResult(
                success=False,
                error="解析失败：未找到笔记数据",
            )

        if state.get("notFoundPage"):
            return XiaohongshuParseResult(
                success=False,
                error="该笔记不存在或已删除",
            )

        result = self._parse_state(state, resolved_url)
        if not result.success and result.error == "未找到笔记数据":
            # noteDetailMap 偶发为空，重取一次
            logger.debug("XHS noteDetailMap 为空，重试一次")
            html, _ = await self._fetch_page(resolved_url)
            if html:
                state = self._extract_state(html)
                if state:
                    result = self._parse_state(state, resolved_url)
        return result

    @staticmethod
    def _is_login_wall(final_url: str, html: str) -> bool:
        low_url = (final_url or "").lower()
        if any(marker in low_url for marker in LOGIN_URL_MARKERS):
            return True
        low = html.lower()
        return any(marker in low for marker in LOGIN_HTML_MARKERS)

    async def _resolve_url(self, url: str) -> str | None:
        text_match = re.search(r"(https?://[^\s]+)", url)
        if not text_match:
            return None
        raw_url = text_match.group(1)

        if "xhslink.com" in raw_url or "xhslink.cn" in raw_url:
            for attempt in range(2):
                try:
                    async with httpx.AsyncClient(
                        timeout=DEFAULT_TIMEOUT, follow_redirects=False
                    ) as client:
                        resp = await client.get(
                            raw_url,
                            headers={"User-Agent": ANDROID_UA, **BASE_HEADERS},
                        )
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location", "")
                        if location:
                            return self._clean_url(unquote(location))
                except (
                    httpx.HTTPError,
                    httpx.TimeoutException,
                    httpx.ConnectError,
                ) as e:
                    logger.warning(f"XHS 短链接解析失败 (attempt {attempt + 1}): {e}")
                    if attempt == 0:
                        await asyncio.sleep(1)
            # last resort: let httpx follow
            try:
                async with httpx.AsyncClient(
                    timeout=DEFAULT_TIMEOUT, follow_redirects=True
                ) as client:
                    resp = await client.get(
                        raw_url, headers={"User-Agent": ANDROID_UA, **BASE_HEADERS}
                    )
                    return self._clean_url(str(resp.url))
            except (httpx.HTTPError, httpx.TimeoutException, httpx.ConnectError) as e:
                logger.warning(f"XHS 短链接全链跟随失败: {e}")
                return None

        return self._clean_url(raw_url)

    def _clean_url(self, url: str) -> str:
        """归一化为 /explore/<note_id>?xsec_token=...&xsec_source=pc_share

        App 分享的 /discovery/item/ 与 xsec_source=app_share 会被重定向至登录/验证页，
        仅 PC 的 explore 形态返回含 __INITIAL_STATE__ 的 SSR 页面。
        """
        parsed = urlparse(url)
        if (
            "xiaohongshu.com" not in parsed.netloc
            and "rednote.com" not in parsed.netloc
        ):
            return url

        qs = parse_qs(parsed.query)
        token = (qs.get("xsec_token") or [""])[0]
        note_id_match = NOTE_ID_RE.search(parsed.path)

        if note_id_match:
            path = f"/explore/{note_id_match.group(1)}"
            new_qs = {"xsec_token": token, "xsec_source": "pc_share"} if token else {}
            return urlunparse(
                parsed._replace(path=path, query=urlencode(new_qs) if new_qs else "")
            )

        return url

    async def _fetch_page(self, url: str) -> tuple[str | None, str]:
        # 统一 PC UA：Android UA 与 app_share 来源均被重定向至登录墙
        headers = {
            "User-Agent": PC_UA,
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
            "Referer": "https://www.xiaohongshu.com/",
            **BASE_HEADERS,
            # PC 需完整的 Chrome Accept，通用 Accept 易被判验证页
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8,"
                "application/signed-exchange;v=b3;q=0.7"
            ),
        }

        cookie_dict = {}
        if self.cookie:
            for pair in self.cookie.split(";"):
                pair = pair.strip()
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    cookie_dict[k.strip()] = v.strip()

        async def fetch(cookies: dict | None) -> tuple[str | None, str]:
            await _throttle_fetch()
            try:
                async with httpx.AsyncClient(
                    timeout=DEFAULT_TIMEOUT,
                    follow_redirects=True,
                    cookies=cookies,
                ) as client:
                    resp = await client.get(url, headers=headers)
            except (httpx.HTTPError, httpx.TimeoutException, httpx.ConnectError) as e:
                logger.warning(f"XHS 页面请求失败: {e}")
                return None, url
            final = str(resp.url)
            if resp.status_code != 200:
                logger.warning(f"XHS 页面返回 {resp.status_code}")
                return None, final
            return resp.text, final

        def usable(html: str | None, final_url: str) -> bool:
            return bool(html) and not self._is_login_wall(final_url, html)

        def done(
            source: str, html: str | None, final_url: str
        ) -> tuple[str | None, str]:
            logger.debug(f"XHS 页面获取成功（{source}）{urlparse(final_url).path}")
            return html, final_url

        final_url = url
        # 1) 配置的 Cookie；失效时会被判登录，需降级
        if cookie_dict:
            html, final_url = await fetch(cookie_dict)
            if usable(html, final_url):
                return done("配置 Cookie", html, final_url)
            self._cookie_suspect = True
            logger.warning(
                "XHS 带 Cookie 请求被重定向至登录页，疑 Cookie 失效，改用游客/匿名请求重试"
            )
            await asyncio.sleep(0.6)

        # 2) 游客 session：有效会话的风控阈值高于匿名
        guest = await _get_guest_cookies()
        if guest:
            html, final_url = await fetch(guest)
            if usable(html, final_url):
                return done("游客 session", html, final_url)
            logger.debug(f"XHS 游客 session 仍被拦截: {final_url}")

        # 3) 匿名；限额按时间窗滚动，退避需覆盖一个窗口
        for attempt in range(2):
            if attempt:
                await asyncio.sleep(2.5 + random.uniform(0.3, 1.0))
            html, final_url = await fetch(None)
            if usable(html, final_url):
                return done("匿名", html, final_url)
            logger.debug(f"XHS 匿名请求被拦截 (attempt {attempt + 1}): {final_url}")
        return html, final_url

    def _extract_state(self, html: str) -> dict | None:
        match = _INIT_STATE_RE.search(html)
        if match:
            raw = match.group(1)
            try:
                return _loads_state(raw)
            except json.JSONDecodeError as e:
                logger.debug(f"XHS __INITIAL_STATE__ regex JSON 解析失败: {e}")

        marker = "window.__INITIAL_STATE__"
        start = html.find(marker)
        if start == -1:
            return None
        brace_start = html.find("{", start)
        if brace_start == -1:
            return None
        script_end = html.find("</script>", brace_start)
        if script_end == -1:
            script_end = len(html)

        depth = 0
        in_string = False
        in_single = False
        escaped = False
        for i in range(brace_start, script_end):
            ch = html[i]
            if escaped:
                escaped = False
                continue
            if ch == "\\" and (in_string or in_single):
                escaped = True
                continue
            if ch == '"' and not in_single:
                in_string = not in_string
                continue
            if ch == "'" and not in_string:
                in_single = not in_single
                continue
            if not in_string and not in_single:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        raw = html[brace_start : i + 1]
                        try:
                            return _loads_state(raw)
                        except json.JSONDecodeError as e:
                            logger.error(
                                f"XHS __INITIAL_STATE__ brace JSON 解析失败: {e}"
                            )
                            return None
        return None

    def _parse_state(self, state: dict, url: str) -> XiaohongshuParseResult:
        note = None
        note_id = ""

        note_data = state.get("noteData", {}).get("data", {}).get("noteData")
        if note_data:
            note = note_data

        if not note:
            note_map = state.get("note", {}).get("noteDetailMap")
            if note_map:
                first_id = state.get("note", {}).get("firstNoteId")
                if first_id and first_id in note_map:
                    note = note_map[first_id].get("note")
                else:
                    for entry in note_map.values():
                        if isinstance(entry, dict) and "note" in entry:
                            note = entry["note"]
                            break

        if not note:
            return XiaohongshuParseResult(success=False, error="未找到笔记数据")

        desc = note.get("desc", "")
        raw_title = note.get("title", "")
        title = raw_title or desc[:100]
        note_type = note.get("type", "normal")
        publish_ts = note.get("time", 0)
        publish_time = (
            datetime.fromtimestamp(publish_ts / 1000).strftime("%Y-%m-%d")
            if publish_ts
            else ""
        )

        user = note.get("user", {}) or {}
        author_name = user.get("nickName") or user.get("nickname", "")
        author_id = user.get("userId", "")

        parts = (author_name or "").replace("\u200b", "").strip()
        if author_id:
            parts = f"{parts}(主页id:{author_id})"

        media_items = []
        video_url = ""
        image_urls = []
        video_duration = 0

        if note_type == "video":
            video_info = note.get("video", {})
            video_url = ""

            # 1) originVideoKey — 无水印原视频
            consumer = video_info.get("consumer", {})
            origin_key = consumer.get("originVideoKey", "")
            if origin_key:
                video_url = f"https://sns-video-bd.xhscdn.com/{origin_key}"
                media_items.append({"urls": [video_url], "type": "video"})
            else:
                # 2) 回退：stream 列表，按清晰度选最优
                media = video_info.get("media", {})
                stream = media.get("stream", {})
                video_url = self._pick_stream_url(stream)
                if video_url:
                    media_items.append({"urls": [video_url], "type": "video"})
                else:
                    return XiaohongshuParseResult(
                        success=False, error="无法找到视频流", note_id=note_id
                    )

        else:
            img_list = note.get("imageList", [])
            for img in img_list:
                raw_url = ""
                for key in ("urlDefault", "url"):
                    if img.get(key):
                        raw_url = img[key]
                        break
                if not raw_url and img.get("infoList"):
                    for info in img["infoList"]:
                        if info.get("imageScene") == "WB_DFT" and info.get("url"):
                            raw_url = info["url"]
                            break
                if not raw_url:
                    continue

                if raw_url.startswith("//"):
                    raw_url = "https:" + raw_url
                elif raw_url.startswith("http://"):
                    raw_url = "https://" + raw_url[7:]

                if _PICASSO_RE.search(raw_url):
                    continue

                if img.get("livePhoto"):
                    stream = img.get("stream", {})
                    v_url = self._pick_stream_url(stream)
                    if v_url:
                        media_items.append({"urls": [v_url], "type": "video"})
                        continue

                clean_url = self._get_raw_image_url(raw_url)
                if self.prefer_original:
                    media_items.append({"urls": [clean_url, raw_url], "type": "image"})
                else:
                    media_items.append({"urls": [raw_url, clean_url], "type": "image"})
                image_urls.append(clean_url)

        has_video_items = any(m["type"] == "video" for m in media_items)
        has_image_items = any(m["type"] == "image" for m in media_items)
        if has_video_items and has_image_items:
            media_type = "multi_video"
        elif has_video_items:
            media_type = "video"
        else:
            media_type = "multi_image"

        note_id = note.get("noteId") or ""
        cover_url = ""
        if note_type == "video" and video_url:
            img_list = note.get("imageList", [])
            if img_list:
                cover_url = img_list[0].get("urlDefault", "")

        if not media_items:
            return XiaohongshuParseResult(
                success=False, error="未找到任何可下载内容", note_id=note_id
            )

        return XiaohongshuParseResult(
            success=True,
            title=title,
            desc=desc,
            has_title=bool(raw_title),
            author=parts,
            note_id=note_id,
            media_type=media_type,
            publish_time=publish_time,
            media_items=media_items,
            image_urls=image_urls,
            video_url=video_url,
            cover_url=cover_url,
            duration=video_duration,
        )

    @staticmethod
    def _pick_stream_url(stream: dict) -> str:
        """从 stream 中选清晰度最高的轨道。

        分组键随登录态变化：匿名按编码分（h264/h265/av1/h266），
        登录态按清晰度等级分（EF4~EF7），故不按 key 枚举，改为遍历全部列表。
        """
        tracks = [
            track
            for value in stream.values()
            if isinstance(value, list)
            for track in value
            if isinstance(track, dict)
            and (track.get("masterUrl") or track.get("backupUrls"))
        ]
        if not tracks:
            return ""

        def rank(track: dict) -> tuple:
            height = track.get("height")
            size = track.get("size")
            return (
                height if isinstance(height, (int, float)) else 0,
                size if isinstance(size, (int, float)) else 0,
            )

        best = max(tracks, key=rank)

        # Prefer backupUrls[0] over masterUrl
        burls = best.get("backupUrls")
        if isinstance(burls, list) and burls and burls[0]:
            url = burls[0]
        else:
            url = best.get("masterUrl", "")

        if url and url.startswith("//"):
            url = "https:" + url
        return url or ""

    @staticmethod
    def _clean_webpic_path(path: str) -> str:
        m = re.match(r"/\d{12}/[a-f0-9]{32}(/.*)", path)
        if m:
            return m.group(1)
        return path

    @staticmethod
    def _get_raw_image_url(ime_url: str) -> str:
        if ime_url.startswith("//"):
            ime_url = "https:" + ime_url
        elif ime_url.startswith("http://"):
            ime_url = "https://" + ime_url[7:]
        parsed = urlparse(ime_url)
        clean_path = re.sub(r"![^/]*$", "", parsed.path)
        clean_path = XiaohongshuParser._clean_webpic_path(clean_path)
        return f"https://sns-img-hw.xhscdn.com{clean_path}"
