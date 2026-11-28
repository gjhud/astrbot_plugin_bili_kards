import asyncio
import hashlib
import html
import json
import logging
import re
import time
import urllib.parse

import aiohttp

logger = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

MIXIN_KEY_ENC_TAB = [
    46,
    47,
    18,
    2,
    53,
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

HOME = "https://www.bilibili.com/"
NAV = "https://api.bilibili.com/x/web-interface/nav"
SPACE_FEED = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
# Feed items only carry a summary for long opus posts; the detail endpoint
# returns the full paragraph list.
OPUS_DETAIL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/detail"
VIDEO_VIEW = "https://api.bilibili.com/x/web-interface/view"
ARTICLE_VIEW = "https://api.bilibili.com/x/article/view"

ZERO_WIDTH = "\u200b\u200c\u200d\ufeff"

TYPE_LABELS = {
    "DYNAMIC_TYPE_DRAW": "图文",
    "DYNAMIC_TYPE_ARTICLE": "专栏",
    "DYNAMIC_TYPE_AV": "视频",
    "DYNAMIC_TYPE_FORWARD": "转发",
    "DYNAMIC_TYPE_LIVE": "直播",
    "DYNAMIC_TYPE_LIVE_RCMD": "直播",
    "DYNAMIC_TYPE_PGC": "番剧",
    "DYNAMIC_TYPE_COURSES": "课程",
    "DYNAMIC_TYPE_MUSIC": "音乐",
    "DYNAMIC_TYPE_COMMON_SQUARE": "动态",
}


def _clean(text, limit=None):
    """把接口里的富文本节点整理成适合聊天窗口显示的纯文本。
    不再对正文做字数截断，直接返回（只做空白和换行清理）。"""
    if not isinstance(text, str):
        text = str(text or "")
    text = text.translate({ord(c): None for c in ZERO_WIDTH})
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    # 截断逻辑已移除，不再限制长度
    return text


def _node_text(node):
    """递归提取 B 站动态里各种 text/words/summary 节点的文字。"""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_node_text(x) for x in node)
    if isinstance(node, dict):
        for key in (
            "text",
            "words",
            "title",
            "desc",
            "description",
            "summary",
            "content",
        ):
            value = node.get(key)
            if value not in (None, ""):
                return _node_text(value)
        nodes = node.get("rich_text_nodes")
        if isinstance(nodes, list):
            return "".join(_node_text(x) for x in nodes)
    return ""


def _pick_body(module_dynamic):
    """从 module_dynamic 中提取标题和正文，兼容 opus/major/旧版字段。"""
    if not isinstance(module_dynamic, dict):
        return "", ""

    title = ""
    parts = []

    desc = module_dynamic.get("desc")
    if desc:
        text = _clean(_node_text(desc))
        if text:
            parts.append(text)

    opus = module_dynamic.get("opus")
    if isinstance(opus, dict):
        text = _clean(_node_text(opus.get("title")))
        if text and not title:
            title = text
        text = _clean(_node_text(opus.get("summary")))
        if text:
            parts.append(text)

    major = module_dynamic.get("major")
    if isinstance(major, dict):
        for key in (
            "opus",
            "article",
            "archive",
            "common",
            "live",
            "draw",
            "music",
            "pgc",
            "courses",
            "medialist",
            "ugc_season",
        ):
            node = major.get(key)
            if not isinstance(node, dict):
                continue
            text = _clean(_node_text(node.get("title")))
            if text and not title:
                title = text
            for field in ("summary", "desc", "description", "content"):
                value = node.get(field)
                if value:
                    text = _clean(_node_text(value))
                    if text:
                        parts.append(text)

    draw = module_dynamic.get("draw")
    if isinstance(draw, dict):
        text = _clean(_node_text(draw.get("description")))
        if text:
            parts.append(text)
        for item in draw.get("items") or []:
            if isinstance(item, dict):
                text = _clean(_node_text(item.get("description")))
                if text:
                    parts.append(text)

    for key in ("topic", "additional"):
        node = module_dynamic.get(key)
        if isinstance(node, dict):
            text = _clean(_node_text(node.get("title") or node.get("desc_text")))
            if text:
                parts.append(text)

    body = []
    for text in parts:
        text = _clean(text)
        if not text or text == title or text in body:
            continue
        body.append(text)
    return title, "\n".join(body).strip()


