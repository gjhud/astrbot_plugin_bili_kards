import asyncio
import hashlib
import time
import urllib.parse

import aiohttp

HEAD = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

MIXIN = [
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


BILI_SPI = "https://api.bilibili.com/x/frontend/finger/spi"
NAV = "https://api.bilibili.com/x/web-interface/nav"


def mixin_key(img, sub):
    raw = img + sub
    return "".join(raw[i] for i in MIXIN)[:32]


def strip_val(v):
    bad = ["!", "'", "(", ")", "*"]
    v = str(v)
    for c in bad:
        v = v.replace(c, "")
    return v


def wbi_sign(params, mixin):
    p = dict(params)
    p["wts"] = int(time.time())
    q = {k: strip_val(x) for k, x in p.items()}
    qs = urllib.parse.urlencode(sorted(q.items()))
    q["w_rid"] = hashlib.md5((qs + mixin).encode()).hexdigest()
    return q


async def fetch(sess, url, params=None):
    async with sess.get(url, params=params, timeout=15) as r:
        return await r.json()


busy = asyncio.Semaphore(3)
_cache = {"mixin": None, "ts": 0.0, "buv": None, "cookie": ""}


async def get_keys(sess):
    now = time.time()
    if _cache["mixin"] and now - _cache["ts"] < 21600:
        return _cache["mixin"]
    spi = await fetch(sess, BILI_SPI)
    d = (spi or {}).get("data") or {}
    if d.get("b_3"):
        _cache["buv"] = "buv2=" + d["b_3"]
    nav = await fetch(sess, NAV)
    wi = ((nav or {}).get("data") or {}).get("wbi_img") or {}
    img, sub = wi.get("img_url"), wi.get("sub_url")
    if not img or not sub:
        return None
    img = img.rsplit("/", 1)[-1].split(".")[0]
    sub = sub.rsplit("/", 1)[-1].split(".")[0]
    _cache["mixin"] = mixin_key(img, sub)
    _cache["ts"] = now
    return _cache["mixin"]


async def _get(sess, url, params=None, sign=True, buv=False):
    async with busy:
        p = dict(params or {})
        if sign:
            m = await get_keys(sess)
            if m:
                p = wbi_sign(p, m)
        head = dict(HEAD)
        if buv and _cache["buv"]:
            head["Cookie"] = _cache["buv"]
        for i in range(2):
            try:
                async with sess.get(url, params=p, headers=head, timeout=15) as r:
                    return await r.json()
            except Exception:
                _cache["mixin"] = None
                if i:
                    return {"code": -1, "message": "request failed"}
        return {"code": -1, "message": "request failed"}


API = {
    "video": "https://api.bilibili.com/x/web-interface/view",
    "user": "https://api.bilibili.com/x/space/wbi/acc/info",
    "stat": "https://api.bilibili.com/x/relation/stat",
    "search": "https://api.bilibili.com/x/web-interface/wbi/search/type",
    "popular": "https://api.bilibili.com/x/web-interface/popular",
    "rank": "https://api.bilibili.com/x/web-interface/ranking/v2",
}


async def get_video(sess, bvid=None, aid=None, cid=False):
    r = await _get(
        sess,
        API["video"],
        {"bvid": bvid, "aid": aid, "cid": "1" if cid else None},
        sign=False,
    )
    return (
        (r or {}).get("data")
        if (r or {}).get("code") == 0
        else {"error": (r or {}).get("message")}
    )


async def get_user(sess, mid):
    r = await _get(sess, API["user"], {"mid": mid}, buv=True)
    if (r or {}).get("code") != 0:
        return {"error": (r or {}).get("message")}
    return (r or {}).get("data")


async def get_stat(sess, mid):
    r = await _get(sess, API["stat"], {"vmid": mid}, sign=False)
    return ((r or {}).get("data") or {}).get("follower")


async def search(sess, kw, page=1):
    r = await _get(
        sess,
        API["search"],
        {"search_type": "video", "keyword": kw, "page": page},
        buv=True,
    )
    d = (r or {}).get("data") or {}
    res = []
    for v in d.get("result") or []:
        t = (
            (v.get("title") or "")
            .replace('<em class="keyword">', "")
            .replace("</em>", "")
        )
        res.append(
            {
                "title": t,
                "author": v.get("author"),
                "bvid": v.get("bvid"),
                "play": v.get("play"),
                "url": v.get("arcurl"),
            }
        )
    return res


async def popular(sess, limit=5):
    r = await _get(sess, API["popular"], {"ps": 20, "pn": 1})
    ls = ((r or {}).get("data") or {}).get("list") or []
    return [
        {
            "title": x.get("title"),
            "author": x.get("owner", {}).get("name"),
            "bvid": x.get("bvid"),
            "play": (x.get("stat") or {}).get("view"),
        }
        for x in ls[:limit]
    ]


async def demo(q):
    async with aiohttp.ClientSession(headers=HEAD) as s:
        print(await search(s, q or "KARDS"))


if __name__ == "__main__":
    asyncio.run(demo("KARDS"))
