import asyncio
import json
import re
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, unquote

import httpx

from astrbot.api import logger

from .model import XiaohongshuParseResult
from .constants import ANDROID_UA, PC_UA, BASE_HEADERS, DEFAULT_TIMEOUT


_INIT_STATE_RE = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", re.DOTALL
)

_PICASSO_RE = re.compile(r"picasso-static|fe-platform")

LOGIN_INDICATORS = [
    "用户登录",
    "登录后查看",
    "请登录",
    "login",
]


class XiaohongshuParser:
    def __init__(self, cookie: str = "", prefer_original: bool = True):
        self.cookie = cookie
        self.prefer_original = prefer_original

    async def parse(self, url: str) -> XiaohongshuParseResult:
        resolved_url = await self._resolve_url(url)
        if not resolved_url:
            return XiaohongshuParseResult(
                success=False, error="无法解析小红书链接：链接格式无效"
            )

        html = await self._fetch_page(resolved_url)
        if not html:
            return XiaohongshuParseResult(
                success=False,
                error="无法获取页面内容，该页面可能需要登录后查看",
            )

        state = self._extract_state(html)
        if not state:
            lower = html.lower()
            for keyword in LOGIN_INDICATORS:
                if keyword in lower:
                    return XiaohongshuParseResult(
                        success=False,
                        error="该笔记需要登录后查看",
                    )
            return XiaohongshuParseResult(
                success=False,
                error="解析失败：未找到笔记数据",
            )

        if state.get("notFoundPage"):
            return XiaohongshuParseResult(
                success=False,
                error="该笔记不存在或已删除",
            )

        return self._parse_state(state, resolved_url)

    async def _resolve_url(self, url: str) -> str | None:
        text_match = re.search(r"(https?://[^\s]+)", url)
        if not text_match:
            return None
        raw_url = text_match.group(1)

        if "xhslink.com" in raw_url:
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
                            return unquote(location)
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
                    return str(resp.url)
            except (httpx.HTTPError, httpx.TimeoutException, httpx.ConnectError) as e:
                logger.warning(f"XHS 短链接全链跟随失败: {e}")
                return None

        return self._clean_url(raw_url)

    def _clean_url(self, url: str) -> str:
        parsed = urlparse(url)
        if (
            "xiaohongshu.com" not in parsed.netloc
            and "rednote.com" not in parsed.netloc
        ):
            return url
        path = parsed.path
        if "/explore/" in path or "xsec_source=pc" in url:
            return url
        if "/discovery/item/" in path:
            qs = parse_qs(parsed.query)
            qs.pop("source", None)
            qs.pop("xhsshare", None)
            new_qs = urlencode(qs, doseq=True) if qs else ""
            return urlunparse(parsed._replace(query=new_qs))
        return url

    async def _fetch_page(self, url: str) -> str | None:
        is_pc = "/explore/" in url or "xsec_source=pc" in url
        ua = PC_UA if is_pc else ANDROID_UA
        headers = {"User-Agent": ua, **BASE_HEADERS}
        if is_pc:
            headers["Sec-CH-UA-Mobile"] = "?0"
            headers["Sec-CH-UA-Platform"] = '"Windows"'

        cookies = {}
        if self.cookie:
            for pair in self.cookie.split(";"):
                pair = pair.strip()
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    cookies[k.strip()] = v.strip()

        for attempt in range(2):
            try:
                async with httpx.AsyncClient(
                    timeout=DEFAULT_TIMEOUT,
                    follow_redirects=True,
                    cookies=cookies or None,
                ) as client:
                    resp = await client.get(url, headers=headers)
                    if resp.status_code == 200:
                        return resp.text
                    if attempt == 0 and resp.status_code in (301, 302, 303, 307, 308):
                        url = str(resp.url)
                        continue
                    logger.warning(f"XHS 页面返回 {resp.status_code}")
                    return None
            except (httpx.HTTPError, httpx.TimeoutException, httpx.ConnectError) as e:
                logger.warning(f"XHS 页面请求失败 (attempt {attempt + 1}): {e}")
                if attempt == 0:
                    await asyncio.sleep(1)
        return None

    def _extract_state(self, html: str) -> dict | None:
        match = _INIT_STATE_RE.search(html)
        if match:
            raw = match.group(1).replace("undefined", "null")
            try:
                return json.loads(raw)
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
                        raw = html[brace_start : i + 1].replace("undefined", "null")
                        try:
                            return json.loads(raw)
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
        # Combine h264 + h265 tracks, sort by quality (height desc)
        tracks = []
        for codec in ("h264", "h265", "av1", "h266"):
            t = stream.get(codec, [])
            if isinstance(t, list):
                tracks.extend(t)
        if not tracks:
            return ""

        tracks.sort(
            key=lambda t: t.get("height", 0) if isinstance(t, dict) else 0, reverse=True
        )

        best = tracks[0] if isinstance(tracks[0], dict) else None
        if not best:
            return ""

        # Prefer backupUrls[0] over masterUrl
        burls = best.get("backupUrls")
        if burls and isinstance(burls, list) and burls[0]:
            url = burls[0]
        else:
            url = best.get("masterUrl", "")

        if url:
            if url.startswith("//"):
                url = "https:" + url
            return url
        return ""

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