def _pick_pics(module_dynamic):
    """提取配图地址，统一成 https，去重后返回。"""
    urls = []
    major = module_dynamic.get("major")
    if isinstance(major, dict):
        opus = major.get("opus")
        if isinstance(opus, dict):
            for pic in opus.get("pics") or []:
                url = pic.get("url") if isinstance(pic, dict) else pic
                if url:
                    urls.append(url)
        archive = major.get("archive")
        if isinstance(archive, dict) and archive.get("cover"):
            urls.append(archive["cover"])

    draw = module_dynamic.get("draw")
    if not isinstance(draw, dict) and isinstance(major, dict):
        draw = major.get("draw")
    if isinstance(draw, dict):
        for pic in draw.get("items") or []:
            url = (pic.get("src") or pic.get("url")) if isinstance(pic, dict) else pic
            if url:
                urls.append(url)

    out = []
    for url in urls:
        url = str(url).replace("http://", "https://")
        if url not in out:
            out.append(url)
    return out


def _paragraph_text(para):
    """Flatten one opus detail paragraph into plain text."""
    text = para.get("text")
    if isinstance(text, dict):
        chunks = []
        for node in text.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            word = (node.get("word") or {}).get("words")
            if word:
                chunks.append(str(word))
                continue
            rich = node.get("rich")
            if isinstance(rich, dict) and rich.get("text"):
                chunks.append(str(rich["text"]))
        if chunks:
            return "".join(chunks).strip()
    return _node_text(para).strip()


def _detail_content(item):
    """Extract full text and inline image urls from an opus detail item.

    Args:
        item: The ``data.item`` object returned by the opus detail endpoint.

    Returns:
        ``(text, pic_urls)``. Text keeps the original paragraph order; tiny
        decorative separator images (e.g. 592x9 lines) are skipped.
    """
    modules = item.get("modules")
    if isinstance(modules, dict):
        modules = [modules]
    content = None
    for module in modules or []:
        if (
            isinstance(module, dict)
            and module.get("module_type") == "MODULE_TYPE_CONTENT"
        ):
            content = module.get("module_content") or {}
            break
    if not isinstance(content, dict):
        return "", []

    lines = []
    pics = []
    for para in content.get("paragraphs") or []:
        if not isinstance(para, dict):
            continue
        text = _paragraph_text(para)
        if text:
            lines.append(text)
        pic = para.get("pic")
        if not isinstance(pic, dict):
            continue
        for entry in pic.get("pics") or []:
            if not isinstance(entry, dict) or not entry.get("url"):
                continue
            width = int(entry.get("width") or 0)
            height = int(entry.get("height") or 0)
            if (width and width < 32) or (height and height < 32):
                continue
            url = str(entry["url"]).replace("http://", "https://")
            if url not in pics:
                pics.append(url)
    return "\n".join(lines).strip(), pics


def _pick_url(item, module_dynamic):
    basic = item.get("basic") or {}
    url = basic.get("jump_url") or ""
    if not url:
        major = module_dynamic.get("major")
        if isinstance(major, dict):
            for key in ("archive", "article", "opus", "live"):
                node = major.get(key)
                if isinstance(node, dict) and node.get("jump_url"):
                    url = node["jump_url"]
                    break
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = "https://www.bilibili.com" + url
    if not url:
        did = item.get("id_str") or basic.get("id_str") or ""
        if did:
            url = "https://t.bilibili.com/" + did
    return url


