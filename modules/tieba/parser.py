import hashlib
import re
from typing import Any

import httpx

from astrbot.api import logger

from .constants import (
    API_PAGE,
    API_PAGE_PC,
    API_TBS,
    HEADERS,
    PAGE_PC_SALT,
    REG_TIEBA,
    SIGN_SALT,
    TIMEOUT,
)
from .model import TiebaMedia, TiebaParseResult, TiebaReply
from . import protobuf_helper  # safe, never raises

if protobuf_helper.PROTO_AVAILABLE:
    from aiotieba.api.get_posts.protobuf.PbPageReqIdl_pb2 import PbPageReqIdl
    from aiotieba.api.get_posts.protobuf.PbPageResIdl_pb2 import PbPageResIdl

_PROTO_READY = protobuf_helper.PROTO_AVAILABLE
_PB_API = "https://tiebac.baidu.com/c/f/pb/page?cmd=302001"


class TiebaError(Exception):
    def __init__(self, msg: str):
        self.msg = msg
        super().__init__(msg)


class TiebaParser:
    def __init__(self, max_replies: int = 20, sort: str = "time"):
        self.max_replies = max_replies
        self.sort = sort
        self._proxy: str | None = None

    @staticmethod
    def match(url: str) -> bool:
        return bool(re.search(REG_TIEBA, url))

    @staticmethod
    def get_kz(url: str) -> str:
        if m := re.search(r"/p/(\d+)", url):
            return m.group(1)
        raise TiebaError("无法从 URL 中提取帖子 ID")

    # ── helpers shared by both paths ──────────────────────────

    @staticmethod
    def _extract_content(content_list) -> str:
        parts: list[str] = []
        for item in content_list:
            if isinstance(item, dict):
                t = item.get("type")
                if t == 0:
                    parts.append(item.get("text", ""))
                elif t == 2:
                    name = item.get("c", "") or item.get("text", "")
                    if name:
                        parts.append(f"\u3010{name}\u3011")
            else:
                t = item.type
                if t == 0:
                    parts.append(item.text)
                elif t == 2:
                    name = item.c or item.text
                    if name:
                        parts.append(f"\u3010{name}\u3011")
        return "".join(parts)

    @staticmethod
    def _extract_author_name(info) -> str:
        if isinstance(info, dict):
            return info.get("name_show", "") or info.get("name", "")
        return info.name_show or info.name

    # ── protobuf path ─────────────────────────────────────────

    async def _fetch_protobuf(self, url: str) -> Any:
        kz = int(self.get_kz(url))
        req = PbPageReqIdl()
        req.data.common._client_type = 2
        req.data.common._client_version = "12.64.1.1"
        req.data.kz = kz
        req.data.pn = 1
        if self.sort == "hot":
            req.data.rn = max(30, int(self.max_replies * 1.5))
        else:
            req.data.rn = 30
        req.data.r = 0
        req.data.lz = 0
        req.data.with_floor = 1
        req.data.floor_rn = 3

        body = req.SerializeToString()
        boundary = "----" + hashlib.md5(str(kz).encode()).hexdigest()
        part_body = (
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="data"; filename="file"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + body
            + f"\r\n--{boundary}--\r\n".encode()
        )

        async with httpx.AsyncClient(proxy=self._proxy, timeout=TIMEOUT) as cli:
            resp = await cli.post(
                _PB_API,
                content=part_body,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "x_bd_data_type": "protobuf",
                },
            )
        res = PbPageResIdl()
        res.ParseFromString(resp.content)
        if res.error.errorno:
            msg = res.error.errormsg or "获取帖子内容失败"
            raise TiebaError(msg)
        return res.data

    def _parse_protobuf(self, data) -> TiebaParseResult:
        user_map: dict[int, str] = {
            u.id: self._extract_author_name(u) for u in data.user_list if u.id
        }
        post_list = list(data.post_list)
        if not post_list:
            return TiebaParseResult(success=False, error="帖子列表为空")

        op_post = post_list[0]
        title = data.thread.title or ""
        op_author = user_map.get(op_post.author_id, "")
        forum_name = data.forum.name or ""
        content = self._extract_content(op_post.content)
        op_agree_num = (
            data.thread.agree.agree_num if data.thread.HasField("agree") else 0
        )

        media_items: list[TiebaMedia] = []
        image_urls: list[str] = []
        for c in op_post.content:
            if c.type == 3:
                src = (
                    c.origin_src or c.big_cdn_src or c.cdn_src or c.src or ""
                ).strip()
                if src:
                    if src.startswith("http://"):
                        src = "https://" + src[7:]
                    media_items.append(TiebaMedia(url=src))
                    image_urls.append(src)

        all_replies: list[TiebaReply] = []
        for post in post_list[1:]:
            floor = post.floor or 0
            r_content = self._extract_content(post.content)
            if not r_content.strip():
                continue
            if len(all_replies) >= self.max_replies and self.sort != "hot":
                break
            r_author = user_map.get(post.author_id, "")
            r_agree_num = post.agree.agree_num if post.HasField("agree") else 0
            r_media: list[TiebaMedia] = []
            for c in post.content:
                if c.type == 3:
                    src = (
                        c.origin_src or c.big_cdn_src or c.cdn_src or c.src or ""
                    ).strip()
                    if src:
                        if src.startswith("http://"):
                            src = "https://" + src[7:]
                        r_media.append(TiebaMedia(url=src))
            all_replies.append(
                TiebaReply(
                    floor=floor,
                    author=r_author,
                    content=r_content,
                    agree_num=r_agree_num,
                    media_items=r_media,
                )
            )

        if self.sort == "hot":
            all_replies.sort(key=lambda r: r.agree_num, reverse=True)
        replies = all_replies[: self.max_replies]

        return TiebaParseResult(
            success=True,
            title=title,
            content=content,
            author=op_author,
            forum_name=forum_name,
            tieba_id=str(data.thread.id),
            media_type="video" if False else ("image" if image_urls else "text"),
            media_items=media_items,
            image_urls=image_urls,
            video_url="",
            cover_url="",
            create_time=op_post.time,
            replies=replies,
            agree_num=op_agree_num,
        )

    # ── JSON fallback path ────────────────────────────────────

    @staticmethod
    def gen_sign(params: dict[str, Any], salt: str = SIGN_SALT) -> str:
        items = sorted(params.items())
        base_str = "".join(f"{k}={v}" for k, v in items)
        return hashlib.md5((base_str + salt).encode("utf-8")).hexdigest()

    async def _fetch_tbs(self) -> str:
        async with httpx.AsyncClient(proxy=self._proxy, headers=HEADERS) as cli:
            resp = await cli.get(API_TBS)
            resp.raise_for_status()
            data = resp.json()
        if tbs := data.get("tbs"):
            return str(tbs)
        raise TiebaError("获取 tbs 失败")

    async def _fetch_json(self, url: str) -> dict[str, Any]:
        kz = self.get_kz(url)
        tbs = await self._fetch_tbs()

        page_data = {
            "kz": kz,
            "pn": "1",
            "lz": "0",
            "r": "2",
            "tbs": tbs,
            "rn": "30",
            "floor_rn": "3",
            "with_floor": "1",
            "st_type": "tb_frslist",
            "back": "0",
            "mark": "0",
            "scr_dip": "320",
            "scr_h": "1920",
            "scr_w": "1080",
        }
        page_data["sign"] = self.gen_sign(page_data, SIGN_SALT)

        pc_data = {
            "pn": "1",
            "lz": "0",
            "r": "2",
            "mark_type": "0",
            "back": "0",
            "fr": "personalize_page",
            "kz": kz,
            "session_request_times": "1",
            "tbs": tbs,
            "subapp_type": "pc",
            "_client_type": "20",
        }
        pc_data["sign"] = self.gen_sign(pc_data, PAGE_PC_SALT)

        async with httpx.AsyncClient(
            proxy=self._proxy, timeout=TIMEOUT, headers=HEADERS
        ) as cli:
            r1 = await cli.post(API_PAGE, data=page_data)
            r2 = await cli.post(API_PAGE_PC, data=pc_data)
            result_page: dict[str, Any] = r1.json()
            result_pc: dict[str, Any] = r2.json()

        if result_page.get("error_code"):
            msg = result_page.get("error_msg") or "获取帖子内容失败"
            raise TiebaError(msg)

        result_page["_user_list_pc"] = result_pc.get("user_list", [])
        oti = result_pc.get("thread", {}).get("origin_thread_info", {})
        result_page["_media_pc"] = oti.get("media", [])
        result_page["_fname_pc"] = oti.get("fname", "")
        if not result_page.get("forum", {}).get("name"):
            result_page["forum"] = {"name": result_page["_fname_pc"]}
        return result_page

    def _parse_json(self, raw: dict[str, Any]) -> TiebaParseResult:
        post_list = raw.get("post_list", [])
        if not post_list:
            return TiebaParseResult(success=False, error="帖子列表为空")

        op_post = post_list[0]
        title = op_post.get("title", "") or raw.get("thread", {}).get("title", "")

        user_map: dict[int, str] = {}
        for u in raw.get("_user_list_pc", []):
            uid = u.get("id")
            if uid:
                user_map[uid] = self._extract_author_name(u)

        op_author_info = op_post.get("author", {})
        op_author_id = op_author_info.get("id")
        author = user_map.get(op_author_id) or self._extract_author_name(op_author_info)
        forum_name = raw.get("forum", {}).get("name", "")
        content = self._extract_content(op_post.get("content", []))
        op_agree = raw.get("thread", {}).get("agree", {})
        op_agree_num = op_agree.get("agree_num", 0) if isinstance(op_agree, dict) else 0

        media_items: list[TiebaMedia] = []
        image_urls: list[str] = []
        for om in raw.get("_media_pc", []):
            url_big = om.get("big_pic", "")
            url_small = om.get("small_pic", "")
            if url_big:
                media_items.append(
                    TiebaMedia(
                        url=url_big,
                        thumb_url=url_small or None,
                        width=om.get("width", 0),
                        height=om.get("height", 0),
                    )
                )
                image_urls.append(url_big)

        all_replies: list[TiebaReply] = []
        for p in post_list[1:]:
            floor = p.get("floor", 0)
            r_content = self._extract_content(p.get("content", []))
            if not r_content.strip():
                continue
            if len(all_replies) >= self.max_replies and self.sort != "hot":
                break
            r_author_id = p.get("author", {}).get("id")
            r_author = user_map.get(r_author_id) or self._extract_author_name(
                p.get("author", {})
            )
            r_agree = p.get("agree", {})
            r_agree_num = (
                r_agree.get("agree_num", 0) if isinstance(r_agree, dict) else 0
            )
            r_media: list[TiebaMedia] = []
            for item in p.get("content", []):
                if item.get("type") == 3:
                    src = item.get("src", "")
                    if src:
                        if src.startswith("http://"):
                            src = "https://" + src[7:]
                        r_media.append(TiebaMedia(url=src))
            all_replies.append(
                TiebaReply(
                    floor=floor,
                    author=r_author,
                    content=r_content,
                    agree_num=r_agree_num,
                    media_items=r_media,
                )
            )

        if self.sort == "hot":
            all_replies.sort(key=lambda r: r.agree_num, reverse=True)
        replies = all_replies[: self.max_replies]

        return TiebaParseResult(
            success=True,
            title=title,
            content=content,
            author=author,
            forum_name=forum_name,
            tieba_id=str(raw.get("thread", {}).get("id", "")),
            media_type="video" if False else ("image" if image_urls else "text"),
            media_items=media_items,
            image_urls=image_urls,
            video_url="",
            cover_url="",
            create_time=op_post.get("time", 0),
            replies=replies,
            agree_num=op_agree_num,
        )

    # ── main ──────────────────────────────────────────────────

    async def parse(self, url: str) -> TiebaParseResult:
        if _PROTO_READY:
            try:
                data = await self._fetch_protobuf(url)
                return self._parse_protobuf(data)
            except TiebaError:
                logger.warning("贴吧 protobuf 解析失败，降级到 JSON")
            except Exception as e:
                logger.warning(f"贴吧 protobuf 异常，降级到 JSON: {e}")

        try:
            raw = await self._fetch_json(url)
            return self._parse_json(raw)
        except TiebaError as e:
            return TiebaParseResult(success=False, error=e.msg)
        except Exception as e:
            return TiebaParseResult(success=False, error=f"贴吧解析失败: {e}")
