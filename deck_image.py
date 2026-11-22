"""Render a decoded KARDS deck as a shareable deck image.

The renderer uses the Chinese card art served through the public ``wsrv.nl``
image proxy (the official ``www.kards.com`` host is not reachable from every
network).  Card art is cached on disk, and a text-only placeholder is drawn
when an image cannot be downloaded, so image generation never blocks the
command entirely.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import math
import socket
from pathlib import Path
from urllib.parse import quote

import aiohttp
from PIL import Image, ImageDraw, ImageFont

from . import data_paths, deck_code

FONT_PATH = "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"
MONO_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"

PLUGIN_DIR = data_paths.PLUGIN_DIR
LOCAL_THUMB_DIR = data_paths.THUMB_DIR
CARD_CACHE_DIR = data_paths.CARD_CACHE_DIR
IMAGE_CACHE_DIR = data_paths.IMAGE_CACHE_DIR

CARD_IMAGE_URL = (
    "https://wsrv.nl/?url="
    "www.kards.com%2Fimages%2Fcard%2Fv53%2Fzh-Hans%2F{card_id}.avif"
    "&output=webp&w=400"
)
CARD_IMAGE_FALLBACK = (
    "https://images.weserv.nl/?url="
    "www.kards.com%2Fimages%2Fcard%2Fv53%2Fzh-Hans%2F{card_id}.avif"
    "&output=webp&w=400"
)

WIDTH = 1200
MARGIN = 44
CONTENT_W = WIDTH - MARGIN * 2
COLS = 6
GAP_X = 12
TILE_W = (CONTENT_W - GAP_X * (COLS - 1)) // COLS
IMG_H = int(TILE_W * 702 / 500)
LABEL_H = 32
TILE_H = IMG_H + LABEL_H
GAP_Y = 18

# The cards-only montage uses a fixed 8 x 5 grid (40 slots).
CARD_COLS = 8
CARD_TILE_W = WIDTH // CARD_COLS
CARD_TILE_H = round(CARD_TILE_W * 702 / 500)
CARD_SLOTS = 40

BG_TOP = (13, 17, 23)
BG_BOTTOM = (22, 28, 36)
PANEL = (26, 33, 43)
PANEL_LIGHT = (34, 43, 55)
GOLD = (216, 180, 90)
TEXT = (240, 234, 220)
MUTED = (154, 164, 178)
AMBER = (245, 190, 80)
WHITE = (255, 255, 255)

FACTION_COLORS = {
    "Germany": (138, 143, 152),
    "Britain": (59, 130, 246),
    "Japan": (239, 68, 68),
    "Soviet": (185, 28, 28),
    "USA": (34, 197, 94),
    "France": (96, 165, 250),
    "Italy": (74, 222, 128),
    "Poland": (245, 158, 11),
    "Finland": (56, 189, 248),
    "Anzac": (249, 115, 22),
    "Neutral": (163, 163, 163),
}

TYPE_UNIT = "unit"
TYPE_ORDER = "order"
TYPE_COUNTER = "counter"


def _font(size: int, mono: bool = False) -> ImageFont.FreeTypeFont:
    path = MONO_FONT_PATH if mono else FONT_PATH
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _faction_color(faction: str) -> tuple[int, int, int]:
    return FACTION_COLORS.get(faction, (163, 163, 163))


def _category(card: deck_code.DeckCard) -> str:
    if card.card_type == "order":
        return TYPE_ORDER
    if card.card_type == "countermeasure":
        return TYPE_COUNTER
    return TYPE_UNIT


def _truncate(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "…"
    while text and draw.textlength(text + ellipsis, font=font) > max_width:
        text = text[:-1]
    return text + ellipsis


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def _rounded_image(
    image: Image.Image, size: tuple[int, int], radius: int
) -> Image.Image:
    image = image.convert("RGB").resize(size, Image.LANCZOS)
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size[0] - 1, size[1] - 1), radius, fill=255
    )
    result = Image.new("RGB", size, (0, 0, 0))
    result.paste(image, (0, 0), mask)
    return result


def _background(height: int) -> Image.Image:
    image = Image.new("RGB", (WIDTH, height), BG_TOP)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(
            int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * ratio) for i in range(3)
        )
        draw.line((0, y, WIDTH, y), fill=color)
    return image


def _load_cached_image(path: Path) -> Image.Image | None:
    try:
        with Image.open(path) as image:
            return image.convert("RGB")
    except (OSError, ValueError):
        return None


def _local_thumb(card_id: str) -> Image.Image | None:
    """Load a bundled thumbnail, falling back to the base card for ``*_vet``."""
    if not card_id:
        return None
    names = [card_id.lower()]
    if names[0].endswith("_vet"):
        names.append(names[0][:-4])
    for name in names:
        path = LOCAL_THUMB_DIR / f"{name}.jpg"
        if path.exists():
            cached = _load_cached_image(path)
            if cached is not None:
                return cached
    return None


async def _download_card_image(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    card: deck_code.DeckCard,
) -> Image.Image | None:
    if not card.card_id:
        return None
    CARD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CARD_CACHE_DIR / f"{card.card_id.lower()}.webp"
    cached = _load_cached_image(cache_path)
    if cached is not None:
        return cached

    card_id = quote(card.card_id, safe="")
    urls = [
        CARD_IMAGE_URL.format(card_id=card_id),
        CARD_IMAGE_FALLBACK.format(card_id=card_id),
    ]
    timeout = aiohttp.ClientTimeout(total=15, connect=8, sock_read=12)
    async with semaphore:
        for url in urls:
            try:
                async with session.get(url, timeout=timeout) as response:
                    if response.status != 200:
                        continue
                    data = await response.read()
                    if not data:
                        continue
                    with Image.open(io.BytesIO(data)) as image:
                        image.load()
                        result = image.convert("RGB")
                    tmp = cache_path.with_suffix(".tmp")
                    tmp.write_bytes(data)
                    tmp.replace(cache_path)
                    return result
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
                continue
    return None


async def _load_card_images(
    cards: list[deck_code.DeckCard],
) -> dict[str, Image.Image | None]:
    unique: dict[str, deck_code.DeckCard] = {}
    for card in cards:
        unique.setdefault(card.import_id, card)

    images: dict[str, Image.Image | None] = {}
    pending: list[deck_code.DeckCard] = []
    for card in unique.values():
        local = _local_thumb(card.card_id)
        if local is not None:
            images[card.import_id] = local
        else:
            pending.append(card)

    if not pending:
        return images

    connector = aiohttp.TCPConnector(family=socket.AF_INET, ssl=True)
    semaphore = asyncio.Semaphore(2)
    async with aiohttp.ClientSession(connector=connector) as session:
        results = await asyncio.gather(
            *[_download_card_image(session, semaphore, card) for card in pending],
            return_exceptions=True,
        )
    for card, result in zip(pending, results):
        images[card.import_id] = result if isinstance(result, Image.Image) else None
    return images


def _draw_card_tile(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    card: deck_code.DeckCard,
    image: Image.Image | None,
    x: int,
    y: int,
) -> None:
    shadow = (x + 2, y + 3, x + TILE_W + 2, y + IMG_H + 3)
    draw.rounded_rectangle(shadow, 10, fill=(5, 7, 10))

    if image is not None:
        tile = _rounded_image(image, (TILE_W, IMG_H), 10)
        canvas.paste(tile, (x, y))
    else:
        color = _faction_color(card.faction)
        draw.rounded_rectangle((x, y, x + TILE_W, y + IMG_H), 10, fill=PANEL)
        draw.rounded_rectangle(
            (x + 1, y + 1, x + TILE_W - 1, y + IMG_H - 1),
            9,
            outline=color,
            width=2,
        )
        draw.text(
            (x + TILE_W // 2, y + 28),
            "KARDS",
            font=_font(14),
            fill=color,
            anchor="ma",
        )
        draw.text(
            (x + TILE_W // 2, y + IMG_H // 2 - 10),
            _truncate(draw, card.display_name, _font(18), TILE_W - 20),
            font=_font(18),
            fill=TEXT,
            anchor="ma",
        )
        draw.text(
            (x + TILE_W // 2, y + IMG_H // 2 + 24),
            f"{card.kredits if card.kredits is not None else '?'}K",
            font=_font(20),
            fill=GOLD,
            anchor="ma",
        )

    if card.count > 1:
        badge_text = f"×{card.count}"
        font = _font(20)
        text_w = int(draw.textlength(badge_text, font=font))
        badge_w = max(44, text_w + 20)
        badge_h = 30
        bx = x + TILE_W - badge_w - 6
        by = y + 6
        draw.rounded_rectangle(
            (bx, by, bx + badge_w, by + badge_h),
            8,
            fill=(9, 13, 18),
            outline=GOLD,
            width=2,
        )
        draw.text(
            (bx + badge_w / 2, by + badge_h / 2 - 1),
            badge_text,
            font=font,
            fill=GOLD,
            anchor="mm",
        )

    cost_text = f"{card.kredits if card.kredits is not None else '?'}K"
    pill_font = _font(15)
    pill_w = int(draw.textlength(cost_text, font=pill_font)) + 14
    pill_x = x
    pill_y = y + IMG_H + 5
    draw.rounded_rectangle(
        (pill_x, pill_y, pill_x + pill_w, pill_y + 23),
        6,
        fill=_faction_color(card.faction),
    )
    draw.text(
        (pill_x + pill_w / 2, pill_y + 11),
        cost_text,
        font=pill_font,
        fill=(10, 14, 18),
        anchor="mm",
    )
    name_font = _font(15)
    name_x = pill_x + pill_w + 7
    name = _truncate(draw, card.display_name, name_font, x + TILE_W - name_x)
    draw.text((name_x, pill_y + 3), name, font=name_font, fill=TEXT)


def _draw_header(draw: ImageDraw.ImageDraw, deck: deck_code.DecodedDeck) -> int:
    main_cn = deck_code.FACTION_CN.get(deck.main_faction, deck.main_faction)
    ally_cn = (
        deck_code.FACTION_CN.get(deck.ally_faction, deck.ally_faction)
        if deck.ally_faction
        else "无"
    )
    draw.text((MARGIN, 34), "KARDS 卡组", font=_font(52), fill=GOLD)
    draw.text(
        (MARGIN + 3, 108),
        f"主国  {main_cn}     盟国  {ally_cn}",
        font=_font(25),
        fill=TEXT,
    )
    draw.text(
        (MARGIN + 3, 148),
        f"卡表 {deck.total} 张  ·  含 HQ 共 {deck.total + deck_code.HQ_COUNT} 张",
        font=_font(20),
        fill=MUTED,
    )

    code_font = _font(15, mono=True)
    code = _truncate(draw, deck.raw, code_font, CONTENT_W - 28)
    draw.rounded_rectangle(
        (MARGIN, 182, MARGIN + CONTENT_W, 218),
        8,
        fill=(9, 13, 18),
        outline=(52, 63, 78),
        width=1,
    )
    draw.text((MARGIN + 14, 192), code, font=code_font, fill=(196, 205, 218))

    draw.text(
        (WIDTH - MARGIN, 46),
        "bilikards",
        font=_font(18),
        fill=(90, 100, 114),
        anchor="ra",
    )
    draw.line((MARGIN, 232, WIDTH - MARGIN, 232), fill=(48, 58, 72), width=1)
    return 248


def _draw_section(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    faction: str,
    cards: list[deck_code.DeckCard],
    images: dict[str, Image.Image | None],
    y: int,
) -> int:
    color = _faction_color(faction)
    faction_cn = deck_code.FACTION_CN.get(faction, faction or "未知阵营")
    count = sum(card.count for card in cards)

    draw.rounded_rectangle((MARGIN, y + 4, MARGIN + 7, y + 38), 3, fill=color)
    draw.text((MARGIN + 20, y), faction_cn, font=_font(29), fill=TEXT)
    count_text = f"{count} 张"
    draw.text(
        (MARGIN + 26 + draw.textlength(faction_cn, font=_font(29)), y + 8),
        count_text,
        font=_font(19),
        fill=MUTED,
    )
    y += 52

    for index, card in enumerate(cards):
        row = index // COLS
        col = index % COLS
        x = MARGIN + col * (TILE_W + GAP_X)
        tile_y = y + row * (TILE_H + GAP_Y)
        _draw_card_tile(canvas, draw, card, images.get(card.import_id), x, tile_y)

    rows = math.ceil(len(cards) / COLS)
    return y + rows * TILE_H + max(0, rows - 1) * GAP_Y + 26


def _draw_curve(
    draw: ImageDraw.ImageDraw,
    cards: list[deck_code.DeckCard],
    y: int,
) -> int:
    buckets: list[dict[str, int]] = [
        {TYPE_UNIT: 0, TYPE_ORDER: 0, TYPE_COUNTER: 0} for _ in range(8)
    ]
    for card in cards:
        cost = card.kredits if card.kredits is not None else 0
        bucket = buckets[min(max(cost, 0), 7)]
        bucket[_category(card)] += card.count

    draw.text((MARGIN, y), "费用曲线", font=_font(26), fill=TEXT)
    legend_x = WIDTH - MARGIN - 330
    for label, color in (
        ("单位", (232, 226, 212)),
        ("指令", GOLD),
        ("反制", (107, 114, 128)),
    ):
        draw.rounded_rectangle((legend_x, y + 9, legend_x + 16, y + 21), 3, fill=color)
        draw.text((legend_x + 23, y + 5), label, font=_font(16), fill=MUTED)
        legend_x += 90

    chart_top = y + 48
    chart_h = 138
    bar_w = 78
    gap = (CONTENT_W - bar_w * 8) // 7
    max_count = max(
        1,
        max(sum(bucket.values()) for bucket in buckets),
    )

    for cost, bucket in enumerate(buckets):
        total = sum(bucket.values())
        x = MARGIN + cost * (bar_w + gap)
        draw.rounded_rectangle(
            (x, chart_top, x + bar_w, chart_top + chart_h),
            6,
            fill=(22, 28, 36),
        )
        current_bottom = chart_top + chart_h
        for category, color in (
            (TYPE_UNIT, (232, 226, 212)),
            (TYPE_ORDER, GOLD),
            (TYPE_COUNTER, (107, 114, 128)),
        ):
            value = bucket[category]
            if not value:
                continue
            height = max(3, int(chart_h * value / max_count))
            draw.rectangle(
                (x + 5, current_bottom - height, x + bar_w - 5, current_bottom),
                fill=color,
            )
            current_bottom -= height
        label = "7K+" if cost == 7 else f"{cost}K"
        draw.text(
            (x + bar_w / 2, chart_top + chart_h + 8),
            label,
            font=_font(16),
            fill=MUTED,
            anchor="ma",
        )
        if total:
            draw.text(
                (
                    x + bar_w / 2,
                    chart_top + chart_h - int(chart_h * total / max_count) - 22,
                ),
                str(total),
                font=_font(15),
                fill=TEXT,
                anchor="ma",
            )
    return chart_top + chart_h + 40


def _draw_stats(
    draw: ImageDraw.ImageDraw,
    deck: deck_code.DecodedDeck,
    y: int,
) -> int:
    units = orders = counters = 0
    rarity = {"Standard": 0, "Limited": 0, "Special": 0, "Elite": 0}
    cost_sum = 0
    for card in deck.cards:
        category = _category(card)
        if category == TYPE_UNIT:
            units += card.count
        elif category == TYPE_ORDER:
            orders += card.count
        else:
            counters += card.count
        if card.rarity in rarity:
            rarity[card.rarity] += card.count
        if card.kredits is not None:
            cost_sum += card.kredits * card.count

    average = cost_sum / deck.total if deck.total else 0
    items = [
        (f"单位 {units}", (232, 226, 212)),
        (f"指令 {orders}", GOLD),
        (f"反制 {counters}", (154, 164, 178)),
        (f"平均费用 {average:.2f}", TEXT),
        (f"标准 {rarity['Standard']}", MUTED),
        (f"限定 {rarity['Limited']}", (205, 127, 50)),
        (f"特殊 {rarity['Special']}", (192, 192, 192)),
        (f"精英 {rarity['Elite']}", GOLD),
    ]
    x = MARGIN
    for text, color in items:
        font = _font(17)
        draw.text((x, y), text, font=font, fill=color)
        x += int(draw.textlength(text, font=font)) + 34
    return y + 38


def _warning_lines(warnings: list[str]) -> list[str]:
    scratch = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(scratch)
    font = _font(17)
    lines: list[str] = []
    for warning in warnings:
        lines.extend(_wrap(draw, "· " + warning, font, CONTENT_W - 36))
    return lines


def _draw_warnings(
    draw: ImageDraw.ImageDraw,
    warnings: list[str],
    y: int,
) -> int:
    font = _font(17)
    lines = _warning_lines(warnings)
    height = 40 + len(lines) * 25
    draw.rounded_rectangle(
        (MARGIN, y, MARGIN + CONTENT_W, y + height),
        8,
        fill=(43, 34, 16),
        outline=(120, 92, 35),
        width=1,
    )
    draw.text((MARGIN + 16, y + 8), "提示", font=_font(17), fill=AMBER)
    for index, line in enumerate(lines):
        draw.text(
            (MARGIN + 16, y + 34 + index * 25),
            line,
            font=font,
            fill=(232, 214, 172),
        )
    return y + height + 18


def _draw_card_only_tile(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    card: deck_code.DeckCard,
    image: Image.Image | None,
    x: int,
    y: int,
) -> None:
    """Draw one card as a bare tile, without the cost/name footer."""
    if image is not None:
        tile = image.convert("RGB").resize((CARD_TILE_W, CARD_TILE_H), Image.LANCZOS)
        canvas.paste(tile, (x, y))
    else:
        color = _faction_color(card.faction)
        draw.rectangle((x, y, x + CARD_TILE_W - 1, y + CARD_TILE_H - 1), fill=PANEL)
        draw.rectangle(
            (x + 2, y + 2, x + CARD_TILE_W - 3, y + CARD_TILE_H - 3),
            outline=color,
            width=2,
        )
        draw.text(
            (x + CARD_TILE_W // 2, y + 16),
            "KARDS",
            font=_font(12),
            fill=color,
            anchor="ma",
        )
        draw.text(
            (x + CARD_TILE_W // 2, y + CARD_TILE_H // 2 - 14),
            _truncate(draw, card.display_name, _font(15), CARD_TILE_W - 16),
            font=_font(15),
            fill=TEXT,
            anchor="ma",
        )
        draw.text(
            (x + CARD_TILE_W // 2, y + CARD_TILE_H // 2 + 14),
            f"{card.kredits if card.kredits is not None else '?'}K",
            font=_font(18),
            fill=GOLD,
            anchor="ma",
        )

    if card.count > 1:
        badge_text = f"×{card.count}"
        font = _font(18)
        text_w = int(draw.textlength(badge_text, font=font))
        badge_w = max(38, text_w + 16)
        badge_h = 26
        bx = x + CARD_TILE_W - badge_w - 5
        by = y + 5
        draw.rounded_rectangle(
            (bx, by, bx + badge_w, by + badge_h),
            7,
            fill=(9, 13, 18),
            outline=GOLD,
            width=2,
        )
        draw.text(
            (bx + badge_w / 2, by + badge_h / 2 - 1),
            badge_text,
            font=font,
            fill=GOLD,
            anchor="mm",
        )


def _draw_hq_tile(
    draw: ImageDraw.ImageDraw,
    deck: deck_code.DecodedDeck,
    x: int,
    y: int,
) -> None:
    """Draw the HQ slot, which is not stored in the import code."""
    color = _faction_color(deck.main_faction)
    faction_cn = deck_code.FACTION_CN.get(deck.main_faction, deck.main_faction)
    draw.rectangle((x, y, x + CARD_TILE_W - 1, y + CARD_TILE_H - 1), fill=PANEL)
    draw.rectangle(
        (x + 3, y + 3, x + CARD_TILE_W - 4, y + CARD_TILE_H - 4),
        outline=color,
        width=3,
    )
    draw.text(
        (x + CARD_TILE_W // 2, y + CARD_TILE_H // 2 - 34),
        "HQ",
        font=_font(38),
        fill=GOLD,
        anchor="ma",
    )
    draw.text(
        (x + CARD_TILE_W // 2, y + CARD_TILE_H // 2 + 18),
        faction_cn,
        font=_font(17),
        fill=TEXT,
        anchor="ma",
    )
    draw.text(
        (x + CARD_TILE_W // 2, y + CARD_TILE_H // 2 + 46),
        "总部 · 码外",
        font=_font(12),
        fill=MUTED,
        anchor="ma",
    )


def _draw_empty_tile(draw: ImageDraw.ImageDraw, x: int, y: int) -> None:
    """Draw an empty deck slot for partial or manually built decks."""
    draw.rectangle((x, y, x + CARD_TILE_W - 1, y + CARD_TILE_H - 1), fill=(17, 22, 29))
    dash = (54, 64, 78)
    step = 12
    for offset in range(4, CARD_TILE_W - 4, step):
        draw.line(
            (x + offset, y + 4, min(x + offset + 6, x + CARD_TILE_W - 4), y + 4),
            fill=dash,
            width=1,
        )
        draw.line(
            (
                x + offset,
                y + CARD_TILE_H - 5,
                min(x + offset + 6, x + CARD_TILE_W - 4),
                y + CARD_TILE_H - 5,
            ),
            fill=dash,
            width=1,
        )
    for offset in range(4, CARD_TILE_H - 4, step):
        draw.line(
            (x + 4, y + offset, x + 4, min(y + offset + 6, y + CARD_TILE_H - 4)),
            fill=dash,
            width=1,
        )
        draw.line(
            (
                x + CARD_TILE_W - 5,
                y + offset,
                x + CARD_TILE_W - 5,
                min(y + offset + 6, y + CARD_TILE_H - 4),
            ),
            fill=dash,
            width=1,
        )
    draw.text(
        (x + CARD_TILE_W // 2, y + CARD_TILE_H // 2),
        "+",
        font=_font(34),
        fill=(62, 74, 90),
        anchor="mm",
    )


def _render_cards(
    deck: deck_code.DecodedDeck,
    images: dict[str, Image.Image | None],
    output_path: Path,
) -> Path:
    """Render the 40-card montage: cards only, tiled edge to edge."""
    expanded: list[deck_code.DeckCard] = []
    for card in deck.cards:
        expanded.extend([card] * max(1, card.count))

    # Keep the HQ as the final slot.  A standard code has 39 cards, so the
    # grid is exactly 8 x 5; partial manual decks keep empty slots visible.
    if len(expanded) >= CARD_SLOTS - 1:
        slots = expanded
        hq_index = len(expanded)
    else:
        slots = expanded
        hq_index = CARD_SLOTS - 1
    total_tiles = max(CARD_SLOTS, hq_index + 1)
    rows = math.ceil(total_tiles / CARD_COLS)
    height = rows * CARD_TILE_H

    canvas = Image.new("RGB", (WIDTH, height), BG_TOP)
    draw = ImageDraw.Draw(canvas)
    for index in range(rows * CARD_COLS):
        x = (index % CARD_COLS) * CARD_TILE_W
        y = (index // CARD_COLS) * CARD_TILE_H
        if index < len(slots):
            card = slots[index]
            _draw_card_only_tile(canvas, draw, card, images.get(card.import_id), x, y)
        elif index == hq_index:
            _draw_hq_tile(draw, deck, x, y)
        else:
            _draw_empty_tile(draw, x, y)

    IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, "JPEG", quality=93, optimize=True)
    return output_path


def _draw_decklist(
    draw: ImageDraw.ImageDraw,
    deck: deck_code.DecodedDeck,
    y: int,
) -> int:
    """Draw the text decklist in two columns per faction."""
    grouped: dict[str, list[deck_code.DeckCard]] = {}
    for card in deck.cards:
        grouped.setdefault(card.faction, []).append(card)

    faction_order: list[str] = [deck.main_faction]
    if deck.ally_faction:
        faction_order.append(deck.ally_faction)
    for faction in grouped:
        if faction not in faction_order:
            faction_order.append(faction)

    draw.text((MARGIN, y), "卡表", font=_font(26), fill=TEXT)
    y += 42
    col_w = (CONTENT_W - 30) // 2
    for faction in faction_order:
        cards = grouped.get(faction) or []
        if not cards:
            continue
        color = _faction_color(faction)
        faction_cn = deck_code.FACTION_CN.get(faction, faction or "未知阵营")
        count = sum(card.count for card in cards)
        draw.rectangle((MARGIN, y + 4, MARGIN + 7, y + 32), fill=color)
        draw.text((MARGIN + 20, y), faction_cn, font=_font(23), fill=TEXT)
        draw.text(
            (
                MARGIN + 32 + int(draw.textlength(faction_cn, font=_font(23))),
                y + 6,
            ),
            f"{count} 张",
            font=_font(17),
            fill=MUTED,
        )
        y += 36

        rows = math.ceil(len(cards) / 2)
        for index, card in enumerate(cards):
            col = index // rows
            row = index % rows
            x = MARGIN + col * (col_w + 30)
            line_y = y + row * 27
            cost = f"{card.kredits}K" if card.kredits is not None else "?K"
            text = _truncate(
                draw,
                f"{card.count}x ({cost}) {card.display_name}",
                _font(16),
                col_w,
            )
            draw.text((x, line_y), text, font=_font(16), fill=TEXT)
        y += rows * 27 + 16
    return y


def _render_info(
    deck: deck_code.DecodedDeck,
    output_path: Path,
) -> Path:
    """Render all non-card information as a separate poster."""
    canvas = _background(3200)
    draw = ImageDraw.Draw(canvas)

    y = _draw_header(draw, deck)
    y = _draw_decklist(draw, deck, y)

    draw.line((MARGIN, y, WIDTH - MARGIN, y), fill=(48, 58, 72), width=1)
    y += 24
    y = _draw_curve(draw, deck.cards, y)
    y = _draw_stats(draw, deck, y)
    if deck.warnings:
        y = _draw_warnings(draw, deck.warnings, y)

    y += 16
    draw.text(
        (WIDTH - MARGIN, y),
        "KARDS 卡组代码解析 · bilikards",
        font=_font(15),
        fill=(78, 88, 102),
        anchor="ra",
    )

    canvas = canvas.crop((0, 0, WIDTH, y + 40))
    IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, "JPEG", quality=93, optimize=True)
    return output_path


def _cleanup(directory: Path, keep: int) -> None:
    try:
        files = sorted(
            (path for path in directory.iterdir() if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for path in files[keep:]:
        try:
            path.unlink()
        except OSError:
            pass


async def render_deck_images(
    deck: deck_code.DecodedDeck,
) -> tuple[Path, Path]:
    """Render a decoded deck as a cards montage plus an info poster.

    Args:
        deck: Decoded deck returned by :func:`deck_code.decode_deck_code`.

    Returns:
        A tuple ``(cards_path, info_path)``.  The first image tiles the 40
        deck slots edge to edge; the second contains faction, decklist, curve,
        stats, warnings, and the deck code.
    """
    digest = hashlib.sha1(deck.raw.encode("utf-8")).hexdigest()[:16]
    cards_path = IMAGE_CACHE_DIR / f"deck_{digest}_cards.jpg"
    info_path = IMAGE_CACHE_DIR / f"deck_{digest}_info.jpg"
    if (
        cards_path.exists()
        and cards_path.stat().st_size > 0
        and info_path.exists()
        and info_path.stat().st_size > 0
    ):
        return cards_path, info_path

    images = await _load_card_images(deck.cards)
    await asyncio.to_thread(_render_cards, deck, images, cards_path)
    await asyncio.to_thread(_render_info, deck, info_path)
    _cleanup(IMAGE_CACHE_DIR, 60)
    _cleanup(CARD_CACHE_DIR, 1200)
    return cards_path, info_path


async def render_deck_image(deck: deck_code.DecodedDeck) -> Path:
    """Render a decoded deck and return the cards montage path.

    Kept for callers that only need one image.

    Args:
        deck: Decoded deck returned by :func:`deck_code.decode_deck_code`.

    Returns:
        Path to the cards montage.
    """
    cards_path, _info_path = await render_deck_images(deck)
    return cards_path


def render_deck_image_sync(deck: deck_code.DecodedDeck) -> Path:
    """Synchronous wrapper used by tests and simple callers."""
    return asyncio.run(render_deck_image(deck))


SEARCH_COLS = 5
SEARCH_GAP = 14
SEARCH_MARGIN = 36
SEARCH_TILE_W = (
    WIDTH - SEARCH_MARGIN * 2 - SEARCH_GAP * (SEARCH_COLS - 1)
) // SEARCH_COLS
SEARCH_IMG_H = round(SEARCH_TILE_W * 702 / 500)
SEARCH_LABEL_H = 48
SEARCH_HEADER_H = 78


def _draw_search_tile(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    item: dict,
    image: Image.Image | None,
    x: int,
    y: int,
) -> None:
    """Draw one search result: card art plus a compact name/cost footer."""
    name = str(item.get("name") or item.get("card_id") or "").strip()
    card_id = str(item.get("card_id") or "").strip()
    cost = item.get("cost")
    faction = str(item.get("faction") or "")
    color = _faction_color(faction)

    if image is not None:
        tile = _rounded_image(image, (SEARCH_TILE_W, SEARCH_IMG_H), 10)
        canvas.paste(tile, (x, y))
    else:
        draw.rounded_rectangle(
            (x, y, x + SEARCH_TILE_W - 1, y + SEARCH_IMG_H - 1),
            10,
            fill=PANEL,
            outline=color,
            width=2,
        )
        draw.text(
            (x + SEARCH_TILE_W // 2, y + 18),
            "KARDS",
            font=_font(12),
            fill=color,
            anchor="ma",
        )
        draw.text(
            (x + SEARCH_TILE_W // 2, y + SEARCH_IMG_H // 2 - 10),
            _truncate(draw, name, _font(16), SEARCH_TILE_W - 20),
            font=_font(16),
            fill=TEXT,
            anchor="ma",
        )
        if card_id:
            draw.text(
                (x + SEARCH_TILE_W // 2, y + SEARCH_IMG_H // 2 + 14),
                _truncate(draw, card_id, _font(10), SEARCH_TILE_W - 20),
                font=_font(10),
                fill=MUTED,
                anchor="ma",
            )

    label_y = y + SEARCH_IMG_H + 5
    cost_text = f"{cost}K" if cost is not None else "?K"
    draw.text((x + 2, label_y), cost_text, font=_font(13), fill=GOLD)
    draw.text(
        (x + SEARCH_TILE_W - 2, label_y),
        deck_code.FACTION_CN.get(faction, faction)[:4],
        font=_font(11),
        fill=color,
        anchor="ra",
    )
    draw.text(
        (x + 2, label_y + 20),
        _truncate(draw, name, _font(13), SEARCH_TILE_W - 4),
        font=_font(13),
        fill=TEXT,
    )


def render_search_grid(
    items: list[dict], query: str = "", limit: int = 10, page: int = 1, total: int = 0
) -> Path:
    """Render search results as one card-art montage.

    Args:
        items: Card dictionaries with ``card_id``, ``name``, ``cost`` and
            ``faction`` keys.
        query: Search text shown in the header.
        limit: Maximum number of cards to draw on this image.
        page: Current page number, shown in the header.
        total: Total number of matches, shown in the header.

    Returns:
        Path to the generated JPEG.
    """
    cards = [item for item in items if isinstance(item, dict)][:limit]
    if not cards:
        raise ValueError("没有可渲染的卡牌")

    rows = math.ceil(len(cards) / SEARCH_COLS)
    grid_h = rows * (SEARCH_IMG_H + SEARCH_LABEL_H) + (rows - 1) * SEARCH_GAP
    height = SEARCH_HEADER_H + grid_h + SEARCH_MARGIN
    canvas = _background(height)
    draw = ImageDraw.Draw(canvas)

    title = "卡牌搜索"
    if query:
        title += f" · {query}"
    draw.text(
        (SEARCH_MARGIN, 22),
        _truncate(draw, title, _font(26), CONTENT_W),
        font=_font(26),
        fill=TEXT,
    )
    count_text = f"本页 {len(cards)} 张"
    if total:
        count_text += f" · 共 {total} 张"
    if page > 1:
        count_text += f" · 第 {page} 页"
    draw.text(
        (SEARCH_MARGIN, 54),
        count_text + " · KARDS",
        font=_font(13),
        fill=MUTED,
    )

    for index, item in enumerate(cards):
        row, col = divmod(index, SEARCH_COLS)
        x = SEARCH_MARGIN + col * (SEARCH_TILE_W + SEARCH_GAP)
        y = SEARCH_HEADER_H + row * (SEARCH_IMG_H + SEARCH_LABEL_H + SEARCH_GAP)
        image = _local_thumb(str(item.get("card_id") or ""))
        _draw_search_tile(canvas, draw, item, image, x, y)

    digest = hashlib.sha1(
        (
            f"{query}|{page}|{limit}|" + "|".join(str(c.get("card_id")) for c in cards)
        ).encode("utf-8")
    ).hexdigest()[:16]
    IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    output_path = IMAGE_CACHE_DIR / f"search_{digest}.jpg"
    canvas.save(output_path, "JPEG", quality=93, optimize=True)
    _cleanup(IMAGE_CACHE_DIR, 60)
    return output_path
