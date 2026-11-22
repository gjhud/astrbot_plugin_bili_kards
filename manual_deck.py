"""Manual KARDS deck storage shared by chat commands and the WebUI page.

The official import code does not contain the HQ, so the manual deck only
stores the 39 main-deck cards plus an optional main/ally faction override.
A synthetic import code is generated on demand so the existing decoder and
image renderer can be reused unchanged.

The same module also owns the editable alias library and the official card
import used by the plugin page and the chat commands.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime

from . import data_paths, deck_code

PLUGIN_DIR = data_paths.PLUGIN_DIR
DECK_FILE = data_paths.DECK_FILE
DECK_DB = data_paths.DECK_DB
LEGACY_DECK_KEY = "legacy"
ALIAS_FILE = data_paths.ALIAS_FILE
CARDS_FILE = data_paths.CARDS_FILE
MAX_COPIES = 4
ALIAS_VERSION = 1


# Full-name searches ignore whitespace: ``MONSOONRAIN`` matches the card named
# ``MONSOON RAIN`` and ``第16步兵旅`` matches ``第 16 步兵旅``.
def compact_text(value: object) -> str:
    """Normalize text for whitespace-insensitive comparison."""
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"\s+", "", text)


# Field corrections shared by the chat command and the web page.
# canonical field -> (accepted tokens, label, card-row index, import key)
FIX_FIELDS = {
    "name_zh": (
        ("name_zh", "name", "中文名", "中文", "名称", "名字"),
        "中文名",
        1,
        "name_zh",
    ),
    "name_en": (("name_en", "english", "英文名", "英文"), "英文名", 2, "name_en"),
    "faction": (("faction", "nation", "阵营", "国家"), "阵营", 3, "faction"),
    "type": (("type", "card_type", "类型"), "类型", 4, "type"),
    "rarity": (("rarity", "稀有度"), "稀有度", 5, "rarity"),
    "cost": (("cost", "kredits", "费用"), "费用", 6, "cost"),
    "operation_cost": (
        ("operation_cost", "op", "行动", "行动费用"),
        "行动费用",
        7,
        "operation_cost",
    ),
    "attack": (("attack", "攻击", "攻"), "攻击", 8, "attack"),
    "defense": (("defense", "防御", "防"), "防御", 9, "defense"),
    "description": (("description", "desc", "描述", "效果"), "描述", 10, "description"),
    "keywords": (("keywords", "关键字", "关键词"), "关键字", 11, "keywordsNormalized"),
    "tags": (("tags", "标签"), "标签", 12, "tags"),
}
FIX_INT_FIELDS = {"cost", "operation_cost", "attack", "defense"}
FIX_LIST_FIELDS = {"keywords", "tags"}


def fix_field_key(token: object) -> str:
    """Map a user-supplied field token to its canonical fix-field key."""
    text = str(token or "").strip().lower()
    for key, (aliases, _, _, _) in FIX_FIELDS.items():
        if text in aliases:
            return key
    return ""


def parse_fix_value(field: str, value: object) -> tuple[bool, object]:
    """Validate and convert a correction value for one card field."""
    if field in FIX_INT_FIELDS:
        try:
            return True, int(str(value).strip())
        except (TypeError, ValueError):
            return False, "必须是整数"
    if field in FIX_LIST_FIELDS:
        text = str(value or "")
        parts = [part for part in re.split(r"[,，、;；\n]+", text) if part.strip()]
        return True, [part.strip() for part in parts]
    text = str(value or "").strip()
    return (True, text) if text else (False, "不能为空")


def apply_card_fix(import_id: str, field: str, value: object) -> dict:
    """Apply one field correction to a card and return the updated row info.

    Args:
        import_id: Import id of the card to correct.
        field: Canonical field key from :data:`FIX_FIELDS`.
        value: New value; converted by :func:`parse_fix_value`.

    Returns:
        A dictionary with ``label``, ``name``, ``shown`` and ``summary``.

    Raises:
        ValueError: If the field or value is invalid.
    """
    import_id = str(import_id or "").strip()
    if import_id not in deck_code.CARDS:
        raise ValueError("找不到卡牌：" + import_id)
    if field not in FIX_FIELDS:
        raise ValueError("未知字段：" + str(field))
    ok, parsed = parse_fix_value(field, value)
    if not ok:
        raise ValueError(f"{FIX_FIELDS[field][1]}{parsed}")
    _, label, index, entry_key = FIX_FIELDS[field]
    summary = import_cards([{"importId": import_id, entry_key: parsed}])
    row = deck_code.CARDS.get(import_id) or []
    shown = row[index] if len(row) > index else ""
    if isinstance(shown, list):
        shown = "、".join(str(item) for item in shown)
    name = (row[1] or row[2]) if len(row) > 2 else import_id
    return {
        "id": import_id,
        "field": field,
        "label": label,
        "value": shown,
        "name": name,
        "summary": summary,
    }


FACTION_ORDER = {
    "Germany": 0,
    "Britain": 1,
    "Japan": 2,
    "Soviet": 3,
    "USA": 4,
    "France": 5,
    "Italy": 6,
    "Poland": 7,
    "Finland": 8,
    "Anzac": 9,
    "Neutral": 10,
}

IMPORT_FIELDS = [
    "card_id",
    "name_zh",
    "name_en",
    "faction",
    "card_type",
    "rarity",
    "kredits",
    "operation_cost",
    "attack",
    "defense",
    "description",
    "keywords",
    "tags",
]

# Unit types share the "unit" card kind; orders and countermeasures are their
# own kind. Both the kind and the unit type are searchable.
CARD_KIND_CN = {
    "infantry": "单位",
    "tank": "单位",
    "artillery": "单位",
    "fighter": "单位",
    "bomber": "单位",
    "order": "指令",
    "countermeasure": "反制",
}

_STAT_FIELDS = {
    "cost": ("费用", "花费", "费", "cost", "kredits", "kredit"),
    "operation": ("行动费用", "行动", "operationcost", "operation", "op"),
    "attack": ("攻击力", "攻击", "攻", "atk", "attack"),
    "defense": ("防御力", "防御", "防", "def", "defense", "血"),
}
_STAT_INDEX = {"cost": 6, "operation": 7, "attack": 8, "defense": 9}

_BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _empty_deck(user_key: str = "") -> dict:
    return {"main": "", "ally": "", "cards": {}, "updated": 0, "user": user_key}


def chat_user_key(platform: object = "", sender: object = "") -> str:
    """Build a stable deck key for a chat sender.

    Args:
        platform: Platform id or name from the chat event.
        sender: Sender id from the chat event.

    Returns:
        A namespaced deck key unique to that sender.
    """
    platform_text = str(platform or "").strip() or "chat"
    sender_text = str(sender or "").strip() or "unknown"
    return f"chat:{platform_text}:{sender_text}"


def web_user_key(username: object = "") -> str:
    """Build a stable deck key for a dashboard user.

    Args:
        username: Authenticated dashboard username.

    Returns:
        A namespaced deck key unique to that dashboard user.
    """
    return "web:" + (str(username or "").strip() or "default")


def user_label(user_key: object) -> str:
    """Format a deck key for display.

    Args:
        user_key: Internal deck key.

    Returns:
        A short human-readable label.
    """
    key = str(user_key or "")
    if key.startswith("web:"):
        return "网页 " + (key[4:] or "默认")
    if key.startswith("chat:"):
        parts = key.split(":", 2)
        return "聊天 " + (parts[2] if len(parts) > 2 else "未知")
    return key or "默认"


def _deck_key(user_key: object = None) -> str:
    key = str(user_key or "").strip()
    return key or "default"


def _normalize_cards(raw_cards: object) -> dict[str, int]:
    cards: dict[str, int] = {}
    if not isinstance(raw_cards, dict):
        return cards
    for import_id, count in raw_cards.items():
        import_id = str(import_id)
        if import_id not in deck_code.CARDS or is_token_card(import_id):
            continue
        try:
            value = int(count)
        except (TypeError, ValueError):
            continue
        if value > 0:
            cards[import_id] = min(value, MAX_COPIES)
    return cards


def _normalize_deck(data: object, user_key: str = "") -> dict:
    if not isinstance(data, dict):
        return _empty_deck(user_key)
    try:
        updated = int(data.get("updated") or 0)
    except (TypeError, ValueError):
        updated = 0
    return {
        "main": str(data.get("main") or ""),
        "ally": str(data.get("ally") or ""),
        "cards": _normalize_cards(data.get("cards")),
        "updated": updated,
        "user": user_key,
    }


def is_token_card(import_id: str) -> bool:
    """Return whether a card is a spawn-only derived/Token card."""
    info = deck_code.CARDS.get(str(import_id))
    if not info or len(info) <= 5:
        return False
    return str(info[5]).strip() in {"Token", "衍生牌", "衍生", "Derivative"}


@contextmanager
def _db_connection():
    DECK_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DECK_DB, timeout=10)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS user_decks ("
        "user_key TEXT PRIMARY KEY, "
        "main TEXT NOT NULL DEFAULT '', "
        "ally TEXT NOT NULL DEFAULT '', "
        "cards TEXT NOT NULL DEFAULT '{}', "
        "updated INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS deck_meta ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    if conn.execute("SELECT 1 FROM deck_meta WHERE key = 'legacy_migrated'").fetchone():
        return
    try:
        legacy = _normalize_deck(json.loads(DECK_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        legacy = _empty_deck()
    _write_deck(conn, LEGACY_DECK_KEY, legacy)
    conn.execute(
        "INSERT OR REPLACE INTO deck_meta(key, value) VALUES('legacy_migrated', '1')"
    )


def _write_deck(conn: sqlite3.Connection, user_key: str, deck: dict) -> None:
    conn.execute(
        "INSERT INTO user_decks(user_key, main, ally, cards, updated) "
        "VALUES(?, ?, ?, ?, ?) "
        "ON CONFLICT(user_key) DO UPDATE SET "
        "main=excluded.main, ally=excluded.ally, cards=excluded.cards, "
        "updated=excluded.updated",
        (
            user_key,
            deck.get("main") or "",
            deck.get("ally") or "",
            json.dumps(deck.get("cards") or {}, ensure_ascii=False, sort_keys=True),
            int(deck.get("updated") or 0),
        ),
    )


def ensure_deck_card(import_id: str) -> None:
    """Validate that a card can be placed in a manual deck.

    Args:
        import_id: Import id of the card to check.

    Raises:
        ValueError: If the card is unknown or is a derived/Token card.
    """
    import_id = str(import_id or "").strip()
    if import_id not in deck_code.CARDS:
        raise ValueError("未知卡牌：" + import_id)
    if is_token_card(import_id):
        info = deck_code.CARDS[import_id]
        name = info[1] or info[2] or import_id
        raise ValueError(f"{name} 是衍生牌，不能加入卡组")


def add_to_deck(import_id: str, count: int = 1, user_key: object = None) -> dict:
    """Add copies of a card to one user's manual deck and persist it.

    Args:
        import_id: Import id of the card.
        count: Number of copies to add, capped at :data:`MAX_COPIES`.
        user_key: Deck owner key; defaults to the shared fallback key.

    Returns:
        The saved deck dictionary.

    Raises:
        ValueError: If the card is unknown or is a derived/Token card.
    """
    import_id = str(import_id or "").strip()
    ensure_deck_card(import_id)
    try:
        count = max(1, int(count))
    except (TypeError, ValueError):
        count = 1
    key = _deck_key(user_key)
    deck = load_deck(key)
    current = deck["cards"].get(import_id, 0)
    deck["cards"][import_id] = min(current + count, MAX_COPIES)
    return save_deck(deck, key)


def remove_from_deck(import_id: str, count: int = 1, user_key: object = None) -> dict:
    """Remove copies of a card from one user's manual deck and persist it.

    Args:
        import_id: Import id of the card.
        count: Number of copies to remove.
        user_key: Deck owner key; defaults to the shared fallback key.

    Returns:
        The saved deck dictionary.
    """
    import_id = str(import_id or "").strip()
    try:
        count = max(1, int(count))
    except (TypeError, ValueError):
        count = 1
    key = _deck_key(user_key)
    deck = load_deck(key)
    current = deck["cards"].get(import_id, 0)
    remaining = max(0, current - count)
    if remaining:
        deck["cards"][import_id] = remaining
    else:
        deck["cards"].pop(import_id, None)
    return save_deck(deck, key)


def load_deck(user_key: object = None) -> dict:
    """Load one user's manual deck from SQLite.

    Args:
        user_key: Deck owner key; defaults to the shared fallback key.

    Returns:
        A normalized deck dictionary with ``main``, ``ally``, ``cards``,
        ``updated`` and ``user`` keys. Unknown and derived cards are dropped.
        A new user starts from the legacy deck file imported on first run.
    """
    key = _deck_key(user_key)
    with _db_connection() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT main, ally, cards, updated FROM user_decks WHERE user_key = ?",
            (key,),
        ).fetchone()
        if row is None and key != LEGACY_DECK_KEY:
            legacy = conn.execute(
                "SELECT main, ally, cards, updated FROM user_decks WHERE user_key = ?",
                (LEGACY_DECK_KEY,),
            ).fetchone()
            if legacy:
                deck = _normalize_deck(
                    {
                        "main": legacy[0],
                        "ally": legacy[1],
                        "cards": json.loads(legacy[2] or "{}"),
                        "updated": legacy[3],
                    },
                    key,
                )
                _write_deck(conn, key, deck)
                return deck
        if row is None:
            return _empty_deck(key)
        return _normalize_deck(
            {
                "main": row[0],
                "ally": row[1],
                "cards": json.loads(row[2] or "{}"),
                "updated": row[3],
            },
            key,
        )


def save_deck(deck: dict, user_key: object = None) -> dict:
    """Persist one user's manual deck to SQLite.

    Args:
        deck: Deck dictionary to save.
        user_key: Deck owner key; falls back to ``deck['user']``.

    Returns:
        The same dictionary with an updated timestamp and owner key.
    """
    key = _deck_key(user_key if user_key is not None else deck.get("user"))
    deck["updated"] = int(time.time())
    deck["user"] = key
    with _db_connection() as conn:
        _ensure_schema(conn)
        _write_deck(conn, key, deck)
    return deck


def load_aliases() -> dict[str, list[str]]:
    """Load the user-maintained alias library from disk.

    Returns:
        Mapping of import id to a list of aliases. Unknown card ids are
        dropped and aliases are de-duplicated case-insensitively.
    """
    try:
        data = json.loads(ALIAS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    raw = data.get("aliases") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}

    aliases: dict[str, list[str]] = {}
    seen: set[str] = set()
    for import_id, items in raw.items():
        import_id = str(import_id)
        if import_id not in deck_code.CARDS:
            continue
        if isinstance(items, str):
            items = [items]
        if not isinstance(items, list):
            continue
        cleaned: list[str] = []
        for item in items:
            alias = str(item or "").strip()
            key = alias.lower()
            if not alias or key in seen:
                continue
            seen.add(key)
            cleaned.append(alias)
        if cleaned:
            aliases[import_id] = cleaned
    return aliases


def save_aliases(aliases: dict[str, list[str]]) -> None:
    """Persist the alias library to disk.

    Args:
        aliases: Mapping of import id to aliases.
    """
    payload = {"version": ALIAS_VERSION, "aliases": dict(sorted(aliases.items()))}
    try:
        ALIAS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ALIAS_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(ALIAS_FILE)
    except OSError:
        pass


def add_alias(import_id: str, alias: str) -> tuple[bool, str]:
    """Add one alias to a card, moving it away from any other card.

    Args:
        import_id: Card import id.
        alias: Alias text.

    Returns:
        ``(ok, message)``.
    """
    import_id = str(import_id or "").strip()
    text = str(alias or "").strip()
    if import_id not in deck_code.CARDS:
        return False, "未知卡牌"
    if not text:
        return False, "别名不能为空"
    if len(text) > 40:
        return False, "别名太长（最多 40 字）"

    aliases = load_aliases()
    lowered = text.lower()
    for other_id, items in list(aliases.items()):
        kept = [item for item in items if item.lower() != lowered]
        if kept:
            aliases[other_id] = kept
        else:
            aliases.pop(other_id, None)

    items = aliases.setdefault(import_id, [])
    if text not in items:
        items.append(text)
    save_aliases(aliases)
    return True, "已添加别名"


def remove_alias(import_id: str, alias: str) -> tuple[bool, str]:
    """Remove one alias from a card, or from every card when id is empty.

    Args:
        import_id: Card import id, or an empty string to search all cards.
        alias: Alias text to remove.

    Returns:
        ``(ok, message)``.
    """
    text = str(alias or "").strip()
    if not text:
        return False, "别名不能为空"

    aliases = load_aliases()
    lowered = text.lower()
    found = False
    for card_id, items in list(aliases.items()):
        if import_id and card_id != import_id:
            continue
        kept = [item for item in items if item.lower() != lowered]
        if len(kept) != len(items):
            found = True
            if kept:
                aliases[card_id] = kept
            else:
                aliases.pop(card_id, None)
    if not found:
        return False, "没有这个别名"
    save_aliases(aliases)
    return True, "已删除别名"


def infer_factions(cards: dict[str, int]) -> tuple[str, str | None]:
    """Infer the main and ally factions from the cards in a deck.

    Args:
        cards: Mapping of import id to copy count.

    Returns:
        ``(main_faction, ally_faction)``.  Neutral-only decks return
        ``("Neutral", None)``.
    """
    tally: dict[str, int] = {}
    for import_id, count in cards.items():
        info = deck_code.CARDS.get(import_id)
        if not info:
            continue
        faction = info[3]
        if faction and faction != "Neutral":
            tally[faction] = tally.get(faction, 0) + count
    ordered = sorted(tally.items(), key=lambda item: (-item[1], item[0]))
    main = ordered[0][0] if ordered else "Neutral"
    ally = ordered[1][0] if len(ordered) > 1 else None
    return main, ally


def resolve_factions(deck: dict) -> tuple[str, str | None]:
    """Resolve the stored faction overrides, falling back to inference.

    Args:
        deck: Deck dictionary returned by :func:`load_deck`.

    Returns:
        ``(main_faction, ally_faction)`` with valid faction names.
    """
    main = str(deck.get("main") or "")
    ally = str(deck.get("ally") or "")
    if main not in deck_code.FACTION_CODE:
        main, inferred_ally = infer_factions(deck.get("cards") or {})
        if not ally:
            ally = inferred_ally or ""
    if ally and ally not in deck_code.FACTION_CODE:
        ally = ""
    if ally == main:
        ally = ""
    return main, ally or None


def _synthesize_code(cards: dict[str, int], main: str, ally: str | None) -> str:
    temp_ids = [cid for cid in cards if not re.fullmatch(r"[0-9A-Za-z]{2}", str(cid))]
    if temp_ids:
        shown = "、".join(temp_ids[:5])
        if len(temp_ids) > 5:
            shown += f" 等 {len(temp_ids)} 个"
        raise ValueError(
            "卡组中存在临时卡牌 ID，无法生成标准卡组码，请先设置永久 ID：" + shown
        )
    header = deck_code.FACTION_CODE.get(main, "0")
    if ally and ally != main:
        header += deck_code.FACTION_CODE.get(ally, "")
    groups: dict[int, list[str]] = {1: [], 2: [], 3: [], 4: []}
    for import_id, count in cards.items():
        groups[min(max(int(count), 1), 4)].append(str(import_id))
    body = ";".join("".join(groups[index]) for index in (1, 2, 3, 4))
    return f"%%{header}|{body}"


def build_decoded(deck: dict) -> deck_code.DecodedDeck:
    """Build a decoded deck from the manual deck data.

    Args:
        deck: Deck dictionary returned by :func:`load_deck`.

    Returns:
        A decoded deck that can be passed to the image renderer.

    Raises:
        ValueError: If the manual deck is empty.
    """
    cards = deck.get("cards") or {}
    if not cards:
        raise ValueError("手动卡组还是空的，先添加几张卡吧")
    main, ally = resolve_factions(deck)
    code = _synthesize_code(cards, main, ally)
    return deck_code.decode_deck_code(code)


def _row_extra(info: list) -> tuple[str, list[str]]:
    """Return description and keywords from a card row, tolerating old rows."""
    description = str(info[10]) if len(info) > 10 and info[10] else ""
    keywords = info[11] if len(info) > 11 and isinstance(info[11], list) else []
    return description, keywords


def _row_tags(info: list) -> list[str]:
    """Return vision/custom tags from a card row, tolerating old rows."""
    tags = info[12] if len(info) > 12 and isinstance(info[12], list) else []
    result: list[str] = []
    for tag in tags:
        text = str(tag or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def deck_payload(deck: dict) -> dict:
    """Build the JSON payload used by the WebUI page.

    Args:
        deck: Deck dictionary returned by :func:`load_deck`.

    Returns:
        A JSON-serializable summary with resolved factions and card rows.
    """
    main, ally = resolve_factions(deck)
    rows: list[dict] = []
    total = 0
    for import_id, count in deck["cards"].items():
        info = deck_code.CARDS.get(import_id)
        if not info:
            continue
        faction = info[3]
        description, keywords = _row_extra(info)
        rows.append(
            {
                "id": import_id,
                "card_id": info[0],
                "name": info[1] or info[2] or import_id,
                "name_en": info[2] or "",
                "faction": faction,
                "faction_cn": deck_code.FACTION_CN.get(faction, faction),
                "type": info[4],
                "type_cn": deck_code.TYPE_CN.get(info[4], info[4]),
                "rarity": info[5],
                "rarity_cn": deck_code.RARITY_CN.get(info[5], info[5]),
                "cost": info[6],
                "attack": info[8],
                "defense": info[9],
                "description": description,
                "keywords": keywords,
                "keywords_cn": [deck_code.keyword_label(k) for k in keywords],
                "count": count,
            }
        )
        total += count

    rank = {main: 0, ally: 1, "Neutral": 2}
    rows.sort(
        key=lambda row: (
            rank.get(row["faction"], 3),
            row["cost"] if row["cost"] is not None else 99,
            row["name"],
        )
    )
    warnings = []
    if total != deck_code.DECK_SIZE - deck_code.HQ_COUNT:
        warnings.append(
            f"当前 {total} 张，标准主卡组应为 "
            f"{deck_code.DECK_SIZE - deck_code.HQ_COUNT} 张（HQ 不计入）"
        )
    return {
        "main": main,
        "ally": ally or "",
        "cards": rows,
        "total": total,
        "warnings": warnings,
        "updated": deck.get("updated", 0),
        "user": user_label(deck.get("user")),
    }


def _card_stat(info: list, index: int) -> int:
    """Read one numeric stat from a card row, tolerating bad data."""
    try:
        return int(info[index] or 0) if len(info) > index else 0
    except (TypeError, ValueError):
        return 0


def card_tag_text(import_id: str, info: list, aliases=None) -> str:
    """Build the searchable tag text for one card.

    The text covers every attribute the chat and page searches expose: names,
    import id, aliases, faction, card kind, unit type, rarity, cost, operation
    cost, attack, defense, keywords, description, and optional vision tags.

    Args:
        import_id: Two-character import id.
        info: Card row from ``deck_code.CARDS``.
        aliases: User-defined aliases for this card.

    Returns:
        Lowercase, space-separated tag text.
    """

    def at(index, default=""):
        if len(info) > index and info[index] not in (None, ""):
            return info[index]
        return default

    card_id = str(at(0))
    name_zh = str(at(1))
    name_en = str(at(2))
    faction = str(at(3))
    card_type = str(at(4))
    rarity = str(at(5))
    cost = _card_stat(info, 6)
    operation = _card_stat(info, 7)
    attack = _card_stat(info, 8)
    defense = _card_stat(info, 9)
    description, keywords = _row_extra(info)
    kind = CARD_KIND_CN.get(card_type, card_type)

    parts = [
        card_id,
        name_zh,
        name_en,
        import_id,
        *(aliases or []),
        faction,
        deck_code.FACTION_CN.get(faction, faction),
        card_type,
        deck_code.TYPE_CN.get(card_type, card_type),
        kind,
        rarity,
        deck_code.RARITY_CN.get(rarity, rarity),
        f"{cost}k",
        f"费用{cost}",
        f"花费{cost}",
        f"cost{cost}",
        f"行动{operation}",
        f"行动费用{operation}",
        f"op{operation}",
        f"攻击{attack}",
        f"攻{attack}",
        f"atk{attack}",
        f"防御{defense}",
        f"防{defense}",
        f"def{defense}",
        f"血{defense}",
        *keywords,
        *(deck_code.keyword_label(k) for k in keywords),
        description,
    ]
    # ``6K2`` is the standard way to write cost 6 with operation cost 2.
    if operation:
        parts.append(f"{cost}k{operation}")
    # Extra labels written by the vision model during a card import.
    if len(info) > 12 and isinstance(info[12], list):
        parts.extend(info[12])
    return " ".join(str(part).strip() for part in parts if str(part).strip()).lower()


def _parse_stat_token(token: str) -> list[tuple[str, int]]:
    """Parse one stat filter token.

    Supported forms:
        ``5/4`` / ``5-4``: attack 5, defense 4.
        ``3/5/4``: cost 3, attack 5, defense 4.
        ``6K``: cost 6.
        ``6K2``: cost 6, operation cost 2.
        ``6K/5/4`` / ``6K2/5/4``: cost and optional operation cost plus stats.
        ``费用6`` / ``6费`` / ``攻击5`` / ``5攻`` / ``行动2`` and English forms.

    Args:
        token: One whitespace-free search token.

    Returns:
        A list of ``(field, value)`` pairs, or an empty list when the token is
        not a stat filter.
    """
    text = token.strip().lower()
    if not text:
        return []

    def cost_spec(value):
        match = re.fullmatch(r"(\d+)k(\d*)", value)
        if not match:
            return None
        stats = [("cost", int(match.group(1)))]
        if match.group(2):
            stats.append(("operation", int(match.group(2))))
        return stats

    stats = cost_spec(text)
    if stats:
        return stats

    # Slash/hyphen separated numbers. A leading ``nK`` (optionally with the
    # operation cost attached) is a cost spec, otherwise three numbers mean
    # cost/attack/defense and two numbers mean attack/defense.
    parts = re.split(r"[/\-]", text)
    if len(parts) >= 2:
        stats = []
        rest = parts
        leading = cost_spec(parts[0])
        if leading:
            stats.extend(leading)
            rest = parts[1:]
        elif len(parts) == 3 and parts[0].isdigit():
            stats.append(("cost", int(parts[0])))
            rest = parts[1:]
        if len(rest) == 2 and all(part.isdigit() for part in rest):
            stats.append(("attack", int(rest[0])))
            stats.append(("defense", int(rest[1])))
            return stats
        if len(rest) == 1 and rest[0].isdigit():
            stats.append(("attack", int(rest[0])))
            return stats
        return []

    # Number followed by a Chinese/English label, e.g. ``5攻`` / ``2行动``.
    match = re.fullmatch(
        r"(\d+)(费用|花费|行动费用|行动|攻击力|攻击|攻|防御力|防御|防|血|费)", text
    )
    if match:
        for field, names in _STAT_FIELDS.items():
            if match.group(2) in names:
                return [(field, int(match.group(1)))]
        return []

    # Label followed by a number, e.g. ``攻击5`` / ``op=2``.
    match = re.fullmatch(
        r"(费用|花费|行动费用|行动|攻击力|攻击|攻|防御力|防御|防|血|费|"
        r"cost|kredits|kredit|operationcost|operation|op|atk|attack|def|defense)"
        r"[:=]?(\d+)",
        text,
    )
    if match:
        for field, names in _STAT_FIELDS.items():
            if match.group(1) in names:
                return [(field, int(match.group(2)))]
    return []


def search_cards(query: str, limit: int = 10) -> list[str]:
    """Search cards by space-separated tags and stat filters.

    Every token must match (AND). Free tokens are substring-matched against
    :func:`card_tag_text`; stat tokens are compared exactly.

    Args:
        query: Search expression, e.g. ``德国 坦克 6K/5/4 闪击``.
        limit: Maximum number of matches to return; ``0`` means unlimited.

    Returns:
        Matching import ids, exact name/alias hits first.
    """
    text = unicodedata.normalize("NFKC", str(query or "")).strip().lower()
    if not text:
        return []
    stats: list[tuple[str, int]] = []
    free: list[str] = []
    for token in text.split():
        parsed = _parse_stat_token(token)
        if parsed:
            stats.extend(parsed)
        else:
            free.append(token)

    aliases = load_aliases()
    hits: list[tuple[int, int, int, str, str]] = []
    for import_id, info in deck_code.CARDS.items():
        if not isinstance(info, list) or not info:
            continue
        if any(_card_stat(info, _STAT_INDEX[field]) != value for field, value in stats):
            continue
        card_aliases = aliases.get(import_id, [])
        tags = card_tag_text(import_id, info, card_aliases)
        compact_tags = compact_text(tags)
        if any(compact_text(token) not in compact_tags for token in free):
            continue
        name_zh = str(info[1] or "").lower()
        name_en = str(info[2] or "").lower()
        card_id = str(info[0] or "").lower()
        first = compact_text(free[0]) if free else ""
        if first and (
            first
            in (
                compact_text(name_zh),
                compact_text(name_en),
                compact_text(card_id),
                compact_text(import_id),
            )
            or first in (compact_text(alias) for alias in card_aliases)
        ):
            score = 0
        elif first and (
            compact_text(name_zh).startswith(first)
            or compact_text(name_en).startswith(first)
        ):
            score = 1
        else:
            score = 2
        hits.append(
            (
                score,
                FACTION_ORDER.get(info[3], 99),
                _card_stat(info, 6),
                name_zh or name_en,
                import_id,
            )
        )
    hits.sort()
    return [hit[-1] for hit in (hits if limit <= 0 else hits[:limit])]


def card_catalog() -> list[dict]:
    """Return every locally known card for the WebUI catalog.

    Returns:
        A sorted list of compact card dictionaries, including description,
        keywords, and the user-defined aliases.
    """
    aliases = load_aliases()
    rows: list[dict] = []
    for import_id, info in deck_code.CARDS.items():
        faction = info[3]
        description, keywords = _row_extra(info)
        card_aliases = aliases.get(import_id, [])
        rows.append(
            {
                "id": import_id,
                "card_id": info[0],
                "name": info[1] or info[2] or import_id,
                "name_en": info[2] or "",
                "faction": faction,
                "faction_cn": deck_code.FACTION_CN.get(faction, faction),
                "type": info[4],
                "type_cn": deck_code.TYPE_CN.get(info[4], info[4]),
                "rarity": info[5],
                "rarity_cn": deck_code.RARITY_CN.get(info[5], info[5]),
                "cost": info[6],
                "operation": info[7],
                "attack": info[8],
                "defense": info[9],
                "description": description,
                "keywords": keywords,
                "keywords_cn": [deck_code.keyword_label(k) for k in keywords],
                "aliases": card_aliases,
                "tags": card_tag_text(import_id, info, card_aliases),
            }
        )
    rows.sort(
        key=lambda row: (
            FACTION_ORDER.get(row["faction"], 99),
            row["cost"] if row["cost"] is not None else 99,
            row["name"],
        )
    )
    return rows


def find_cards(query: str, limit: int = 8, deep: bool = False) -> list[str]:
    """Find import ids matching a name, short id, alias, or description.

    Args:
        query: Search text.
        limit: Maximum number of matches to return; ``0`` means unlimited.
        deep: Also match description and ability labels. Used by the search
            command; add/remove keep the shorter name-and-alias lookup.

    Returns:
        Matching import ids, best matches first.
    """
    text = str(query or "").strip().lower()
    compact = compact_text(text)
    if not compact:
        return []
    if compact in deck_code.CARDS:
        return [compact]

    aliases = load_aliases()
    exact: list[str] = []
    prefix: list[str] = []
    partial: list[str] = []

    def remember(bucket: list[str], import_id: str) -> None:
        if import_id not in bucket:
            bucket.append(import_id)

    for import_id, info in deck_code.CARDS.items():
        name_zh = compact_text(info[1])
        name_en = compact_text(info[2])
        card_id = compact_text(info[0])

        if compact in (name_zh, name_en, card_id):
            remember(exact, import_id)
        elif name_zh.startswith(compact) or name_en.startswith(compact):
            remember(prefix, import_id)
        elif compact in name_zh or compact in name_en or compact in card_id:
            remember(partial, import_id)
        else:
            for alias in aliases.get(import_id, []):
                lowered = compact_text(alias)
                if compact == lowered:
                    remember(exact, import_id)
                    break
                if lowered.startswith(compact):
                    remember(prefix, import_id)
                    break
                if compact in lowered:
                    remember(partial, import_id)
                    break
            else:
                if deep:
                    description, keywords = _row_extra(info)
                    labels = compact_text(
                        " ".join(
                            [description]
                            + [deck_code.keyword_label(k) for k in keywords]
                        )
                    )
                    if compact in labels:
                        remember(partial, import_id)

    for bucket in (exact, prefix, partial):
        if bucket:
            return bucket if limit <= 0 else bucket[:limit]
    return []


def _random_base62(length: int = 6) -> str:
    import secrets

    return "".join(secrets.choice(_BASE62) for _ in range(max(1, length)))


def _generate_temp_import_id(existing: dict, length: int = 6) -> str:
    """Generate a collision-free temporary import id for a new card.

    New cards use ``DIY-<random>`` as a temporary id until a permanent id is
    assigned by command.

    Args:
        existing: Current card table.
        length: Number of random base62 characters after ``DIY-``.

    Returns:
        A temporary import id not present in ``existing``.
    """
    for _ in range(10000):
        import_id = "DIY-" + _random_base62(length)
        if import_id not in existing:
            return import_id
    raise ValueError("无法生成临时卡牌 ID，请稍后重试")


def _is_valid_permanent_import_id(import_id: str) -> bool:
    """Return True when ``import_id`` is a valid permanent import id.

    Permanent ids use two base62 characters so they remain compatible with the
    standard deck import/export code format.
    """
    return bool(re.fullmatch(r"[0-9A-Za-z]{2}", str(import_id or "")))


def import_cards(payload) -> dict:
    """Merge official card data into ``deck_cards.json``.

    Args:
        payload: A list of card dictionaries, a ``{"cards": [...]}`` object,
            a mapping of import id to row, or a JSON string containing any of
            those shapes. Fards-style and simplified field names are accepted.

    Returns:
        A summary with added, updated, unchanged and total counts plus the
        per-card actions and the backup path.

    Raises:
        ValueError: If the payload cannot be parsed or contains no valid card.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise ValueError("JSON 解析失败：" + str(exc)) from exc

    if isinstance(payload, dict) and "cards" in payload:
        raw_cards = payload.get("cards")
        if isinstance(raw_cards, dict):
            entries = []
            for import_id, row in raw_cards.items():
                if isinstance(row, dict):
                    item = dict(row)
                    item.setdefault("importId", import_id)
                    entries.append(item)
                elif isinstance(row, list):
                    item = dict(zip(IMPORT_FIELDS, row))
                    item["importId"] = import_id
                    entries.append(item)
        else:
            entries = raw_cards
    elif isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        entries = [payload]
    else:
        entries = []

    if not isinstance(entries, list):
        raise ValueError("卡牌数据必须是数组或 {cards: [...]}")

    def localized(value) -> str:
        if isinstance(value, dict):
            for key in ("zh-Hans", "zh", "zh-CN", "en", "en-US", "en-EN"):
                if value.get(key):
                    return str(value[key])
            for item in value.values():
                if item:
                    return str(item)
        return str(value or "")

    def to_int(value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def reverse(mapping: dict, value, fallback: str = "") -> str:
        text = str(value or "").strip()
        if not text:
            return fallback
        if text in mapping:
            return text
        for key, label in mapping.items():
            if text.lower() in (key.lower(), str(label).lower()):
                return key
        return text

    try:
        data = json.loads(CARDS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {"meta": {}, "cards": {}}
    if not isinstance(data, dict):
        data = {"meta": {}, "cards": {}}
    table = data.get("cards")
    if not isinstance(table, dict):
        table = {}

    by_card_id: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for import_id, row in table.items():
        if not isinstance(row, list) or not row:
            continue
        by_card_id[str(row[0])] = import_id
        if len(row) > 1 and row[1]:
            by_name[str(row[1]).strip().lower()] = import_id
        if len(row) > 2 and row[2]:
            by_name.setdefault(str(row[2]).strip().lower(), import_id)

    added = updated = unchanged = 0
    results: list[dict] = []

    def first(card: dict, *keys):
        for key in keys:
            if key in card and card[key] is not None:
                return True, card[key]
        return False, None

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        has_import, raw_import = first(entry, "importId", "import_id", "id")
        import_id = str(raw_import or "").strip() if has_import else ""
        has_card, raw_card = first(entry, "cardId", "card_id")
        card_id = str(raw_card or "").strip() if has_card else ""
        has_zh, raw_zh = first(entry, "nameZh", "name_zh", "name", "title")
        name_zh = localized(raw_zh).strip() if has_zh else ""
        has_en, raw_en = first(entry, "nameEn", "name_en", "english", "titleEn")
        name_en = localized(raw_en).strip() if has_en else ""

        if import_id and import_id not in table:
            # If an unknown id was supplied but we can identify an existing card
            # by card id/name, update that card instead of creating a duplicate.
            matched = (
                by_card_id.get(card_id)
                or by_name.get(name_zh.lower())
                or by_name.get(name_en.lower())
            )
            if matched:
                import_id = matched
        if not import_id:
            lookup = card_id or name_zh or name_en
            import_id = (
                by_card_id.get(card_id)
                or by_name.get(name_zh.lower())
                or by_name.get(name_en.lower())
                or ""
            )
            if not import_id:
                if not lookup:
                    continue
                # New cards default to a temporary DIY id.
                import_id = _generate_temp_import_id(table)

        old = table.get(import_id)
        old_row = old if isinstance(old, list) else []

        def old_at(index, default):
            if len(old_row) > index and old_row[index] not in (None, ""):
                return old_row[index]
            return default

        if not card_id:
            card_id = old_at(0, "")
        if not name_zh:
            name_zh = old_at(1, "")
        if not name_en:
            name_en = old_at(2, "")
        if not name_zh and not name_en:
            continue
        if not card_id:
            card_id = (
                import_id
                or re.sub(r"[^0-9A-Za-z_]+", "_", name_en or name_zh).strip("_").lower()
            )

        has_faction, raw_faction = first(entry, "faction", "nation", "nationName")
        faction = (
            reverse(deck_code.FACTION_CN, raw_faction, old_at(3, "Neutral"))
            if has_faction
            else old_at(3, "Neutral")
        )
        has_type, raw_type = first(entry, "type", "cardType", "card_type")
        card_type = (
            reverse(deck_code.TYPE_CN, raw_type, old_at(4, ""))
            if has_type
            else old_at(4, "")
        )
        has_rarity, raw_rarity = first(entry, "rarity")
        if has_rarity:
            alias = deck_code.RARITY_ALIASES.get(str(raw_rarity or "").strip())
            if alias:
                raw_rarity = alias
        rarity = (
            reverse(deck_code.RARITY_CN, raw_rarity, old_at(5, "Standard"))
            if has_rarity
            else old_at(5, "Standard")
        )
        has_kredits, raw_kredits = first(entry, "kredits", "cost", "credits")
        kredits = to_int(raw_kredits, old_at(6, 0)) if has_kredits else old_at(6, 0)
        has_operation, raw_operation = first(
            entry, "operationCost", "operation_cost", "opCost"
        )
        operation_cost = (
            to_int(raw_operation, old_at(7, 0)) if has_operation else old_at(7, 0)
        )
        has_attack, raw_attack = first(entry, "attack")
        attack = to_int(raw_attack, old_at(8, 0)) if has_attack else old_at(8, 0)
        has_defense, raw_defense = first(entry, "defense")
        defense = to_int(raw_defense, old_at(9, 0)) if has_defense else old_at(9, 0)
        has_description, raw_description = first(entry, "description", "desc", "text")
        description = (
            str(raw_description or "").strip() if has_description else old_at(10, "")
        )
        has_keywords, raw_keywords = first(
            entry, "keywordsNormalized", "keywords", "attributes"
        )
        keywords: list[str] = []
        if has_keywords:
            if isinstance(raw_keywords, str):
                raw_keywords = re.split(r"[,，;\s]+", raw_keywords)
            if isinstance(raw_keywords, list):
                for keyword in raw_keywords:
                    keyword = str(keyword or "").strip()
                    # Normalize ``heavyArmor1`` / ``intel2`` to the colon form.
                    match = re.fullmatch(r"(heavyArmor|intel)([123])", keyword)
                    if match:
                        keyword = f"{match.group(1)}:{match.group(2)}"
                    if keyword and keyword not in keywords:
                        keywords.append(keyword)
        else:
            keywords = list(old_at(11, []))

        has_tags, raw_tags = first(entry, "tags", "vision_tags", "labels", "tag")
        tags: list[str] = []
        if has_tags:
            if isinstance(raw_tags, str):
                raw_tags = re.split(r"[,，;；、\n]+", raw_tags)
            if isinstance(raw_tags, list):
                for tag in raw_tags:
                    tag = str(tag or "").strip()
                    if tag and tag not in tags:
                        tags.append(tag)
        else:
            tags = _row_tags(old_row)

        row = [
            card_id,
            name_zh or name_en,
            name_en or name_zh,
            faction,
            card_type,
            rarity,
            kredits,
            operation_cost,
            attack,
            defense,
            description,
            keywords,
            tags,
        ]
        old_normalized = list(old_row[:13]) if old is not None else []
        if old_normalized != row:
            if old is None:
                added += 1
                action = "added"
            else:
                updated += 1
                action = "updated"
        else:
            unchanged += 1
            action = "unchanged"
        table[import_id] = row
        by_card_id[card_id] = import_id
        by_name[name_zh.lower()] = import_id
        by_name[name_en.lower()] = import_id
        results.append({"id": import_id, "name": row[1], "action": action})

    if not results:
        raise ValueError("没有解析到有效卡牌")

    backup_path = ""
    try:
        backup = CARDS_FILE.with_name(
            "deck_cards.json.bak." + datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        shutil.copy2(CARDS_FILE, backup)
        backup_path = str(backup)
    except OSError:
        pass

    data["cards"] = dict(sorted(table.items()))
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    meta["count"] = len(table)
    meta["generated"] = time.strftime("%Y-%m-%d")
    meta.setdefault("source", "manual import")
    meta["fields"] = IMPORT_FIELDS
    data["meta"] = meta

    try:
        tmp = CARDS_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        tmp.replace(CARDS_FILE)
    except OSError as exc:
        raise ValueError("写入卡表失败：" + str(exc)) from exc

    deck_code.CARDS.clear()
    deck_code.CARDS.update(data["cards"])
    return {
        "added": added,
        "updated": updated,
        "unchanged": unchanged,
        "total": len(table),
        "backup": backup_path,
        "cards": results,
    }


def _save_cards_table(data: dict) -> str:
    """Persist ``deck_cards.json`` atomically and return backup path if any."""
    backup_path = ""
    try:
        backup = CARDS_FILE.with_name(
            "deck_cards.json.bak." + datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        if CARDS_FILE.exists():
            shutil.copy2(CARDS_FILE, backup)
            backup_path = str(backup)
    except OSError:
        pass
    tmp = CARDS_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    tmp.replace(CARDS_FILE)
    deck_code.CARDS.clear()
    deck_code.CARDS.update(data.get("cards") or {})
    return backup_path


def set_permanent_id(target: str, new_id: str) -> dict:
    """Set a permanent import id for a card (typically a DIY temporary card)."""
    target = str(target or "").strip()
    new_id = str(new_id or "").strip()
    if not target:
        raise ValueError("请提供要设置永久 ID 的卡牌（临时ID或卡名）")
    if not _is_valid_permanent_import_id(new_id):
        raise ValueError("永久 ID 必须是 2 位字母或数字（用于卡组码兼容）")

    try:
        data = json.loads(CARDS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("读取卡表失败：" + str(exc)) from exc
    if not isinstance(data, dict):
        data = {"meta": {}, "cards": {}}
    table = data.get("cards")
    if not isinstance(table, dict):
        table = {}
        data["cards"] = table

    old_id = target if target in table else ""
    if not old_id:
        matches = find_cards(target, limit=10, deep=True) or search_cards(
            target, limit=10
        )
        if len(matches) == 1:
            old_id = matches[0]
        elif len(matches) > 1:
            names = []
            for mid in matches[:8]:
                info = table.get(mid) or []
                name = info[1] if isinstance(info, list) and len(info) > 1 else mid
                names.append(f"{mid}:{name}")
            raise ValueError("找到多张卡牌，请指定临时 ID：" + "、".join(names))
    if not old_id or old_id not in table:
        raise ValueError(f"未找到卡牌：{target}")
    if old_id == new_id:
        raise ValueError("新旧 ID 相同")
    if new_id in table:
        info = table[new_id]
        name = info[1] if isinstance(info, list) and len(info) > 1 else new_id
        raise ValueError(f"永久 ID {new_id} 已被占用：{name}")

    table[new_id] = table.pop(old_id)
    data["cards"] = dict(sorted(table.items()))
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    meta["count"] = len(table)
    meta["generated"] = time.strftime("%Y-%m-%d")
    meta.setdefault("source", "manual import")
    meta["fields"] = IMPORT_FIELDS
    data["meta"] = meta
    backup_path = _save_cards_table(data)

    aliases = load_aliases()
    if old_id in aliases:
        aliases[new_id] = aliases.pop(old_id)
        save_aliases(aliases)

    with _db_connection() as conn:
        _ensure_schema(conn)
        rows = conn.execute("SELECT user_key, cards FROM user_decks").fetchall()
        for user_key, cards_json in rows:
            cards = json.loads(cards_json or "{}")
            if old_id in cards:
                cards[new_id] = cards.pop(old_id)
                conn.execute(
                    "UPDATE user_decks SET cards = ? WHERE user_key = ?",
                    (json.dumps(cards, ensure_ascii=False, sort_keys=True), user_key),
                )

    info = table.get(new_id) or []
    name = (
        info[1] if len(info) > 1 and info[1] else (info[2] if len(info) > 2 else new_id)
    )
    return {"old_id": old_id, "new_id": new_id, "name": name, "backup": backup_path}
