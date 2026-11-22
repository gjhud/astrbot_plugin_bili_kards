"""KARDS 卡牌数据整理脚本（插件内置工具版）。
把原始卡牌 JSON 整理成 deck_code.py 加载的 deck_cards.json。
字段顺序必须与 deck_code.decode_deck_code 的关键字访问一致。

两种写入模式：
  覆盖模式（默认）  完全以本次源文件为准，源里没有的卡会被丢掉
  增量模式 --merge  由工作区 merge_cards.py 整合而来：源里有的覆盖，
                    源里没有的保留，适合官方数据缺字段时兜底

用法: python tools/sync_deck_cards.py 原始data.json
      [--merge] [--dry-run] [--report] [-o 输出路径]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import data_paths  # noqa: E402

DEFAULT_OUT = data_paths.CARDS_FILE

FIELD_ORDER = [
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
]

NUMERIC_FIELDS = ("kredits", "operation_cost", "attack", "defense")


def _zero(value):
    """数值字段兜底，None 一律当 0。"""
    return 0 if value is None else value


def _localized(value, lang):
    """从多语言字段里取指定语言，缺失时回退到常用语言。"""
    if isinstance(value, dict):
        for key in (lang, "zh-Hans", "en-EN"):
            text = value.get(key)
            if isinstance(text, str) and text:
                return text
        return ""
    return value if isinstance(value, str) else ""


def _keywords(raw):
    """收集规范化关键字，去重并保持顺序。

    ``keywordsNormalized`` 是官方能力列表；``attributes`` 还混有
    BecomesVeteran 等内部标记，只在没有前者的简化数据里兜底。
    """
    source = raw.get("keywordsNormalized")
    if not source:
        source = raw.get("keywords") or raw.get("attributes") or []
    result = []
    for key in source:
        text = str(key or "").strip()
        match = re.fullmatch(r"(heavyArmor|intel)([123])", text)
        if match:
            text = f"{match.group(1)}:{match.group(2)}"
        if text and text not in result:
            result.append(text)
    return result


def normalize_card(entry):
    """把一条原始记录转成统一结构，无效记录返回 None。"""
    if not isinstance(entry, dict):
        return None
    raw = entry.get("json")
    if not isinstance(raw, dict):
        # 兼容 Fards 的扁平结构，字段直接挂在条目上
        raw = entry
    import_id = entry.get("importId") or raw.get("importId") or raw.get("import_id")
    card_id = entry.get("cardId") or raw.get("cardId") or raw.get("id")
    if not import_id or not card_id:
        return None
    name_zh = _localized(raw.get("title") or raw.get("nameZh"), "zh-Hans")
    name_en = (
        _localized(raw.get("title") or raw.get("nameEn"), "en-EN")
        or str(raw.get("nameEn") or "")
        or str(card_id)
    )
    return {
        "import_id": str(import_id),
        "card_id": str(card_id),
        "name_zh": name_zh or str(raw.get("nameZh") or ""),
        "name_en": name_en,
        "faction": raw.get("faction") or "",
        "card_type": raw.get("type") or raw.get("cardType") or "",
        "rarity": raw.get("rarity") or "",
        # 数值列可能是 null，统一兜底成 0，避免解码时 None 参与运算
        "kredits": _zero(raw.get("kredits")),
        "operation_cost": _zero(raw.get("operationCost", raw.get("operation_cost"))),
        "attack": _zero(raw.get("attack")),
        "defense": _zero(raw.get("defense")),
        "description": str(raw.get("description") or "").strip(),
        "keywords": _keywords(raw),
    }


def load_source(path):
    """读取原始卡牌 JSON，兼容 [...] 与 {"cards": [...]} 两种形态。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        entries = data.get("cards") or []
    else:
        entries = []
    cards = []
    for entry in entries:
        card = normalize_card(entry)
        if card is not None:
            cards.append(card)
    return data, cards


def build_payload(cards, source="", generated=None):
    """按 FIELD_ORDER 生成插件可直接读取的表结构。"""
    table = {}
    for card in cards:
        table[card["import_id"]] = [card[key] for key in FIELD_ORDER]
    return {
        "meta": {
            "source": source,
            "generated": generated or dt.date.today().isoformat(),
            "count": len(table),
        },
        "cards": dict(sorted(table.items())),
    }


def load_existing(path):
    """读回插件现有卡表，用于比对，失败时返回空表。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cards = data.get("cards") if isinstance(data, dict) else None
    return cards if isinstance(cards, dict) else {}


def diff_tables(old, new):
    """返回新增、删除、字段变更三组 import_id。"""
    old_ids = set(old or {})
    new_ids = set(new)
    added = sorted(new_ids - old_ids)
    removed = sorted(old_ids - new_ids)
    changed = sorted(cid for cid in (old_ids & new_ids) if old[cid] != new[cid])
    return added, removed, changed


def write_output(path, payload):
    """落盘并返回序列化文本，便于统一校验。"""
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    Path(path).write_text(text + chr(10), encoding="utf-8")
    return text


def merge_tables(existing, new):
    """增量合并：已有卡保留，源里出现的按新数据覆盖。返回 (表, 新增数, 覆盖数)。"""
    merged = dict(existing or {})
    added = 0
    updated = 0
    for import_id, row in new.items():
        if import_id not in merged:
            added += 1
        elif list(merged[import_id]) != list(row):
            updated += 1
        merged[import_id] = row
    return dict(sorted(merged.items())), added, updated


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="整理 KARDS 卡牌数据，生成插件 deck_cards.json"
    )
    parser.add_argument("source", help="原始卡牌 JSON 路径")
    parser.add_argument("-o", "--output", default=str(DEFAULT_OUT), help="输出卡表路径")
    parser.add_argument(
        "--merge",
        action="store_true",
        help="增量合并，保留本次源文件里没有的旧卡",
    )
    parser.add_argument("--dry-run", action="store_true", help="只比对，不写文件")
    parser.add_argument("--report", action="store_true", help="打印新增与变更明细")
    args = parser.parse_args(argv)

    _, cards = load_source(args.source)
    if not cards:
        print("未解析到有效卡牌，检查源文件结构")
        return 1

    payload = build_payload(cards, source=str(args.source))
    existing = load_existing(args.output)

    if args.merge:
        table, added_count, updated = merge_tables(existing, payload["cards"])
        payload["cards"] = table
        payload["meta"]["count"] = len(table)
        added = sorted(set(table) - set(existing))
        removed = []
        changed = sorted(
            cid
            for cid in (set(existing) & set(table))
            if list(existing[cid]) != list(table[cid])
        )
        print(
            f"增量合并：源 {len(cards)} 张，合并后 {len(table)} 条；"
            f"新增 {added_count}，覆盖 {updated}"
        )
    else:
        added, removed, changed = diff_tables(existing, payload["cards"])
        print(
            f"解析 {len(cards)} 张，输出 {payload['meta']['count']} 条；"
            f"新增 {len(added)}，删除 {len(removed)}，变更 {len(changed)}"
        )

    if args.report:
        for cid in (added + changed)[:50]:
            print("  变更", cid, payload["cards"][cid][:3])

    if args.dry_run:
        print("dry-run，未写入")
        return 0

    write_output(args.output, payload)
    print("已写入", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
