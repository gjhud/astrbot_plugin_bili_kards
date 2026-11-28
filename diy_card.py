"""DIY card renderer prototype.

Draws a KARDS-like 500x702 card face from a dict spec.  Font choice was
calibrated against existing KARDS card art by a vision model: SimHei for
Chinese, a condensed Latin face for cost/attack/defense numbers.  A bundled
copy of each font lives in ``fonts/`` so the renderer stays consistent even
on machines without those system fonts.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 500, 702

FONT_DIR = Path(__file__).resolve().parent / "fonts"


def _bundled_fonts(
    profile_key: str, fallback: str, recommendations_key: str = "model_recommendations"
) -> tuple[str, ...]:
    """Return bundled font paths from the model-generated profile first."""
    result: list[str] = []
    try:
        profile = json.loads(
            (FONT_DIR / "font_profile.json").read_text(encoding="utf-8")
        )
        name = str(profile.get(profile_key) or fallback)
        result.append(str(FONT_DIR / name))
        for item in profile.get(recommendations_key, []):
            candidate = str(item)
            if candidate.endswith((".ttf", ".ttc")):
                result.append("C:/Windows/Fonts/" + candidate)
    except (OSError, ValueError, TypeError):
        result.append(str(FONT_DIR / fallback))
    return tuple(dict.fromkeys(result))


CN_FONTS = _bundled_fonts("bundled_font", "kards_card_font.ttf") + (
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/msyhbd.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)
NUM_FONTS = _bundled_fonts(
    "bundled_number_font", "kards_card_number.ttf", "number_recommendations"
) + (
    "C:/Windows/Fonts/bahnschrift.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)

FACTION_CN = {
    "Germany": "德国", "Britain": "英国", "Japan": "日本", "Soviet": "苏联",
    "USA": "美国", "France": "法国", "Italy": "意大利", "Poland": "波兰",
    "Finland": "芬兰", "Anzac": "澳新军团", "Neutral": "中立",
}
TYPE_CN = {
    "infantry": "步兵", "tank": "坦克", "artillery": "火炮",
    "fighter": "战斗机", "bomber": "轰炸机", "order": "指令",
    "countermeasure": "反制",
}
RARITY_CN = {
    "Standard": "普通", "Limited": "限定", "Special": "特殊",
    "Elite": "精英", "Token": "衍生牌",
}
FACTION_COLOR = {
    "Germany": (138, 143, 152), "Britain": (59, 130, 246),
    "Japan": (239, 68, 68), "Soviet": (185, 28, 28),
    "USA": (34, 197, 94), "France": (96, 165, 250),
    "Italy": (74, 222, 128), "Poland": (245, 158, 11),
    "Finland": (56, 189, 248), "Anzac": (249, 115, 22),
    "Neutral": (163, 163, 163),
}
RARITY_COLOR = {
    "Standard": (150, 155, 162), "Limited": (64, 145, 235),
    "Special": (224, 176, 62), "Elite": (166, 96, 220),
    "Token": (120, 120, 120),
}

FRAME = (18, 22, 28)
PAPER = (232, 224, 204)
PANEL = (246, 241, 226)
INK = (26, 30, 36)
MUTED = (104, 104, 100)
GOLD = (204, 158, 56)

ART_BOX = (24, 92, 476, 466)
DESC_BOX = (24, 518, 476, 634)


def _font(size: int, kind: str = "cn") -> ImageFont.FreeTypeFont:
    candidates = NUM_FONTS if kind == "num" else CN_FONTS
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _fit_font(draw, text, size, max_width, min_size=12, kind="cn"):
    while size > min_size:
        font = _font(size, kind)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 1
    return _font(min_size, kind)


def _wrap(draw, text, font, max_width):
    lines, current = [], ""
    for ch in text:
        candidate = current + ch
        if ch == "\n":
            lines.append(current)
            current = ""
            continue
        if current and draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = ch
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def _cover_art(art: Image.Image, box) -> Image.Image:
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    scale = max(bw / art.width, bh / art.height)
    resized = art.convert("RGB").resize(
        (max(1, round(art.width * scale)), max(1, round(art.height * scale))),
        Image.LANCZOS,
    )
    left = (resized.width - bw) // 2
    top = (resized.height - bh) // 2
    return resized.crop((left, top, left + bw, top + bh))


def _circle_number(draw, center, radius, fill, text, font):
    x, y = center
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill,
                 outline=FRAME, width=3)
    draw.text((x, y + 1), text, font=font, fill=(255, 255, 255), anchor="mm")


def render_card(spec: dict, art: Image.Image | None = None) -> Image.Image:
    """Return a 500x702 card image for *spec*."""
    name = str(spec.get("name") or spec.get("name_zh") or "未命名")
    name_en = str(spec.get("name_en") or "")
    faction = str(spec.get("faction") or "Neutral")
    card_type = str(spec.get("card_type") or "infantry")
    rarity = str(spec.get("rarity") or "Standard")
    cost = str(spec.get("cost") if spec.get("cost") is not None else "")
    attack = str(spec.get("attack") if spec.get("attack") is not None else "")
    defense = str(spec.get("defense") if spec.get("defense") is not None else "")
    description = str(spec.get("description") or "")
    keywords = spec.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.replace("，", ",").split(",") if k.strip()]

    faction_cn = FACTION_CN.get(faction, faction)
    type_cn = TYPE_CN.get(card_type, card_type)
    rarity_cn = RARITY_CN.get(rarity, rarity)
    accent = FACTION_COLOR.get(faction, (163, 163, 163))
    gem = RARITY_COLOR.get(rarity, (163, 163, 163))

    canvas = Image.new("RGB", (W, H), FRAME)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((4, 4, W - 5, H - 5), radius=16, fill=PAPER,
                           outline=GOLD, width=3)

    # header
    draw.rectangle((10, 10, W - 11, 84), fill=(30, 36, 46))
    draw.rectangle((10, 82, W - 11, 86), fill=GOLD)
    name_font = _fit_font(draw, name, 28, 350, 14)
    draw.text((W // 2 + 14, 46), name, font=name_font, fill=(248, 244, 234), anchor="mm")
    if name_en:
        en_font = _fit_font(draw, name_en, 13, 330, 9)
        draw.text((W // 2 + 14, 70), name_en, font=en_font, fill=(176, 186, 200), anchor="mm")

    _circle_number(draw, (48, 47), 31, GOLD, cost, _font(40, "num"))
    # rarity diamond
    draw.polygon([(452, 18), (478, 47), (452, 76), (426, 47)], fill=gem,
                 outline=FRAME)
    rf = _fit_font(draw, rarity_cn, 14, 44, 9)
    draw.text((452, 47), rarity_cn, font=rf, fill=(255, 255, 255), anchor="mm")

    # art
    if art is not None:
        canvas.paste(_cover_art(art, ART_BOX), (ART_BOX[0], ART_BOX[1]))
    else:
        draw.rectangle(ART_BOX, fill=(58, 64, 72))
        for i in range(0, ART_BOX[2] - ART_BOX[0], 28):
            draw.line((ART_BOX[0] + i, ART_BOX[3], ART_BOX[0] + i + 180, ART_BOX[1]),
                      fill=(70, 78, 88), width=10)
        draw.text((W // 2, 279), "DIY 卡图", font=_font(34), fill=(220, 226, 235), anchor="mm")
    draw.rectangle(ART_BOX, outline=FRAME, width=3)

    # faction / type strip
    draw.rectangle((10, 474, W - 11, 512), fill=accent)
    draw.text((24, 493), type_cn, font=_font(20), fill=(255, 255, 255), anchor="lm")
    draw.text((W - 24, 493), faction_cn, font=_font(18), fill=(255, 255, 255), anchor="rm")

    # description
    draw.rounded_rectangle(DESC_BOX, radius=8, fill=PANEL, outline=FRAME, width=2)
    desc_font = _font(17)
    lines = _wrap(draw, description, desc_font, DESC_BOX[2] - DESC_BOX[0] - 28)
    if keywords:
        lines = _wrap(draw, "【" + " · ".join(str(k) for k in keywords) + "】",
                      desc_font, DESC_BOX[2] - DESC_BOX[0] - 28) + lines
    max_lines = 6
    lines = lines[:max_lines]
    if len(lines) == max_lines and lines:
        last = lines[-1]
        while last and draw.textlength(last + "…", font=desc_font) > DESC_BOX[2] - DESC_BOX[0] - 28:
            last = last[:-1]
        lines[-1] = last + "…"
    for idx, line in enumerate(lines):
        draw.text((DESC_BOX[0] + 14, DESC_BOX[1] + 12 + idx * 18), line,
                  font=desc_font, fill=INK)

    # stats
    draw.rectangle((10, 638, W - 11, H - 10), fill=(30, 36, 46))
    if card_type in ("order", "countermeasure"):
        draw.text((64, 668), "—", font=_font(34, "num"), fill=(210, 214, 220), anchor="mm")
        draw.text((436, 668), "—", font=_font(34, "num"), fill=(210, 214, 220), anchor="mm")
    else:
        _circle_number(draw, (64, 668), 29, (158, 48, 48), attack, _font(38, "num"))
        _circle_number(draw, (436, 668), 29, (48, 92, 158), defense, _font(38, "num"))
    draw.text((W // 2, 670), "DIY", font=_font(13, "num"), fill=(150, 158, 170), anchor="mm")
    return canvas


def render_card_png(spec: dict, art: Image.Image | None = None) -> bytes:
    """Render a card PNG, preferring the bundled KARDS templates.

    Template assets are opened one at a time and released after compositing,
    so even a failed template render only costs a few megabytes.  If the
    assets are missing or unusable, fall back to the procedural renderer.
    """
    try:
        from . import card_template

        if card_template.available():
            return card_template.render_template_png(spec, art)
    except Exception:
        pass
    buf = io.BytesIO()
    render_card(spec, art).save(buf, "PNG")
    return buf.getvalue()


def art_from_bytes(raw: bytes) -> Image.Image:
    return Image.open(io.BytesIO(raw)).convert("RGB")
