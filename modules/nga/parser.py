import re
from xml.etree import ElementTree as ET

import httpx

from astrbot.api import logger

from .constants import (
    API_READ,
    NGA_UA,
    BROWSER_UA,
    REG_NGA,
    TIMEOUT,
)
from .model import NgaMedia, NgaParseResult, NgaReply

_RECONNECT_MARKERS = re.compile(r"<!--msgcodestart-->(\d+)<!--msgcodeend-->")


class NgaError(Exception):
    def __init__(self, msg: str):
        self.msg = msg
        super().__init__(msg)


class NgaParser:
    def __init__(self, max_replies: int = 20, sort: str = "time"):
        self.max_replies = max_replies
        self.sort = sort
        self._proxy: str | None = None
        self._ua_android = False
        self._attach_base_url = "https://img.nga.178.com/attachments"
        self.access_uid: str = ""
        self.access_token: str = ""
        self.cookie: str = ""

    @staticmethod
    def match(url: str) -> bool:
        return bool(re.search(REG_NGA, url))

    @staticmethod
    def get_tid(url: str) -> str:
        if m := re.search(REG_NGA, url):
            return m.group(1)
        raise NgaError("无法从 URL 中提取帖子 ID")

    # ── HTTP ────────────────────────────────────────────────

    async def _fetch_xml(self, tid: str, page: int = 1, retries: int = 2) -> str:
        ua = NGA_UA
        param_sets = [
            {"__output": "10"},
            {"lite": "xml"},
        ]
        for attempt in range(retries + 1):
            params = param_sets[attempt % 2]
            param_str = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{API_READ}?tid={tid}&__inchst=UTF8&{param_str}&page={page}"
            headers = {
                "User-Agent": ua,
                "X-User-Agent": ua,
                "Referer": f"{API_READ}?tid={tid}",
            }
            post_data: dict[str, str] = {}
            if self.access_uid and self.access_token:
                post_data["access_uid"] = str(self.access_uid)
                post_data["access_token"] = str(self.access_token)
            async with httpx.AsyncClient(
                proxy=self._proxy, timeout=TIMEOUT, follow_redirects=True
            ) as cli:
                resp = await cli.post(url, headers=headers, data=post_data)
                raw = resp.content.decode("gb18030", errors="replace")
            error_code = self._check_error(raw)
            if error_code:
                if attempt < retries:
                    import asyncio

                    await asyncio.sleep(1 + attempt)
                    continue
                raise NgaError(f"NGA 返回错误 (code={error_code})")
            if raw.strip().endswith("</root>"):
                return raw
            if attempt < retries:
                import asyncio

                await asyncio.sleep(1 + attempt)
        raise NgaError("NGA 返回的 XML 不完整，请稍后重试")

    async def _fetch_html(self, tid: str, page: int = 1) -> str:
        import random

        url = f"{API_READ}?tid={tid}&page={page}"
        headers = {
            "User-Agent": BROWSER_UA,
            "Cookie": self.cookie,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        }
        async with httpx.AsyncClient(
            proxy=self._proxy, timeout=TIMEOUT, follow_redirects=False
        ) as cli:
            resp = await cli.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.content.decode("gb18030", errors="replace")

            # 403 → guest JS challenge (no login cookie, or cookie expired)
            if resp.status_code == 403:
                body = resp.content.decode("gb18030", errors="replace")
                gm = re.search(r"document\.cookie\s*=\s*'guestJs=([^;]+);domain", body)
                if gm:
                    guest_js = gm.group(1)
                    cj = dict(cli.cookies)
                    nga_uid = cj.get("ngaPassportUid", "")
                    lastvisit = cj.get("lastvisit", "")
                    rand = random.randint(0, 999)
                    url2 = f"{API_READ}?tid={tid}&page={page}&rand={rand}"
                    headers["Cookie"] = (
                        f"guestJs={guest_js}; ngaPassportUid={nga_uid}; lastvisit={lastvisit}"
                    )
                    headers["Referer"] = url
                    resp2 = await cli.get(url2, headers=headers)
                    if resp2.status_code == 200:
                        body2 = resp2.content.decode("gb18030", errors="replace")
                        return body2

        raise NgaError("NGA HTML 访问失败")

    @staticmethod
    def _decode_html_entities(text: str) -> str:
        text = text.replace("<br/>", "\n").replace("<br />", "\n")
        text = text.replace("&amp;", "&")
        text = text.replace("&lt;", "<").replace("&gt;", ">")
        text = text.replace("&#39;", "'").replace("&quot;", '"')
        text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
        text = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), text)
        return text.strip()

    @staticmethod
    def _extract_user_map_from_html(html: str) -> dict[str, str]:
        user_map: dict[str, str] = {}
        m = re.search(
            r"commonui\.userInfo\.setAll\(\s*(\{.*\})\s*\)\s*//userinfoend",
            html,
            re.DOTALL,
        )
        if m:
            try:
                import json

                raw = m.group(1)
                raw = re.sub(
                    r"[\x00-\x1f]",
                    lambda x: f"\\u{ord(x.group(0)):04x}",
                    raw,
                )
                data: dict = json.loads(raw)
                for uid_str, info in data.items():
                    if isinstance(info, dict) and "username" in info:
                        uname = info["username"]
                        if uname:
                            user_map[uid_str] = uname
            except Exception:
                pass
        for m in re.finditer(r"\[uid=(\d+)\]([^\[]+)\[/uid\]", html):
            uid = m.group(1)
            name = m.group(2).strip()
            if name and not name.startswith("UID"):
                user_map[uid] = name
        return user_map

    @staticmethod
    def _extract_scores_from_html(html: str) -> dict[str, int]:
        scores: dict[str, int] = {}
        for m in re.finditer(
            r"commonui\.postArg\.proc\(\s*\d+,"
            r".*?null,\s*null,(\d+),\s*\d+,\s*null,"
            r"\s*'\d+',\d+,\s*'([^']+)'",
            html,
            re.DOTALL,
        ):
            pid = m.group(1)
            score_raw = m.group(2)
            parts = score_raw.split(",")
            if len(parts) == 3:
                try:
                    score_val = int(parts[1])
                    if score_val > 0:
                        scores[pid] = score_val
                except ValueError:
                    pass
        return scores

    @staticmethod
    def _check_error(xml_text: str) -> int:
        m = re.search(
            r"<__MESSAGE>.*?<item>(\d+)</item>.*?</__MESSAGE>", xml_text, re.DOTALL
        )
        if m:
            code = int(m.group(1))
            if code != 0:
                return code
        return 0

    # ── XML content extraction ──────────────────────────────

    @staticmethod
    def _get_content_raw(content_elem: ET.Element | None) -> str:
        if content_elem is None:
            return ""
        xml_str = ET.tostring(content_elem, encoding="unicode")
        inner = xml_str
        if inner.startswith("<content>"):
            inner = inner[len("<content>") :]
        if inner.endswith("</content>"):
            inner = inner[: -len("</content>")]
        # Decode all levels of entity encoding (NGA double-encodes)
        while "&amp;" in inner:
            inner = inner.replace("&amp;", "&")
        inner = inner.replace("&lt;", "<").replace("&gt;", ">")
        inner = inner.replace("&#39;", "'").replace("&quot;", '"')
        # Decode HTML numeric entities (&#xxx; / &#xXXXX;)
        for _ in range(3):
            inner = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), inner)
            inner = re.sub(
                r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), inner
            )
        # Now decode the XML-serialized content: actual XML child elements may
        # appear as <br/>, <b>, <uid=...> etc.  Convert them back to BBCode.
        inner = inner.replace("<br/>", "\n").replace("<br />", "\n")
        inner = inner.replace("<br>", "\n")
        inner = inner.replace("<b>", "[b]").replace("</b>", "[/b]")
        inner = inner.replace("<i>", "[i]").replace("</i>", "[/i]")
        inner = inner.replace("<u>", "[u]").replace("</u>", "[/u]")
        inner = inner.replace("<del>", "[del]").replace("</del>", "[/del]")
        inner = re.sub(r"<uid=(\d+)>", r"[uid=\1]", inner)
        inner = inner.replace("</uid>", "[/uid]")
        return inner.strip()

    # ── BBCode parser ───────────────────────────────────────

    def _resolve_url(self, url: str) -> str:
        url = url.strip()
        if url.startswith("./"):
            url = url[2:]
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return self._attach_base_url.rstrip("/") + url
        if not url.startswith("http"):
            url = self._attach_base_url.rstrip("/") + "/" + url
        return url

    @staticmethod
    def _find_closing_bbc(text: str, tag_name: str, start: int = 0) -> int:
        depth = 1
        i = start
        while i < len(text):
            pos = text.find("[", i)
            if pos == -1:
                return -1
            tag_end = text.find("]", pos)
            if tag_end == -1:
                return -1
            tag_content = text[pos + 1 : tag_end]
            tag_lower = tag_content.lower()
            # Closing tag: [/tag]
            if tag_lower.startswith("/") and tag_lower[1:] == tag_name:
                depth -= 1
                if depth == 0:
                    return pos
                i = tag_end + 1
                continue
            # Opening tag: [tag] / [tag=...] / [tag ...]
            if (
                tag_lower == tag_name
                or tag_lower.startswith(tag_name + "=")
                or tag_lower.startswith(tag_name + " ")
            ):
                depth += 1
            i = tag_end + 1
        return -1

    def parse_bbcode(self, raw: str) -> tuple[str, list[NgaMedia], list[str]]:
        media_items: list[NgaMedia] = []
        result = self._parse_inline(raw, media_items)
        pids = [m.group(1) for m in re.finditer(r"\[pid=(\d+)", raw)]
        return result, media_items, pids

    def _parse_inline(self, text: str, media_items: list[NgaMedia]) -> str:
        pos = 0
        parts: list[str] = []
        while pos < len(text):
            tag_start = text.find("[", pos)
            if tag_start == -1:
                parts.append(text[pos:])
                break

            if tag_start > pos:
                parts.append(text[pos:tag_start])

            tag_end = text.find("]", tag_start)
            if tag_end == -1:
                parts.append(text[tag_start:])
                break

            tag_content = text[tag_start + 1 : tag_end]
            rest = text[tag_end + 1 :]

            if tag_content.startswith("/"):
                parts.append(text[tag_start : tag_end + 1])
                pos = tag_end + 1
                continue

            eq_idx = tag_content.find("=")
            space_idx = tag_content.find(" ")
            if space_idx > 0:
                tag_name = tag_content[:space_idx].lower()
            elif eq_idx > 0:
                tag_name = tag_content[:eq_idx].lower()
            else:
                tag_name = tag_content.lower()

            closing = f"[/{tag_name}]"

            if tag_name == "quote":
                end_idx = rest.lower().find(closing)
                if end_idx != -1:
                    inner = rest[:end_idx]
                    if inner.lstrip().lower().startswith("[pid"):
                        # System quote wrapping a [pid] – skip, pid_map handles it
                        pos = tag_end + 1 + end_idx + len(closing)
                        # Skip inter-block whitespace (NGA formatting artifacts)
                        while pos < len(text) and text[pos] in "\n\r":
                            pos += 1
                    else:
                        # Manual quote – render normally
                        parsed_inner = self._parse_inline(inner, media_items)
                        parts.append(
                            f"\u2500\u2500 \u56de\u590d \u2500\u2500\n{parsed_inner}\n\u2500\u2500"
                        )
                        pos = tag_end + 1 + end_idx + len(closing)
                else:
                    parts.append(text[tag_start : tag_end + 1])
                    pos = tag_end + 1

            elif tag_name == "collapse":
                title = ""
                if eq_idx > 0:
                    title = tag_content[eq_idx + 1 :]
                end_idx = rest.lower().find(closing)
                if end_idx != -1:
                    inner = rest[:end_idx]
                    parsed_inner = self._parse_inline(inner, media_items)
                    collapsed = "\u3010\u6298\u53e0"
                    if title:
                        collapsed += f": {title}"
                    collapsed += f"\u3011\n{parsed_inner}\n\u3010/\u6298\u53e0\u3011"
                    parts.append(collapsed)
                    pos = tag_end + 1 + end_idx + len(closing)
                else:
                    parts.append(text[tag_start : tag_end + 1])
                    pos = tag_end + 1

            elif tag_name == "pid":
                end_idx = rest.lower().find(closing)
                if end_idx != -1:
                    pos = tag_end + 1 + end_idx + len(closing)
                else:
                    pos = tag_end + 1

            elif tag_name == "url":
                href = ""
                if eq_idx > 0:
                    href = tag_content[eq_idx + 1 :]
                end_idx = rest.lower().find(closing)
                if end_idx != -1:
                    inner = rest[:end_idx]
                    parsed_inner = self._parse_inline(inner, media_items)
                    if href:
                        parts.append(f"{parsed_inner} ({href})")
                    else:
                        parts.append(parsed_inner)
                    pos = tag_end + 1 + end_idx + len(closing)
                else:
                    parts.append(text[tag_start : tag_end + 1])
                    pos = tag_end + 1

            elif tag_name == "img":
                end_idx = rest.lower().find(closing)
                if end_idx != -1:
                    src = rest[:end_idx].strip()
                    if src:
                        src = self._resolve_url(src)
                        media_items.append(NgaMedia(url=src))
                        parts.append("\u3010\u56fe\u7247\u3011")
                    pos = tag_end + 1 + end_idx + len(closing)
                else:
                    at_idx = tag_content.find("=")
                    if at_idx > 0:
                        src = tag_content[at_idx + 1 :].strip()
                        if src:
                            src = self._resolve_url(src)
                            media_items.append(NgaMedia(url=src))
                            parts.append("\u3010\u56fe\u7247\u3011")
                    pos = tag_end + 1

            elif tag_name == "flash":
                at_idx = tag_content.find("=")
                if at_idx > 0:
                    src = tag_content[at_idx + 1 :].strip()
                    src = self._resolve_url(src)
                    media_items.append(NgaMedia(url=src))
                    parts.append("\u3010\u89c6\u9891\u3011")
                pos = tag_end + 1

            elif tag_name == "attach":
                end_idx = rest.lower().find(closing)
                if end_idx != -1:
                    inner = rest[:end_idx].strip()
                    parts.append(f"\u3010\u9644\u4ef6: {inner}\u3011")
                    pos = tag_end + 1 + end_idx + len(closing)
                else:
                    parts.append("\u3010\u9644\u4ef6\u3011")
                    pos = tag_end + 1

            elif tag_name == "code":
                end_idx = rest.lower().find(closing)
                if end_idx != -1:
                    inner = rest[:end_idx].strip()
                    parts.append(f"\n```\n{inner}\n```\n")
                    pos = tag_end + 1 + end_idx + len(closing)
                else:
                    parts.append(text[tag_start : tag_end + 1])
                    pos = tag_end + 1

            elif tag_name == "list":
                end_idx = rest.lower().find(closing)
                if end_idx != -1:
                    inner = rest[:end_idx]
                    list_parts = []
                    for item_text in inner.split("[*]"):
                        item_text = item_text.strip()
                        if item_text:
                            parsed_item = self._parse_inline(item_text, media_items)
                            list_parts.append(f"  \u2022 {parsed_item}")
                    if list_parts:
                        parts.append("\n" + "\n".join(list_parts))
                    pos = tag_end + 1 + end_idx + len(closing)
                else:
                    parts.append(text[tag_start : tag_end + 1])
                    pos = tag_end + 1

            elif tag_name in ("table", "tr", "td", "th"):
                end_idx = rest.lower().find(closing)
                if end_idx != -1:
                    inner = rest[:end_idx]
                    parsed_inner = self._parse_inline(inner, media_items)
                    if tag_name in ("td", "th"):
                        parts.append(f"{parsed_inner} ")
                    elif tag_name == "tr":
                        parts.append(f"{parsed_inner}\n")
                    else:
                        parts.append(f"{parsed_inner}")
                    pos = tag_end + 1 + end_idx + len(closing)
                else:
                    parts.append(text[tag_start : tag_end + 1])
                    pos = tag_end + 1

            elif tag_name == "align":
                end_idx = rest.lower().find(closing)
                if end_idx != -1:
                    inner = rest[:end_idx]
                    parsed_inner = self._parse_inline(inner, media_items)
                    parts.append(parsed_inner)
                    pos = tag_end + 1 + end_idx + len(closing)
                else:
                    parts.append(text[tag_start : tag_end + 1])
                    pos = tag_end + 1

            elif tag_name == "b":
                end_idx = self._find_closing_bbc(rest, "b")
                if end_idx != -1:
                    inner = rest[:end_idx]
                    if "[pid" in inner:
                        # Attribution block referencing another post – skip, pid_map handles it
                        closing = f"[/{tag_name}]"
                        pos = tag_end + 1 + end_idx + len(closing)
                        while pos < len(text) and text[pos] in "\n\r":
                            pos += 1
                    else:
                        closing = f"[/{tag_name}]"
                        parsed_inner = self._parse_inline(inner, media_items)
                        parts.append(parsed_inner)
                        pos = tag_end + 1 + end_idx + len(closing)
                else:
                    parts.append(text[tag_start : tag_end + 1])
                    pos = tag_end + 1

            elif tag_name in ("i", "u", "del", "s", "color", "size", "font"):
                end_idx = self._find_closing_bbc(rest, tag_name)
                if end_idx != -1:
                    closing = f"[/{tag_name}]"
                    inner = rest[:end_idx]
                    parsed_inner = self._parse_inline(inner, media_items)
                    parts.append(parsed_inner)
                    pos = tag_end + 1 + end_idx + len(closing)
                else:
                    parts.append(text[tag_start : tag_end + 1])
                    pos = tag_end + 1

            elif tag_name == "tid":
                end_idx = rest.lower().find(closing)
                if end_idx != -1:
                    inner = rest[:end_idx]
                    parsed_inner = self._parse_inline(inner, media_items)
                    parts.append(f"\u2192 \u5e16\u5b50: {parsed_inner}")
                    pos = tag_end + 1 + end_idx + len(closing)
                else:
                    tid_val = tag_content.split("=")[-1] if "=" in tag_content else ""
                    parts.append(f"\u2192 \u5e16\u5b50 tid={tid_val}")
                    pos = tag_end + 1

            elif tag_name == "uid":
                end_idx = rest.lower().find(closing)
                if end_idx != -1:
                    inner = rest[:end_idx]
                    parsed_inner = self._parse_inline(inner, media_items)
                    parts.append(f"@{parsed_inner}")
                    pos = tag_end + 1 + end_idx + len(closing)
                else:
                    uid_val = tag_content.split("=")[-1] if "=" in tag_content else ""
                    parts.append(f"@\u7528\u6237{uid_val}")
                    pos = tag_end + 1

            elif tag_name == "at" or tag_content.startswith("@"):
                name = (
                    tag_content.split("=")[-1]
                    if "=" in tag_content
                    else tag_content.lstrip("@")
                )
                parts.append(f"@{name}")
                pos = tag_end + 1

            elif tag_name == "stripbr":
                pos = tag_end + 1

            elif tag_name.startswith("s:"):
                sticker_name = tag_content[len("s:") :]
                parts.append(f"\u3010\u8d34\u56fe:{sticker_name}\u3011")
                pos = tag_end + 1

            elif tag_name == "_divider":
                parts.append(
                    "\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
                )
                pos = tag_end + 1

            elif tag_name == "h":
                end_idx = rest.lower().find(closing)
                if end_idx != -1:
                    inner = rest[:end_idx]
                    parsed_inner = self._parse_inline(inner, media_items)
                    parts.append(f"\n\u2500\u2500 {parsed_inner} \u2500\u2500\n")
                    pos = tag_end + 1 + end_idx + len(closing)
                else:
                    parts.append(text[tag_start : tag_end + 1])
                    pos = tag_end + 1

            elif tag_name == "dice":
                end_idx = rest.lower().find(closing)
                if end_idx != -1:
                    inner = rest[:end_idx]
                    parts.append(f"\u3010\u9ab0\u5b50: {inner}\u3011")
                    pos = tag_end + 1 + end_idx + len(closing)
                else:
                    parts.append("\u3010\u9ab0\u5b50\u3011")
                    pos = tag_end + 1

            else:
                parts.append(text[tag_start : tag_end + 1])
                pos = tag_end + 1

        return "".join(parts)

    # ── parse ───────────────────────────────────────────────

    @staticmethod
    def _find_item_text(parent: ET.Element | None, tag: str, default: str = "") -> str:
        if parent is None:
            return default
        elem = parent.find(tag)
        if elem is not None and elem.text:
            return elem.text.strip()
        return default

    def _build_user_map(self, root: ET.Element) -> dict[str, str]:
        user_map: dict[str, str] = {}
        u_section = root.find("__U")
        if u_section is None:
            return user_map
        for item in u_section.findall("item"):
            uid = self._find_item_text(item, "uid")
            username = self._find_item_text(item, "username")
            nickname = self._find_item_text(item, "nickname")
            if uid:
                user_map[uid] = nickname or username
        return user_map

    async def parse(self, url: str) -> NgaParseResult:
        tid = self.get_tid(url)

        try:
            raw_xml = await self._fetch_xml(tid, 1)
        except NgaError as e:
            if "XML" in str(e):
                logger.debug(f"NGA XML 接口返回异常，尝试 HTML 回退 ({tid})")
                try:
                    return await self._parse_html_fallback(tid)
                except Exception:
                    pass
            return NgaParseResult(success=False, error=e.msg)
        except Exception as e:
            return NgaParseResult(success=False, error=f"NGA 请求失败: {e}")

        logger.debug(f"NGA 使用 XML API 解析成功 ({tid})")
        try:
            root = ET.fromstring(raw_xml)
        except ET.ParseError as e:
            return NgaParseResult(success=False, error=f"NGA XML 解析失败: {e}")

        try:
            user_map = self._build_user_map(root)

            try:
                html_p1 = await self._fetch_html(tid, 1)
                html_user_map = self._extract_user_map_from_html(html_p1)
                for uid, name in html_user_map.items():
                    if uid not in user_map or user_map[uid].startswith("UID"):
                        user_map[uid] = name
            except Exception:
                pass

            thread_elem = root.find("__T")
            if thread_elem is None:
                return NgaParseResult(success=False, error="未找到帖子信息")
            forum_elem = root.find("__F")

            title = self._find_item_text(thread_elem, "subject")
            author_id = self._find_item_text(thread_elem, "authorid")
            author = user_map.get(
                author_id, self._find_item_text(thread_elem, "author")
            )
            forum_name = (
                self._find_item_text(forum_elem, "name")
                if forum_elem is not None
                else ""
            )
            replies_str = self._find_item_text(thread_elem, "replies", "0")
            total_replies = int(replies_str) if replies_str.isdigit() else 0

            create_time = 0
            ts_str = self._find_item_text(thread_elem, "postdatetimestamp")
            if ts_str and ts_str.lstrip("-").isdigit():
                create_time = int(ts_str)

            global_elem = root.find("__GLOBAL")
            if global_elem is not None:
                abv = global_elem.find("_ATTACH_BASE_VIEW")
                if abv is not None and abv.text:
                    base = abv.text.strip().rstrip("/")
                    if not base.startswith("http"):
                        base = "https://" + base
                    self._attach_base_url = base

            rows_elem = root.find("__R")
            posts: list[ET.Element] = []
            if rows_elem is not None:
                posts = rows_elem.findall("item")

            for post_elem in posts:
                content_elem = post_elem.find("content")
                if content_elem is not None:
                    raw_bbcode_temp = self._get_content_raw(content_elem)
                    for m in re.finditer(
                        r"\[uid=(\d+)\]([^\[]+)\[/uid\]", raw_bbcode_temp
                    ):
                        uid_scanned = m.group(1)
                        display_name = m.group(2).strip()
                        if display_name and not display_name.startswith("UID"):
                            user_map[uid_scanned] = display_name

            op_content = ""
            op_media: list[NgaMedia] = []
            all_replies: list[NgaReply] = []
            pid_map: dict[str, tuple[int, str, str]] = {}
            op_pid = ""
            op_score = 0

            for post_elem in posts:
                post_type_str = self._find_item_text(post_elem, "type", "0")
                post_type = int(post_type_str) if post_type_str.isdigit() else 0
                floor_str = self._find_item_text(post_elem, "lou", "0")
                floor = int(floor_str) if floor_str.isdigit() else 0

                is_op = floor == 0 or (post_type & 0x2000000)

                author_id_p = self._find_item_text(post_elem, "authorid")
                post_author = user_map.get(author_id_p, "")

                content_elem = post_elem.find("content")
                raw_bbcode = self._get_content_raw(content_elem)
                parsed_content, post_media, reply_pids = self.parse_bbcode(raw_bbcode)
                post_id = self._find_item_text(post_elem, "pid")
                score_str = self._find_item_text(post_elem, "score", "0")
                score = int(score_str) if score_str.isdigit() else 0
                post_date = self._find_item_text(post_elem, "postdate")

                if post_id:
                    pid_map[post_id] = (floor, post_author, parsed_content)

                if is_op:
                    op_content = parsed_content
                    op_media = post_media
                    op_pid = post_id
                    op_score = score
                    if not post_author:
                        post_author = user_map.get(author_id, author)
                    if not create_time:
                        ts_str = self._find_item_text(post_elem, "postdatetimestamp")
                        if ts_str and ts_str.lstrip("-").isdigit():
                            create_time = int(ts_str)
                else:
                    if not parsed_content.strip():
                        continue
                    all_replies.append(
                        NgaReply(
                            floor=floor,
                            author=post_author,
                            content=parsed_content,
                            score=score,
                            post_id=post_id,
                            reply_to_pids=reply_pids,
                            media_items=post_media,
                            raw_bbcode=raw_bbcode,
                            post_date=post_date,
                        )
                    )

            page_size = len(posts)
            if self.sort == "hot":
                if page_size >= 20:
                    fetch_target = int(self.max_replies * 1.5)
                    page = 2
                    while len(all_replies) < fetch_target and page <= 10:
                        try:
                            more_xml = await self._fetch_xml(tid, page)
                            more_root = ET.fromstring(more_xml)
                        except Exception as e:
                            logger.warning(f"NGA 热帖第 {page} 页抓取失败: {e}")
                            break
                        more_user_map = self._build_user_map(more_root)
                        for muid, mname in more_user_map.items():
                            if muid not in user_map or user_map[muid].startswith("UID"):
                                user_map[muid] = mname
                        if self.cookie:
                            try:
                                more_html = await self._fetch_html(tid, page)
                                for muid, mname in self._extract_user_map_from_html(
                                    more_html
                                ).items():
                                    if muid not in user_map or user_map[
                                        muid
                                    ].startswith("UID"):
                                        user_map[muid] = mname
                            except Exception:
                                pass
                        more_rows = more_root.find("__R")
                        if more_rows is None:
                            break
                        more_posts = more_rows.findall("item")
                        if not more_posts:
                            break
                        for pe in more_posts:
                            ce = pe.find("content")
                            if ce is not None:
                                raw_bbcode_temp = self._get_content_raw(ce)
                                for m in re.finditer(
                                    r"\[uid=(\d+)\]([^\[]+)\[/uid\]", raw_bbcode_temp
                                ):
                                    dn = m.group(2).strip()
                                    if dn and not dn.startswith("UID"):
                                        user_map[m.group(1)] = dn
                        for pe in more_posts:
                            floor_str = self._find_item_text(pe, "lou", "0")
                            floor = int(floor_str) if floor_str.isdigit() else 0
                            if floor == 0:
                                continue
                            author_id_p = self._find_item_text(pe, "authorid")
                            post_author = user_map.get(author_id_p, "")
                            content_elem = pe.find("content")
                            raw_bbcode = self._get_content_raw(content_elem)
                            parsed_content, post_media, reply_pids = self.parse_bbcode(
                                raw_bbcode
                            )
                            if not parsed_content.strip():
                                continue
                            post_id = self._find_item_text(pe, "pid")
                            score_str = self._find_item_text(pe, "score", "0")
                            score = int(score_str) if score_str.isdigit() else 0
                            post_date = self._find_item_text(pe, "postdate")
                            if post_id:
                                pid_map[post_id] = (floor, post_author, parsed_content)
                            all_replies.append(
                                NgaReply(
                                    floor=floor,
                                    author=post_author,
                                    content=parsed_content,
                                    score=score,
                                    post_id=post_id,
                                    reply_to_pids=reply_pids,
                                    media_items=post_media,
                                    raw_bbcode=raw_bbcode,
                                    post_date=post_date,
                                )
                            )
                        page += 1
                all_replies.sort(key=lambda r: r.score, reverse=True)
            replies = all_replies[: self.max_replies]

            all_media = op_media[:]
            for rp in replies:
                all_media.extend(rp.media_items)
            image_urls = [m.url for m in all_media]

            return NgaParseResult(
                success=True,
                title=title,
                content=op_content,
                author=author,
                forum_name=forum_name,
                tid=tid,
                media_type="image" if image_urls else "text",
                media_items=op_media,
                image_urls=image_urls,
                create_time=create_time,
                replies=replies,
                total_replies=total_replies,
                pid_map=pid_map,
                op_post_id=op_pid,
                op_score=op_score,
            )

        except KeyError as e:
            return NgaParseResult(success=False, error=f"NGA 响应结构异常: {e}")
        except Exception as e:
            return NgaParseResult(success=False, error=f"NGA 解析异常: {e}")

    async def _parse_html_fallback(self, tid: str) -> NgaParseResult:
        html_p1 = await self._fetch_html(tid, 1)
        title_m = re.search(r"<h1\s+id='currentTopicName'[^>]*>(.*?)</h1>", html_p1)
        if not title_m:
            raise NgaError("HTML 解析失败：未找到标题")
        title = self._decode_html_entities(title_m.group(1).strip())

        forum_m = re.search(r"<h2\s+id='currentForumName'[^>]*>(.*?)</h2>", html_p1)
        forum_name = (
            self._decode_html_entities(forum_m.group(1).strip()) if forum_m else ""
        )

        op_uid = ""
        author_m = re.search(
            r"<a\s+href='nuke\.php\?func=ucp&uid=(\d+)'\s+id='postauthor0'",
            html_p1,
        )
        if author_m:
            op_uid = author_m.group(1)

        create_time = 0
        time_m = re.search(r"<span\s+id='postdate0'[^>]*>(.*?)</span>", html_p1)
        if time_m:
            import datetime

            try:
                dt = datetime.datetime.strptime(
                    time_m.group(1).strip(), "%Y-%m-%d %H:%M"
                )
                create_time = int(dt.timestamp())
            except ValueError:
                pass

        op_content = ""
        op_media: list[NgaMedia] = []
        content_m = re.search(
            r"<p\s+id='postcontent0'\s+class='postcontent\s+ubbcode'>(.*?)</p>",
            html_p1,
            re.DOTALL,
        )
        if content_m:
            raw = self._decode_html_entities(content_m.group(1).strip())
            op_content, op_media_list, _ = self.parse_bbcode(raw)
            if op_media_list:
                op_media = op_media_list
                for m_item in op_media:
                    m_item.url = self._resolve_url(m_item.url)

        pid_m = re.search(r"<a\s+id='pid(\d+)Anchor'>", html_p1)
        op_pid = pid_m.group(1) if pid_m else ""

        user_map = self._extract_user_map_from_html(html_p1)

        all_replies: list[NgaReply] = []
        pid_map: dict[str, tuple[int, str, str]] = {}
        seen_pids: set[str] = set()

        self._extract_html_replies(html_p1, user_map, all_replies, pid_map, seen_pids)

        need = self.max_replies
        page = 2
        pid_score = self._extract_scores_from_html(html_p1)
        while len(all_replies) < need and page <= 10:
            try:
                html_n = await self._fetch_html(tid, page)
                page_user_map = self._extract_user_map_from_html(html_n)
                for uid, name in page_user_map.items():
                    if uid not in user_map or user_map[uid].startswith("UID"):
                        user_map[uid] = name
                pid_score.update(self._extract_scores_from_html(html_n))
                self._extract_html_replies(
                    html_n, user_map, all_replies, pid_map, seen_pids
                )
                page += 1
            except Exception:
                break
        for r in all_replies:
            if r.post_id in pid_score:
                r.score = pid_score[r.post_id]
            if r.author.startswith("UID"):
                uid_candidate = r.author[3:]
                if uid_candidate in user_map:
                    r.author = user_map[uid_candidate]

        if self.sort == "hot":
            all_replies.sort(key=lambda r: r.score, reverse=True)
        else:
            all_replies.sort(key=lambda r: r.floor)
        replies = all_replies[: self.max_replies]

        all_media = op_media[:]
        for r in replies:
            all_media.extend(r.media_items)
        image_urls = [m.url for m in all_media]

        return NgaParseResult(
            success=True,
            title=title,
            content=op_content,
            author=user_map.get(op_uid, f"UID{op_uid}" if op_uid else ""),
            forum_name=forum_name,
            tid=tid,
            media_type="image" if image_urls else "text",
            media_items=op_media,
            image_urls=image_urls,
            create_time=create_time,
            replies=replies,
            total_replies=len(all_replies),
            pid_map=pid_map,
            op_post_id=op_pid,
            op_score=pid_score.get(op_pid, 0),
        )

    def _extract_html_replies(
        self,
        html: str,
        user_map: dict[str, str],
        all_replies: list[NgaReply],
        pid_map: dict[str, tuple[int, str, str]],
        seen_pids: set[str],
    ):
        for post_m in re.finditer(
            r"<tr\s+id='post1strow(\d+)'\s+class='postrow[^']*'>.*?"
            r"nuke\.php\?func=ucp&uid=(\d+).*?"
            r"<span\s+id='postdate\1'[^>]*>(.*?)</span>.*?"
            r"<span\s+id='postcontent\1'\s+class='postcontent\s+ubbcode'>(.*?)</span>",
            html,
            re.DOTALL,
        ):
            idx = post_m.group(1)
            author_uid = post_m.group(2)
            post_date = post_m.group(3).strip()
            raw_content = self._decode_html_entities(post_m.group(4).strip())

            floor = int(idx)
            if floor == 0:
                continue

            pid_match = re.search(r"<a\s+id='pid(\d+)Anchor'>", post_m.group(0))
            post_pid = pid_match.group(1) if pid_match else ""

            if post_pid and post_pid in seen_pids:
                continue
            if post_pid:
                seen_pids.add(post_pid)

            author_name = user_map.get(author_uid, f"UID{author_uid}")
            parsed_content, post_media, reply_pids = self.parse_bbcode(raw_content)
            if not parsed_content.strip():
                continue

            if post_pid:
                pid_map[post_pid] = (floor, author_name, parsed_content)

            all_replies.append(
                NgaReply(
                    floor=floor,
                    author=author_name,
                    content=parsed_content,
                    score=0,
                    post_id=post_pid,
                    reply_to_pids=reply_pids,
                    media_items=post_media,
                    raw_bbcode=raw_content,
                    post_date=post_date,
                )
            )
