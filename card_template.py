"""Template-based KARDS-style card renderer.

Uses the bundled fan-project card templates and fonts so DIY cards look like
real KARDS cards instead of a purely procedural drawing.  Assets are opened
one at a time and released immediately after compositing, keeping memory use
to a few megabytes per render.  No background service or model is involved.

Asset source: KARDS-Card-Builder (Babaika328), an unofficial fan project made
under 1939 Games' Community Content Policy.  Use is personal/non-commercial.
"""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageStat

ASSET_DIR = Path(__file__).resolve().parent / "assets" / "kards_builder"
TEMPLATE_DIR = ASSET_DIR / "Templates"
FONT_DIR = ASSET_DIR / "Fonts"
CJK_FONT = Path(__file__).resolve().parent / "fonts" / "kards_card_font.ttf"

CANVAS = (650, 650)
CARD_BOX = (113, 28, 535, 621)
OUT_SIZE = (500, 702)
ART_BOX = (120, 30, 411, 460)
HEADER_BOX = (195, 39, 332, 66)
# Offsets are expressed in final 500x702 output pixels; convert to template px.
OUT_PER_TEMPLATE_X = OUT_SIZE[0] / (CARD_BOX[2] - CARD_BOX[0])
OUT_PER_TEMPLATE_Y = OUT_SIZE[1] / (CARD_BOX[3] - CARD_BOX[1])

FACTION_FILE = {
    "Germany": "germany", "Britain": "britain", "Japan": "japan",
    "Soviet": "ussr", "USA": "usa", "France": "france",
    "Italy": "italy", "Poland": "poland", "Finland": "finland",
    "Anzac": "anzac", "Neutral": "neutral",
}
UNIT_FILE = {
    "infantry": "infantry", "tank": "tank", "artillery": "artillery",
    "fighter": "fighter", "bomber": "bomber",
}
AIR_TYPES = {"fighter", "bomber"}
# Defensive normalisation: an unrecognised type used to fall through to the
# countermeasure template, which looked like "炮兵 became 反制".
CARD_TYPE_ALIASES = {
    "炮兵": "artillery", "炮": "artillery", "火炮": "artillery",
    "步兵": "infantry", "坦克": "tank", "战斗机": "fighter",
    "轰炸机": "bomber", "指令": "order", "反制": "countermeasure",
}
RARITY_FILE = {
    "Standard": "standard", "Limited": "limited",
    "Special": "special_1", "Elite": "elite_1", "Token": "standard",
}
NAME_COLOR = {
    "Finland": (53, 55, 61),
}
DEFAULT_NAME_COLOR = (186, 192, 170)
BODY_COLOR = (82, 85, 89)
COST_COLOR = (181, 185, 165)
STAT_COLOR = (191, 197, 183)


def available() -> bool:
    """Return True when the bundled template assets are present."""
    return (TEMPLATE_DIR / "Units" / "tank.png").is_file()