def extract_item(item):
    """把一条 B 站动态整理成插件内部使用的短结构。"""
    modules = item.get("modules") or {}
    module_dynamic = modules.get("module_dynamic") or {}
    author = modules.get("module_author") or {}
    stat = modules.get("module_stat") or {}

    title, body = _pick_body(module_dynamic)

    origin = item.get("orig")
    if isinstance(origin, dict):
        origin_dynamic = (origin.get("modules") or {}).get("module_dynamic") or {}
        origin_title, origin_body = _pick_body(origin_dynamic)
        if not origin_body:
            origin_body = origin_title
        if origin_body:
            if body:
                body += "\n\n↪ 转发原文：\n" + origin_body
            else:
                body = "↪ 转发原文：\n" + origin_body

    # 正文配图：原动态 + 转发原文里的图片一起收进来
    pics = _pick_pics(module_dynamic)
    origin_src = (item.get("orig") or {}).get("modules") or {}
    if isinstance(origin_src, dict):
        origin_dyn = origin_src.get("module_dynamic") or {}
        if isinstance(origin_dyn, dict):
            for purl in _pick_pics(origin_dyn):
                if purl not in pics:
                    pics.append(purl)

    if not body and not title:
        body = "（无文字内容）"
    elif not body and pics:
        body = "（图片动态，点击链接查看）"

    return {
        "id": item.get("id_str") or (item.get("basic") or {}).get("id_str") or "",
        "type": item.get("type") or "",
        "type_label": TYPE_LABELS.get(item.get("type") or "", "动态"),
        "author": author.get("name") or "KARDS官方",
        "pub_time": author.get("pub_time") or "",
        "title": title,
        "body": body,
        "url": _pick_url(item, module_dynamic),
        "pics": pics,
        "like": ((stat.get("like") or {}).get("count")) or 0,
        "comment": ((stat.get("comment") or {}).get("count")) or 0,
        "forward": ((stat.get("forward") or {}).get("count")) or 0,
    }


