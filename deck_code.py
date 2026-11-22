"""KARDS deck-code decoding and chat-friendly formatting.

The official deck code format is::

    %%<main><ally>|<1-copy ids>;<2-copy ids>;<3-copy ids>;<4-copy ids>

Each card is referenced by its two-character base62 ``import_id``.  The
decoder here is intentionally lenient about surrounding text (share links,
URL-encoded codes, game-save codes with ``~`` extensions) but strict about
the deck-code grammar itself.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlparse

try:
    from . import data_paths
except ImportError:  # pragma: no cover - standalone tools/tests
    import data_paths

_CARD_DATA_PATH = data_paths.CARDS_FILE

FACTION_CODE = {
    "Germany": "1",
    "Britain": "2",
    "Japan": "3",
    "Soviet": "4",
    "USA": "5",
    "France": "6",
    "Italy": "7",
    "Poland": "8",
    "Finland": "9",
    "Anzac": "a",
    "Neutral": "0",
}
CODE_FACTION = {v: k for k, v in FACTION_CODE.items()}

FACTION_CN = {
    "Germany": "德国",
    "Britain": "英国",
    "Japan": "日本",
    "Soviet": "苏联",
    "USA": "美国",
    "France": "法国",
    "Italy": "意大利",
    "Poland": "波兰",
    "Finland": "芬兰",
    "Anzac": "澳新军团",
    "Neutral": "中立",
}

TYPE_CN = {
    "infantry": "步兵",
    "tank": "坦克",
    "artillery": "火炮",
    "fighter": "战斗机",
    "bomber": "轰炸机",
    "order": "指令",
    "countermeasure": "反制",
}

RARITY_CN = {
    "Standard": "普通",
    "Limited": "限定",
    "Special": "特殊",
    "Elite": "精英",
    "Token": "衍生牌",
}

# Older card tables and user input still use these labels.  Keep them working
# so searches and imports do not break after the display name change.
RARITY_ALIASES = {
    "标准": "Standard",
    "普通": "Standard",
    "普通卡": "Standard",
    "衍生卡": "Token",
    "衍生": "Token",
    "Derivative": "Token",
}

# Ability keys come from Fards ``keywordsNormalized``.  Heavy armor and intel
# carry a numeric suffix (``heavyArmor:1``), so they are translated by prefix.
KEYWORD_CN = {
    "alpine": "山地",
    "ambush": "伏击",
    "blitz": "闪击",
    "bond": "协力",
    "covert": "隐蔽",
    "fury": "奋战",
    "guard": "守护",
    "mobilize": "动员",
    "pincer": "钳击",
    "salvage": "收缴",
    "shock": "冲击",
    "smokescreen": "烟幕",
    "heavyArmor": "重甲",
    "intel": "情报",
}

# Official Chinese keyword names take precedence.  Keep the old/wrong labels
# accepted so previously imported cards and vision output still normalize.
KEYWORD_ALIASES = {
    "奋战": "fury",
    "狂怒": "fury",
    "狂热": "fury",
    "收缴": "salvage",
    "回收": "salvage",
}


def keyword_label(key: str) -> str:
    """Translate one ability key to its Chinese label.

    Args:
        key: Normalized ability key such as ``blitz`` or ``heavyArmor:2``.

    Returns:
        Chinese label, or the original key when no translation exists.
    """
    text = str(key or "").strip()
    if not text:
        return ""
    if text in KEYWORD_CN:
        return KEYWORD_CN[text]
    alias = KEYWORD_ALIASES.get(text)
    if alias:
        return KEYWORD_CN[alias]
    if ":" in text:
        name, _, level = text.partition(":")
        base = KEYWORD_CN.get(name)
        if base:
            return f"{base} {level}"
    # Tolerate the un-normalized ``heavyArmor1`` / ``intel2`` forms.
    match = re.fullmatch(r"(heavyArmor|intel)([123])", text)
    if match:
        return f"{KEYWORD_CN[match.group(1)]} {match.group(2)}"
    return text


MAX_COPIES = {
    "Standard": 4,
    "Limited": 3,
    "Special": 2,
    "Elite": 1,
}

DECK_SIZE = 40  # including the HQ, which is not stored in the code
HQ_COUNT = 1
MAIN_MIN = 28
ALLY_MAX = 12


class DeckCodeError(ValueError):
    """Raised when a string cannot be parsed as a KARDS deck code."""


@dataclass
class DeckCard:
    """One card entry in a decoded deck."""

    import_id: str
    card_id: str
    name_zh: str
    name_en: str
    faction: str
    card_type: str
    rarity: str
    kredits: int | None
    operation_cost: int | None
    attack: int | None
    defense: int | None
    count: int

    @property
    def display_name(self) -> str:
        return self.name_zh or self.name_en or f"未知卡牌({self.import_id})"


@dataclass
class DecodedDeck:
    """A decoded KARDS deck."""

    raw: str
    main_faction: str
    ally_faction: str | None
    cards: list[DeckCard] = field(default_factory=list)
    total: int = 0
    warnings: list[str] = field(default_factory=list)


def _load_cards() -> dict[str, list]:
    try:
        with _CARD_DATA_PATH.open(encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    cards = data.get("cards") if isinstance(data, dict) else None
    return cards if isinstance(cards, dict) else {}


CARDS: dict[str, list] = _load_cards()


def _strip_extended(code: str) -> str:
    """Reduce a game-save deck code to the standard import-code body."""
    for marker in ("~", "～"):
        if marker in code:
            code = code.split(marker, 1)[0]
    parts = code.split("|")
    if len(parts) > 2:
        code = "|".join(parts[:2])
    if "|" in code:
        head, body = code.split("|", 1)
        semis = body.count(";")
        if semis < 3:
            body += ";" * (3 - semis)
        code = f"{head}|{body}"
    return code


def normalize_deck_code(text: str) -> str:
    """Normalize a user-supplied deck code or share link.

    Args:
        text: Raw text copied from the game, a website, or a chat message.

    Returns:
        A canonical ``%%<main><ally>|...`` deck code.

    Raises:
        DeckCodeError: If the input does not look like a deck code.
    """
    code = str(text or "").strip()
    if not code:
        raise DeckCodeError("卡组码为空")

    if re.match(r"^https?://", code, re.I):
        parsed = urlparse(code)
        query = parse_qs(parsed.query)
        for key in ("deck_code", "deckCode", "code", "hash"):
            values = query.get(key)
            if values:
                code = values[0]
                break
        else:
            fragment = parsed.fragment
            if "deck_code=" in fragment:
                code = fragment.split("deck_code=", 1)[1].split("&", 1)[0]
            elif fragment:
                code = fragment

    if "deck_code=" in code:
        code = code.split("deck_code=", 1)[1].split("&", 1)[0]

    code = re.sub(r"^deck[:：]\s*", "", code, flags=re.I)

    # Only URL-decode when the text actually contains encoded structural
    # characters.  Decoding unconditionally would corrupt the ``%%`` magic
    # prefix (``%%53`` -> ``%S``) because ``%53`` is a valid escape.
    if re.search(r"%25|%7c|%3b|%7C|%3B", code, re.I):
        for _ in range(3):
            try:
                decoded = unquote(code)
            except ValueError:
                break
            if decoded == code or decoded.startswith("%%"):
                code = decoded
                break
            code = decoded

    code = re.sub(r"\s+", "", code)
    code = code.strip("`'\"“”‘’")
    if "|" not in code:
        for marker in ("～", "~"):
            pos = code.find(marker)
            if pos < 0:
                continue
            head = code[:pos]
            if head.startswith("%%"):
                head = head[2:]
            if re.fullmatch(r"[0-9A-Za-z]{1,2}", head):
                code = code[:pos] + "|" + code[pos + 1 :]
                break
    code = _strip_extended(code)

    if not code.startswith("%%"):
        if re.match(r"^[0-9A-Za-z]{1,2}\|", code):
            code = "%%" + code
        else:
            raise DeckCodeError("卡组码必须以 %% 开头，或形如 53|...（也接受 ～）")

    return code


def _split_import_ids(segment: str) -> list[str]:
    ids: list[str] = []
    index = 0
    while index < len(segment):
        if index + 2 <= len(segment):
            ids.append(segment[index : index + 2])
            index += 2
            continue
        one = segment[index:]
        if one in CARDS:
            ids.append(one)
            index += 1
            continue
        raise DeckCodeError(f"卡牌短码错位：剩余 “{one}” 无法解析（每个短码应为 2 位）")
    return ids


def decode_deck_code(text: str, allow_partial: bool = True) -> DecodedDeck:
    """Decode a KARDS deck code into card entries.

    Args:
        text: Raw deck code or share link.
        allow_partial: Keep decoding when the code does not contain 39 cards.

    Returns:
        The decoded deck, including validation warnings.

    Raises:
        DeckCodeError: If the code is malformed or uses unknown factions.
    """
    raw = normalize_deck_code(text)
    body = raw[2:]
    if "|" not in body:
        raise DeckCodeError("卡组码缺少 | 分隔符（也可写 ～）")

    header, card_part = body.split("|", 1)
    if not 1 <= len(header) <= 2:
        raise DeckCodeError(f"阵营头长度异常：{header!r}（应为 1~2 个字符）")

    main_code = header[0]
    ally_code = header[1] if len(header) == 2 else header[0]
    main_faction = CODE_FACTION.get(main_code)
    ally_faction = CODE_FACTION.get(ally_code)
    if not main_faction:
        raise DeckCodeError(f"未知主国编号：{main_code}")
    if not ally_faction:
        raise DeckCodeError(f"未知盟国编号：{ally_code}")
    if ally_faction == main_faction:
        ally_faction = None

    groups = card_part.split(";")
    if len(groups) > 4:
        raise DeckCodeError(f"分号有 {len(groups) - 1} 个，导入码最多 3 个")

    counts: dict[str, int] = {}
    for group_index, segment in enumerate(groups):
        if not segment:
            continue
        copies = group_index + 1
        for import_id in _split_import_ids(segment):
            counts[import_id] = counts.get(import_id, 0) + copies

    cards: list[DeckCard] = []
    unknown: list[str] = []
    for import_id, copies in counts.items():
        info = CARDS.get(import_id)
        if not info:
            unknown.append(import_id)
            cards.append(
                DeckCard(
                    import_id=import_id,
                    card_id="",
                    name_zh="",
                    name_en="",
                    faction="",
                    card_type="",
                    rarity="",
                    kredits=None,
                    operation_cost=None,
                    attack=None,
                    defense=None,
                    count=copies,
                )
            )
            continue
        cards.append(
            DeckCard(
                import_id=import_id,
                card_id=info[0],
                name_zh=info[1],
                name_en=info[2],
                faction=info[3],
                card_type=info[4],
                rarity=info[5],
                kredits=info[6],
                operation_cost=info[7],
                attack=info[8],
                defense=info[9],
                count=copies,
            )
        )

    total = sum(card.count for card in cards)
    warnings: list[str] = []
    if unknown:
        warnings.append("本地卡表未收录短码：" + "、".join(sorted(unknown)))
    if not allow_partial and total != DECK_SIZE - HQ_COUNT:
        raise DeckCodeError(
            f"码里是 {total} 张，标准导入码应为 {DECK_SIZE - HQ_COUNT} 张"
        )
    if total != DECK_SIZE - HQ_COUNT:
        warnings.append(
            f"码里 {total} 张，标准卡组应为 {DECK_SIZE - HQ_COUNT} 张（HQ 不进码）"
        )

    ally_cards = 0
    for card in cards:
        if ally_faction and card.faction == ally_faction:
            ally_cards += card.count
        cap = MAX_COPIES.get(card.rarity)
        if cap and card.count > cap:
            warnings.append(
                f"{card.display_name} 带 {card.count} 张，"
                f"{RARITY_CN.get(card.rarity, card.rarity)}上限 {cap} 张"
            )
    if ally_cards > ALLY_MAX:
        warnings.append(f"盟国 {ally_cards} 张，上限 {ALLY_MAX} 张")
    main_cards = total - ally_cards + HQ_COUNT
    if main_cards < MAIN_MIN:
        warnings.append(f"主国 {main_cards} 张（含 HQ），至少 {MAIN_MIN} 张")

    faction_order = {main_faction: 0}
    if ally_faction:
        faction_order[ally_faction] = 1
    faction_order.setdefault("Neutral", 2)
    cards.sort(
        key=lambda card: (
            faction_order.get(card.faction, 9),
            card.kredits if card.kredits is not None else 99,
            card.display_name,
        )
    )

    return DecodedDeck(
        raw=raw,
        main_faction=main_faction,
        ally_faction=ally_faction,
        cards=cards,
        total=total,
        warnings=warnings,
    )


def format_deck(deck: DecodedDeck) -> str:
    """Render a decoded deck as a compact chat message.

    Args:
        deck: Decoded deck returned by :func:`decode_deck_code`.

    Returns:
        A multi-line, Chinese-language deck summary.
    """
    main_cn = FACTION_CN.get(deck.main_faction, deck.main_faction)
    ally_cn = (
        FACTION_CN.get(deck.ally_faction, deck.ally_faction)
        if deck.ally_faction
        else "无"
    )
    lines = [
        "KARDS 卡组代码解析",
        f"主国：{main_cn}　盟国：{ally_cn}",
        f"合计：{deck.total} 张（含 HQ {deck.total + HQ_COUNT} 张）",
    ]

    grouped: dict[str, list[DeckCard]] = {}
    for card in deck.cards:
        grouped.setdefault(card.faction, []).append(card)

    faction_order = [deck.main_faction]
    if deck.ally_faction:
        faction_order.append(deck.ally_faction)
    for faction in grouped:
        if faction not in faction_order:
            faction_order.append(faction)

    for faction in faction_order:
        cards = grouped.get(faction)
        if not cards:
            continue
        faction_cn = FACTION_CN.get(faction, faction or "未知阵营")
        count = sum(card.count for card in cards)
        lines.append("")
        lines.append(f"【{faction_cn}】{count} 张")
        for card in cards:
            cost = f"{card.kredits}K" if card.kredits is not None else "?K"
            type_cn = TYPE_CN.get(card.card_type, card.card_type)
            suffix = f" · {type_cn}" if type_cn else ""
            lines.append(f"{card.count}x ({cost}) {card.display_name}{suffix}")

    curve: dict[int, int] = {}
    for card in deck.cards:
        if card.kredits is None:
            continue
        curve[card.kredits] = curve.get(card.kredits, 0) + card.count
    if curve:
        lines.append("")
        lines.append(
            "费用曲线："
            + "  ".join(f"{cost}K×{count}" for cost, count in sorted(curve.items()))
        )

    if deck.warnings:
        lines.append("")
        lines.append("提示：" + "；".join(deck.warnings))

    return "\n".join(lines)


def parse_and_format(text: str) -> str:
    """Decode and format a deck code in one call.

    Args:
        text: Raw deck code or share link.

    Returns:
        A chat-ready deck summary.

    Raises:
        DeckCodeError: If the code cannot be decoded.
    """
    return format_deck(decode_deck_code(text))
