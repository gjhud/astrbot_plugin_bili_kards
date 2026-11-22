"""动态配图本地缓存。

原图按动态 ID 归档到插件数据目录下的 picture/<动态ID>/，
同一张图只下载一次。缓存只增不删，永久归档。
发送前可选把多条动态的配图合并成少量长图，减少刷屏。
"""

import asyncio
import hashlib
import json
import os
import re
from urllib.parse import urlparse

import aiohttp

from . import data_paths

try:
    from PIL import Image as PILImage
except Exception:  # pragma: no cover - 缺库时自动退回逐张发送
    PILImage = None


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

SUPPORTED_EXT = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "heic"}
INDEX_NAME = "index.json"
ALBUMS_DIR = "_albums"
EXT_BY_HEAD = {
    b"\xff\xd8\xff": "jpg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"GIF8": "gif",
    b"BM": "bmp",
    b"RIFF": "webp",
    b"II*\x00": "heic",
    b"MM\x00*": "heic",
}


def _safe(text, limit=48):
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", str(text or ""))
    return text.strip("_")[:limit] or "item"


def cache_root():
    """插件数据目录下的 picture 文件夹。"""
    path = str(data_paths.PICTURE_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def album_root():
    """长图相册目录。"""
    path = os.path.join(cache_root(), ALBUMS_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def sheet_key(paths, params):
    """按原图指纹 + 参数生成长图指纹，用于复用。"""
    raw = [repr(params)]
    for p in paths:
        try:
            st = os.stat(p)
            raw.append(f"{p}:{int(st.st_mtime)}:{st.st_size}")
        except Exception:
            raw.append(str(p))
    return hashlib.md5("|".join(raw).encode()).hexdigest()[:20]


def build_sheets(paths, width=720, max_height=4096, quality=80):
    """把若干本地图片竖向拼成长图，返回长图路径列表。

    单张图、缺 PIL、动图或拼接失败时，原样返回原图列表。
    """
    paths = [p for p in paths or [] if p and os.path.isfile(p)]
    if len(paths) < 2 or PILImage is None:
        return paths

    width = max(240, min(int(width or 720), 1440))
    max_height = max(800, min(int(max_height or 4096), 12000))
    quality = max(40, min(int(quality or 80), 95))

    params = (width, max_height, quality)
    key = sheet_key(paths, params)
    cached = os.path.join(album_root(), f"{key}.jpg")
    if os.path.isfile(cached) and os.path.getsize(cached) > 0:
        return [cached]

    try:
        frames = []
        for path in paths:
            with PILImage.open(path) as im:
                im.load()
                if getattr(im, "n_frames", 1) > 1:
                    return paths
                if im.width > width * 3 or im.height > max_height * 2:
                    im.thumbnail((width * 2, max_height), PILImage.LANCZOS)
                frames.append(im.convert("RGB"))
    except Exception:
        return paths
    if len(frames) < 2:
        return paths

    target_w = max(1, min(width, max(f.width for f in frames)))
    sized = []
    for im in frames:
        ratio = target_w / float(im.width)
        sized.append((im, target_w, max(1, int(round(im.height * ratio)))))

    groups, bucket, height = [], [], 0
    for im, w, h in sized:
        if bucket and height + h > max_height:
            groups.append(bucket)
            bucket, height = [], 0
        bucket.append((im, w, h))
        height += h
    if bucket:
        groups.append(bucket)

    sheets = []
    try:
        for pos, bucket in enumerate(groups):
            total_h = sum(h for _, _, h in bucket)
            canvas = PILImage.new("RGB", (target_w, total_h), (255, 255, 255))
            offset = 0
            for im, w, h in bucket:
                canvas.paste(im.resize((w, h), PILImage.LANCZOS), (0, offset))
                offset += h
            out = (
                cached
                if len(groups) == 1
                else os.path.join(album_root(), f"{key}_{pos}.jpg")
            )
            canvas.save(out, "JPEG", quality=quality, optimize=True)
            canvas.close()
            if os.path.isfile(out) and os.path.getsize(out) > 0:
                sheets.append(out)
        for im, _, _ in sized:
            im.close()
    except Exception:
        return paths

    return sheets or paths


def _guess_ext(url, blob):
    head = blob[:12]
    for prefix, ext in EXT_BY_HEAD.items():
        if head.startswith(prefix):
            if ext == "webp" and blob[8:12] != b"WEBP":
                continue
            return ext
    stem = os.path.splitext(urlparse(str(url)).path)[1].lower().lstrip(".")
    return stem if stem in SUPPORTED_EXT else "jpg"


class ImageCache:
    """把动态配图下载到本地，返回本地绝对路径。"""

    def __init__(self, timeout=30, max_images=9):
        self.timeout = aiohttp.ClientTimeout(total=max(5, int(timeout)))
        self.max_images = max(0, int(max_images))
        # 长图拼接参数：宽度上限 720，单张长图高度上限 10000
        self.sheet_width = 720
        self.sheet_max_height = 10000
        self.session = None
        self._index = None
        self._lock = asyncio.Lock()

    def _load_index(self):
        if self._index is None:
            path = os.path.join(cache_root(), INDEX_NAME)
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                self._index = data if isinstance(data, dict) else {}
            except Exception:
                self._index = {}
        return self._index

    def _save_index(self):
        path = os.path.join(cache_root(), INDEX_NAME)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._index, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def lookup(self, url):
        """已归档则直接返回本地路径，否则返回空。"""
        key = hashlib.md5(str(url).encode()).hexdigest()
        entry = self._load_index().get(key) or {}
        path = entry.get("path") or ""
        return path if path and os.path.isfile(path) else ""

    async def _ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(headers={"User-Agent": UA})
        return self.session

    async def download(self, url, dynamic_id=""):
        """下载单张图，失败返回空字符串。"""
        url = str(url or "")
        if not url.startswith("http"):
            return ""
        hit = self.lookup(url)
        if hit:
            return hit

        key = hashlib.md5(url.encode()).hexdigest()
        folder = os.path.join(cache_root(), _safe(dynamic_id or "misc", 32))
        os.makedirs(folder, exist_ok=True)

        try:
            session = await self._ensure_session()
            headers = {
                "User-Agent": UA,
                "Referer": "https://www.bilibili.com/",
            }
            async with session.get(url, headers=headers, timeout=self.timeout) as resp:
                if resp.status != 200:
                    return ""
                blob = await resp.read()
        except Exception:
            return ""

        if not blob:
            return ""

        ext = _guess_ext(url, blob)
        path = os.path.join(folder, f"{key[:16]}.{ext}")
        try:
            with open(path, "wb") as f:
                f.write(blob)
        except Exception:
            return ""

        index = self._load_index()
        index[key] = {
            "url": url,
            "path": path,
            "dynamic_id": str(dynamic_id or ""),
            "bytes": len(blob),
        }
        self._index = index
        self._save_index()
        return path

    async def cache_item(self, item):
        """缓存一条动态的全部配图，返回本地路径列表。"""
        if self.max_images <= 0:
            return []
        urls = (item or {}).get("pics") or []
        did = (item or {}).get("id") or ""
        tasks = [self.download(url, did) for url in urls[: self.max_images]]
        if not tasks:
            return []
        async with self._lock:
            results = await asyncio.gather(*tasks)
        return [p for p in results if p]

    def purge_all(self):
        """清空全部已归档图片，返回 (删除文件数, 释放字节数)。"""
        root = cache_root()
        removed, freed = 0, 0
        for fname in os.listdir(root) if os.path.isdir(root) else []:
            if fname == INDEX_NAME:
                continue
            fpath = os.path.join(root, fname)
            if fname == ALBUMS_DIR:
                for dirpath, dirnames, filenames in os.walk(fpath, topdown=False):
                    for name in filenames:
                        fp = os.path.join(dirpath, name)
                        try:
                            freed += os.path.getsize(fp)
                            os.remove(fp)
                            removed += 1
                        except Exception:
                            continue
                    try:
                        if not os.listdir(dirpath):
                            os.rmdir(dirpath)
                    except Exception:
                        pass
                continue
            if os.path.isfile(fpath):
                try:
                    freed += os.path.getsize(fpath)
                    os.remove(fpath)
                    removed += 1
                except Exception:
                    continue
        self._index = {}
        self._save_index()
        return removed, freed

    def merge_pics(self, paths):
        """把一条动态的配图拼成少量长图，参数不可用时原样返回。"""
        try:
            return build_sheets(
                paths,
                width=self.sheet_width,
                max_height=self.sheet_max_height,
            )
        except Exception:
            return [p for p in paths or [] if p]

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None
