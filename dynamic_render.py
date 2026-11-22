"""动态图文混排与分段发送。

一条动态 -> 若干段：
    {"text": "...", "pics": ["/abs/a.jpg", ...]}

规则：
1. 正文按原始顺序（标题、换行、段落）保留，配图贴在它原本的位置
2. 不做单条字数截断，改为按段落自然分段，单段超过 PARAGRAPH_LIMIT 字再拆
3. 每段至少配一张图；该段没有可用配图时用纯白占位图补上
"""

import os

from . import data_paths

# 董事长要求：单段按 200 字切分，条数不限，直到正文全部发完
PARAGRAPH_LIMIT = 200
BLANK_NAME = "blank.png"


def blank_image():
    """纯白占位图路径，本地生成一次后复用。"""
    root = str(data_paths.PICTURE_DIR)
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, BLANK_NAME)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    try:
        from PIL import Image

        Image.new("RGB", (64, 64), (255, 255, 255)).save(path)
    except Exception:
        # 兜底：1x1 白色 PNG
        data = bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010802000000"
            "90f7f7de0000000c49444154789c63f8ffff3f0005fe02fea735818400"
            "0000000049454e44ae426082"
        )
        with open(path, "wb") as fp:
            fp.write(data)
    return path


def split_paragraphs(text):
    """按行切段，丢掉空行，保留原始次序。"""
    out = []
    for line in (text or "").replace("\r", "").split("\n"):
        line = line.strip()
        if line:
            out.append(line)
    return out


def group_segments(paragraphs, pics, limit=PARAGRAPH_LIMIT, placeholder=True):
    """把段落和图片交错打包成段列表。

    连续段落会合并到同一段，单段最多 limit 字；只有超过 limit 的长段落
    才按句末标点或字符拆开。每段领一张图，图片按原始顺序对应段落；没有
    图可用时补纯白占位图，保证开启配图时不会出现纯文本段。
    ``placeholder=False`` 用于关闭配图的场景，此时只按段落切分文字。

    Args:
        paragraphs: 按原始顺序排列的标题和正文段落。
        pics: 本条动态可用的本地图片路径，按原始顺序排列。
        limit: 单段最大字符数。
        placeholder: 图片不足时是否补纯白占位图。

    Returns:
        分段列表，每段至多一张图；多余图片会单独成段，避免丢图。
    """
    pics = [p for p in (pics or []) if p]
    blank = blank_image() if placeholder else None
    segments = []
    buf = []
    size = 0

    def take_pics():
        if pics:
            return [pics.pop(0)]
        if blank:
            return [blank]
        return []

    def flush():
        nonlocal buf, size
        if not buf:
            return
        segments.append({"text": "\n".join(buf), "pics": take_pics()})
        buf = []
        size = 0

    for para in paragraphs:
        rest = (para or "").strip()
        if not rest:
            continue
        while len(rest) > limit:
            flush()
            cut = rest[:limit]
            # 优先在句末标点处断开，避免把句子切成两半
            stop = max(cut.rfind(ch) for ch in "。！？!?；;…")
            if stop >= limit // 2:
                cut = cut[: stop + 1]
            segments.append({"text": cut, "pics": take_pics()})
            rest = rest[len(cut) :].lstrip()
        if not rest:
            continue
        # 合并短段落，但不超过单段上限
        if buf and size + 1 + len(rest) > limit:
            flush()
        if buf:
            size += 1
        buf.append(rest)
        size += len(rest)

    flush()
    # 剩余图片单独成段，避免丢图（不设条数上限，发完为止）
    while pics:
        segments.append({"text": "", "pics": [pics.pop(0)]})
    return segments


def build_segments(title, body, url, pics, limit=PARAGRAPH_LIMIT, placeholder=True):
    """由动态字段组装分段结果，结尾附原帖链接。"""
    paras = []
    if title:
        paras.append(str(title).strip())
    paras.extend(split_paragraphs(body))
    if url:
        paras.append("链接: " + str(url).strip())
    return group_segments(paras, pics, limit, placeholder=placeholder)


def render_plain(segments, with_pics=True):
    """纯文本渲染，供终端/无图渠道使用。"""
    blocks = []
    for seg in segments:
        items = []
        if seg.get("text"):
            items.append(seg["text"])
        if with_pics:
            items.extend("图: " + p for p in seg.get("pics") or [])
        if items:
            blocks.append("\n".join(items))
    return blocks