class DynamicClient:
    """B 站空间动态客户端。

    B 站会按游客指纹返回空列表或 412，所以先访问首页拿到激活的 buvid3，
    再用 wbi 签名请求动态接口；遇到空列表/风控时换会话重试。
    """

    def __init__(self, uid="2127596945", cache_ttl=300, timeout=15, proxy=None, cookie=None):
        self.uid = str(uid)
        self.cache_ttl = max(0, int(cache_ttl))
        self.timeout = aiohttp.ClientTimeout(total=max(5, int(timeout)))
        self.proxy = str(proxy or "").strip() or None
        self.cookie = str(cookie or "").strip() or None
        self.session = None
        self._warmed = False
        self._mixin_key = ""
        self._mixin_ts = 0.0
        self._cache_ts = 0.0
        self._cache_items = []
        self._lock = asyncio.Lock()

    def clear_cache(self) -> None:
        """Drop the in-memory dynamic cache so the next fetch hits Bilibili."""
        self._cache_ts = 0.0
        self._cache_items = []

    def _headers(self, headers):
        """Attach an optional logged-in cookie (e.g. SESSDATA) when configured."""
        if not self.cookie:
            return headers
        merged = dict(headers)
        merged["Cookie"] = self.cookie
        return merged

    async def _ensure(self):
        if self.session is None or self.session.closed:
            kwargs = {"headers": {"User-Agent": UA}}
            if self.proxy:
                kwargs["proxy"] = self.proxy
            self.session = aiohttp.ClientSession(**kwargs)
            self._warmed = False
            self._mixin_key = ""
            self._mixin_ts = 0.0
        if not self._warmed:
            try:
                async with self.session.get(
                    HOME,
                    headers={"User-Agent": UA},
                    timeout=self.timeout,
                ) as resp:
                    await resp.read()
            except Exception:
                pass
            self._warmed = True

    async def _reset(self):
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None
        self._warmed = False
        self._mixin_key = ""
        self._mixin_ts = 0.0

    async def _json(self, url, params, referer):
        """GET a Bilibili API endpoint and return parsed JSON or ``None``."""
        await self._ensure()
        headers = self._headers({"User-Agent": UA, "Referer": referer})
        try:
            async with self.session.get(
                url, params=params, headers=headers, timeout=self.timeout
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except Exception:
            return None

    async def _opus_detail(self, opus_id):
        """Fetch the full paragraph text of one opus post.

        Args:
            opus_id: The dynamic/opus id.

        Returns:
            ``(text, pic_urls)``; empty values when the detail is unavailable.
        """
        data = await self._json(
            OPUS_DETAIL,
            {"id": opus_id, "timezone_offset": "-480"},
            f"https://www.bilibili.com/opus/{opus_id}",
        )
        if not isinstance(data, dict) or data.get("code") != 0:
            return "", []
        item = (data.get("data") or {}).get("item")
        if not isinstance(item, dict):
            return "", []
        text, pics = _detail_content(item)
        return _clean(text), pics

    async def _video_desc(self, bvid):
        """Fetch the full description of one video."""
        data = await self._json(
            VIDEO_VIEW,
            {"bvid": bvid},
            f"https://www.bilibili.com/video/{bvid}",
        )
        if not isinstance(data, dict) or data.get("code") != 0:
            return ""
        desc = (data.get("data") or {}).get("desc")
        return _clean(desc) if isinstance(desc, str) else ""

    async def _article_text(self, cvid):
        """Fetch the full body of one cv article as plain text."""
        data = await self._json(
            ARTICLE_VIEW,
            {"id": cvid},
            f"https://www.bilibili.com/read/cv{cvid}",
        )
        if not isinstance(data, dict) or data.get("code") != 0:
            return ""
        content = (data.get("data") or {}).get("content")
        if not isinstance(content, str) or not content:
            return ""
        content = re.sub(r"<br\s*/?>|</p>|</div>", "\n", content, flags=re.IGNORECASE)
        content = re.sub(r"<[^>]+>", "", content)
        return _clean(html.unescape(content))

    async def _full_text(self, item):
        """Return the full original text of one raw dynamic item.

        Args:
            item: A raw item from the space feed.

        Returns:
            ``(text, pic_urls)``; empty when the feed payload is already all
            that the source provides (live, music, plain text, etc.).
        """
        modules = item.get("modules") or {}
        if isinstance(modules, list):
            modules = next((m for m in modules if isinstance(m, dict)), {}) or {}
        module_dynamic = modules.get("module_dynamic") or {}
        major = module_dynamic.get("major") or {}

        if isinstance(major.get("opus"), dict):
            oid = str(
                item.get("id_str") or (item.get("basic") or {}).get("id_str") or ""
            )
            if oid:
                return await self._opus_detail(oid)

        archive = major.get("archive")
        if isinstance(archive, dict) and archive.get("bvid"):
            desc = await self._video_desc(str(archive["bvid"]))
            return (desc, []) if desc else ("", [])

        article = major.get("article")
        if isinstance(article, dict) and article.get("id"):
            text = await self._article_text(str(article["id"]))
            return (text, []) if text else ("", [])
        return "", []

    async def _fill_full(self, raw_item, info):
        """Replace an item's feed summary with the full text when available."""
        modules = raw_item.get("modules") or {}
        module_dynamic = modules.get("module_dynamic") or {}
        body = (info.get("body") or "").strip()

        if (info.get("type") or "") == "DYNAMIC_TYPE_FORWARD":
            origin = raw_item.get("orig")
            if not isinstance(origin, dict):
                return
            origin_text, origin_pics = await self._full_text(origin)
            if not origin_text:
                return
            origin_dynamic = (origin.get("modules") or {}).get("module_dynamic") or {}
            origin_title, _ = _pick_body(origin_dynamic)
            if origin_title and not origin_text.startswith(origin_title):
                origin_text = origin_title + "\n" + origin_text
            own = _clean(_node_text(module_dynamic.get("desc")))
            candidate = (own + "\n\n↪ 转发原文：\n" + origin_text).strip()
            if len(candidate) >= len(body) or body.endswith("..."):
                info["body"] = candidate
            if origin_pics:
                merged = list(info.get("pics") or [])
                for url in origin_pics:
                    if url not in merged:
                        merged.append(url)
                info["pics"] = merged
            return

        text, pics = await self._full_text(raw_item)
        if text and (len(text) >= len(body) or body.endswith("...")):
            info["body"] = text
        if pics:
            merged = list(info.get("pics") or [])
            for url in pics:
                if url not in merged:
                    merged.append(url)
            info["pics"] = merged

    async def _enrich(self, raw_items):
        """Parse raw feed items and fill in their full text concurrently."""
        infos = [extract_item(item) for item in raw_items]
        await asyncio.gather(
            *(self._fill_full(raw, info) for raw, info in zip(raw_items, infos)),
            return_exceptions=True,
        )
        return infos

    async def _get_mixin_key(self):
        if self._mixin_key and time.time() - self._mixin_ts < 3600:
            return self._mixin_key
        await self._ensure()
        headers = self._headers(
            {
                "User-Agent": UA,
                "Referer": f"https://space.bilibili.com/{self.uid}/",
            }
        )
        async with self.session.get(NAV, headers=headers, timeout=self.timeout) as resp:
            data = await resp.json()
        wbi = ((data or {}).get("data") or {}).get("wbi_img") or {}
        img_key = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub_key = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if not img_key or not sub_key:
            raise RuntimeError("B站 wbi key 获取失败")
        raw = img_key + sub_key
        self._mixin_key = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]
        self._mixin_ts = time.time()
        return self._mixin_key

    async def _fetch_page(self, offset=""):
        await self._ensure()
        mixin_key = await self._get_mixin_key()
        params = {
            "host_mid": self.uid,
            "timezone_offset": "-480",
            "features": "itemOpusStyle",
            "offset": offset,
            "web_location": "333.999",
            "platform": "web",
            "wts": int(time.time()),
        }
        query = {
            key: "".join(ch for ch in str(value) if ch not in "!'()*")
            for key, value in params.items()
        }
        encoded = urllib.parse.urlencode(sorted(query.items()))
        query["w_rid"] = hashlib.md5((encoded + mixin_key).encode()).hexdigest()

        headers = self._headers(
            {
                "User-Agent": UA,
                "Referer": f"https://space.bilibili.com/{self.uid}/dynamic",
                "Origin": "https://space.bilibili.com",
                "Accept": "application/json, text/plain, */*",
            }
        )
        async with self.session.get(
            SPACE_FEED, params=query, headers=headers, timeout=self.timeout
        ) as resp:
            if resp.status in (412, 352):
                raise RuntimeError(
                    f"B站风控 HTTP {resp.status}：服务器 IP 被拦截，"
                    "可配置 bili_proxy（代理）或 bili_cookie（登录 Cookie）"
                )
            if resp.status != 200:
                raise RuntimeError(f"B站接口 HTTP {resp.status}")
            text = await resp.text()
        return json.loads(text) if text else {"code": -1, "message": "空响应"}

    async def fetch(self, limit=5):
        """拉取最新动态，返回按时间倒序排列的列表。"""
        limit = max(1, int(limit))
        async with self._lock:
            now = time.time()
            if (
                self._cache_items
                and now - self._cache_ts < self.cache_ttl
                and len(self._cache_items) >= limit
            ):
                return self._cache_items[:limit]

            items = []
            offset = ""
            attempt = 0
            while len(items) < limit and attempt < 4:
                attempt += 1
                try:
                    data = await self._fetch_page(offset)
                except Exception as exc:
                    logger.warning("B站动态请求失败（第 %s 次）：%s", attempt, exc)
                    await self._reset()
                    await asyncio.sleep(2)
                    continue

                code = data.get("code")
                if code != 0:
                    logger.warning(
                        "B站动态接口返回错误（第 %s 次）：code=%s message=%s",
                        attempt,
                        code,
                        data.get("message") or "",
                    )
                    if code in (-412, -352, 412, 352):
                        await self._reset()
                    await asyncio.sleep(2)
                    continue

                page = data.get("data") or {}
                batch = page.get("items") or []
                if not batch:
                    # 新指纹有时会先返回空 feed，换一次会话后通常就能拿到。
                    if attempt >= 2:
                        await self._reset()
                    await asyncio.sleep(2)
                    continue

                items.extend(batch)
                if len(items) >= limit:
                    break
                offset = page.get("offset") or ""
                if not page.get("has_more") or not offset:
                    break

            if items:
                parsed = await self._enrich(items)
                self._cache_ts = time.time()
                self._cache_items = parsed
                return parsed[:limit]

            # 本次失败时退回旧缓存，避免命令直接空手而归。
            if self._cache_items:
                logger.warning("B站动态拉取失败，本次使用旧缓存（%s 条）", len(self._cache_items))
                return self._cache_items[:limit]
            logger.warning("B站动态拉取失败：多次重试均未返回动态，可能被风控")
            return []

    async def close(self):
        await self._reset()