def _open(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGBA")


_FONT_CACHE: dict[tuple[int, bool, bool], ImageFont.FreeTypeFont] = {}
_FONT_CACHE_LIMIT = 128


def _font(size: int, cjk: bool = False, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Load a font with a bounded cache.

    Loading the CJK font per glyph would allocate hundreds of megabytes, so
    font objects are reused.  The cache is capped and cleared wholesale to
    keep long-running bot processes predictable.
    """
    key = (size, cjk, bold)
    font = _FONT_CACHE.get(key)
    if font is not None:
        return font
    if cjk:
        path = CJK_FONT
    elif bold:
        path = FONT_DIR / "Roboto Black.ttf"
    else:
        path = FONT_DIR / "Franklin Gothic Medium Cond Regular.ttf"
    try:
        font = ImageFont.truetype(str(path), size)
    except OSError:
        font = ImageFont.load_default()
    if len(_FONT_CACHE) >= _FONT_CACHE_LIMIT:
        _FONT_CACHE.clear()
    _FONT_CACHE[key] = font
    return font


def _text_items(text: str, size: int, bold: bool):
    items = []
    for ch in str(text):
        cjk = ord(ch) > 0x2E80
        items.append((ch, _font(size, cjk=cjk, bold=bold)))
    return items


def _text_width(draw, items) -> float:
    return sum(draw.textlength(ch, font=font) for ch, font in items)


def _draw_centered(draw, xy, text, size, fill, max_width, bold=False):
    """Draw mixed Chinese/Latin text centred at ``xy`` (vertical middle)."""
    items = _text_items(text, size, bold)
    width = _text_width(draw, items)
    while width > max_width and size > 8:
        size -= 1
        items = _text_items(text, size, bold)
        width = _text_width(draw, items)
    cx, cy = xy
    x = cx - width / 2
    for ch, font in items:
        draw.text((x, cy), ch, font=font, fill=fill, anchor="lm")
        x += draw.textlength(ch, font=font)


def _wrap_lines(draw, text, size, max_width):
    items = _text_items(text, size, False)
    lines, current, width = [], [], 0.0
    for ch, font in items:
        char_width = draw.textlength(ch, font=font)
        if current and width + char_width > max_width:
            lines.append(current)
            current, width = [], 0.0
        current.append((ch, font))
        width += char_width
    if current:
        lines.append(current)
    return lines


BODY_MIN_SIZE = 11
BODY_MAX_SIZE = 20
BODY_BOTTOM = 613  # template px; leaves a small margin above the card edge


def _adaptive_body_size(draw, text, max_width, start_y, bottom=BODY_BOTTOM):
    """Return the largest body font whose wrapped lines fit above ``bottom``.

    Short descriptions get a bigger font; longer ones are stepped down until
    the whole text fits.  When even the minimum size overflows, the minimum is
    returned and ``_draw_body`` ellipsises the last visible line.
    """
    available = max(1.0, bottom - start_y)
    for size in range(BODY_MAX_SIZE, BODY_MIN_SIZE - 1, -1):
        if len(_wrap_lines(draw, text, size, max_width)) * size * 1.2 <= available:
            return size
    return BODY_MIN_SIZE


def _draw_body(draw, cx, start_y, text, size, max_width, bottom=BODY_BOTTOM):
    lines = _wrap_lines(draw, text, size, max_width)
    max_lines = max(1, int((bottom - start_y) / (size * 1.2)))
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:-1] + [("…", _font(size))]
    y = float(start_y)
    for line in lines:
        width = sum(draw.textlength(ch, font=font) for ch, font in line)
        x = cx - width / 2
        for ch, font in line:
            # Default anchor (ascender) keeps CJK punctuation at its designed
            # bottom position.  "lt" aligns the ink top instead, which made
            # ，。：、 jump to the top of each line and look like apostrophes.
            draw.text((x, y), ch, font=font, fill=BODY_COLOR)
            x += draw.textlength(ch, font=font)
        y += size * 1.2


def _number(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _paste_art(canvas: Image.Image, art, offset_x=0.0, offset_y=0.0, zoom=1.0) -> None:
    """Paste card art with offsets and zoom.

    ``offset_x``/``offset_y`` are final 500x702 output pixels: positive x means
    LEFT, positive y means UP; negative values move the opposite way.
    ``zoom`` 1.0 is the default cover fit, >1
    zooms in (centre crop) and <1 zooms out (edges filled with the image's
    average colour).  Offsets are clamped to +/-200 px and zoom to 0.2-5.0.
    """
    if art is None:
        return
    source = art.convert("RGB") if hasattr(art, "convert") else None
    if source is None:
        return
    box_x, box_y, box_w, box_h = ART_BOX
    # User-facing px are output pixels; internal crop works in template pixels.
    offset_x = max(-200.0, min(200.0, _number(offset_x, 0.0))) / OUT_PER_TEMPLATE_X
    offset_y = max(-200.0, min(200.0, _number(offset_y, 0.0))) / OUT_PER_TEMPLATE_Y
    zoom = max(0.2, min(5.0, _number(zoom, 1.0)))

    base_scale = max(box_w / source.width, box_h / source.height)
    scale = base_scale * zoom
    new_w = max(1, round(source.width * scale))
    new_h = max(1, round(source.height * scale))
    resized = source.resize((new_w, new_h), Image.Resampling.LANCZOS)

    # Pad the resized art so offsets and zoom-out never leave holes in frame.
    gap_x = (box_w - new_w) / 2.0
    gap_y = (box_h - new_h) / 2.0
    pad = int(max(abs(offset_x), abs(offset_y))
              + max(0.0, gap_x) + max(0.0, gap_y) + 8)
    average = tuple(int(v) for v in ImageStat.Stat(source).mean)
    padded = Image.new("RGB", (box_w + 2 * pad, box_h + 2 * pad), average)
    padded.paste(resized, (int(round(pad + gap_x)), int(round(pad + gap_y))))
    crop_x = int(round(pad + offset_x))
    crop_y = int(round(pad + offset_y))
    crop = padded.crop((crop_x, crop_y, crop_x + box_w, crop_y + box_h))
    canvas.alpha_composite(crop.convert("RGBA"), dest=(box_x, box_y))

    source.close()
    resized.close()
    padded.close()
    crop.close()


def _template_path(card_type: str, cost: int) -> Path:
    two_digit = len(str(cost)) >= 2
    if card_type in UNIT_FILE:
        base = TEMPLATE_DIR / "Units" / (UNIT_FILE[card_type] + ".png")
        variant = TEMPLATE_DIR / "Units" / (UNIT_FILE[card_type] + "_2.png")
    elif card_type == "order":
        base = TEMPLATE_DIR / "Orders_Countermeasures" / "order.png"
        variant = TEMPLATE_DIR / "Orders_Countermeasures" / "order_2.png"
    else:
        base = TEMPLATE_DIR / "Orders_Countermeasures" / "countermeasure.png"
        variant = TEMPLATE_DIR / "Orders_Countermeasures" / "countermeasure_2.png"
    return variant if two_digit and variant.is_file() else base


def render_template(spec: dict, art=None) -> Image.Image:
    """Render a card using the bundled KARDS templates; returns 500x702 RGBA."""
    raw_type = str(spec.get("card_type") or "infantry").strip()
    card_type = CARD_TYPE_ALIASES.get(raw_type, raw_type.lower())
    if card_type not in UNIT_FILE and card_type not in ("order", "countermeasure"):
        card_type = "infantry"
    faction = str(spec.get("faction") or "Neutral").strip()
    rarity = str(spec.get("rarity") or "Standard").strip()
    try:
        cost = int(spec.get("cost") or 0)
    except (TypeError, ValueError):
        cost = 0
    try:
        attack = int(spec.get("attack") or 0)
    except (TypeError, ValueError):
        attack = 0
    try:
        defense = int(spec.get("defense") or 0)
    except (TypeError, ValueError):
        defense = 0
    name = str(spec.get("name") or spec.get("name_zh") or "未命名")
    description = str(spec.get("description") or "")
    keywords = spec.get("keywords") or []
    if isinstance(keywords, (list, tuple)):
        keywords = " · ".join(str(item) for item in keywords)
    else:
        keywords = str(keywords)

    canvas = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    _paste_art(
        canvas, art,
        offset_x=spec.get("art_x", 0),
        offset_y=spec.get("art_y", 0),
        zoom=spec.get("art_scale", 1),
    )

    with _open(_template_path(card_type, cost)) as base:
        canvas.alpha_composite(base)

    is_unit = card_type in UNIT_FILE
    faction_file = FACTION_FILE.get(faction, "neutral")
    if is_unit:
        air = "air" if card_type in AIR_TYPES else "ground"
        header_name = "neutral.jpg" if faction_file == "neutral" else f"{faction_file}_{air}.jpg"
        header_path = TEMPLATE_DIR / "Units" / "Headers" / header_name
        if header_path.is_file():
            with _open(header_path) as header:
                canvas.alpha_composite(header.resize((HEADER_BOX[2], HEADER_BOX[3])),
                                       dest=(HEADER_BOX[0], HEADER_BOX[1]))
    else:
        nation_path = TEMPLATE_DIR / "Orders_Countermeasures" / "nations" / f"{faction_file}.png"
        if nation_path.is_file():
            with _open(nation_path) as nation:
                nx, ny = (466, 43) if faction_file in ("usa", "ussr") else (469, 50)
                canvas.alpha_composite(nation, dest=(nx, ny))

    rarity_file = RARITY_FILE.get(rarity, "standard")
    rarity_path = TEMPLATE_DIR / "Rarity" / f"{rarity_file}.png"
    if rarity_path.is_file():
        with _open(rarity_path) as icon:
            rx = 311 if rarity == "Special" else 302 if rarity == "Standard" else 306
            canvas.alpha_composite(icon, dest=(rx, 600))

    draw = ImageDraw.Draw(canvas)
    # Nudge all numerals up by 4 template pixels (~5 px in the 500x702 output).
    _draw_centered(draw, (145, 72), str(cost), 62 if cost < 10 else 45,
                   COST_COLOR, 90, bold=True)
    if is_unit:
        _draw_centered(draw, (222, 464), str(attack), 50 if attack < 10 else 37,
                       STAT_COLOR, 100, bold=True)
        _draw_centered(draw, (427, 464), str(defense), 50 if defense < 10 else 37,
                       (196, 201, 187), 100, bold=True)

    name_color = NAME_COLOR.get(faction, DEFAULT_NAME_COLOR)
    if is_unit:
        # Upstream KARDS-Card-Builder: nameFontSize 47, nameAreaWidth 255,
        # centre (332.5, 73), bold FranklinMed with auto-fit down to 6px.
        _draw_centered(draw, (332, 73), name, 47, name_color, 255, bold=True)
        if keywords:
            _draw_centered(draw, (325, 504), keywords, 15, BODY_COLOR, 360, bold=False)
            text_y = 534
        else:
            text_y = 504
    else:
        # Upstream: orderNameFontSize 40, maxWidth 410, top baseline at y=466,
        # dark #44474d on the parchment strip.  Our centre-anchor y is half a
        # font height lower to reproduce the top baseline.  The name is raised
        # by 10 final-output pixels (converted to template pixels here).
        name_y = 486 - 10 / OUT_PER_TEMPLATE_Y
        _draw_centered(draw, (325, name_y), name, 40, (68, 71, 77), 410, bold=True)
        if keywords:
            _draw_centered(draw, (325, 507), keywords, 15, BODY_COLOR, 360, bold=False)
        text_y = 536
    if description:
        # Auto-fit: short text is enlarged, long text shrink-wrapped so it
        # never spills past the bottom of the card frame.
        body_size = _adaptive_body_size(draw, description, 360, text_y)
        _draw_body(draw, 325, text_y, description, body_size, 360)

    card = canvas.crop(CARD_BOX).resize(OUT_SIZE, Image.Resampling.LANCZOS)
    canvas.close()
    return card


def render_template_png(spec: dict, art=None) -> bytes:
    buf = io.BytesIO()
    render_template(spec, art).save(buf, "PNG")
    return buf.getvalue()
