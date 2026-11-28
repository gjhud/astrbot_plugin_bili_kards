import asyncio
import base64
import json
import re
import secrets
import string
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Star, register
from astrbot.core.message.components import Image, Plain
from astrbot.core.star.filter.command import GreedyStr

from . import bili_fetch as bf
from . import data_paths, deck_code, deck_image, diy_card, manual_deck, web_api
from . import dynamic_fetch as df
from . import dynamic_render as dr
from .media_cache import ImageCache

COOLDOWN = 3.0
PENDING_IMPORT_TTL = 60
SEARCH_PAGE_SIZE = 15
SEARCH_PAGE_TTL = 300
_last = {}
# Persist the "command then image" state so a plugin reload cannot silently
# swallow the card images the user sends afterwards.
PENDING_IMPORT_FILE = data_paths.PENDING_IMPORT_FILE


def _load_pending_imports() -> dict[str, tuple[str, float, str]]:
    """Read pending card imports left over from a previous plugin process."""
    try:
        data = json.loads(PENDING_IMPORT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, tuple[str, float, str]] = {}
    for key, item in data.items():
        if isinstance(item, list) and len(item) in (2, 3):
            try:
                result[str(key)] = (
                    str(item[0]),
                    float(item[1]),
                    str(item[2]) if len(item) == 3 else "",
                )
            except (TypeError, ValueError):
                continue
    return result


@dataclass
class SearchPage:
    """One paged card search kept per sender."""

    query: str
    ids: list[str] = field(default_factory=list)
    page: int = 1
    expires_at: float = 0.0

    @property
    def pages(self) -> int:
        return max(1, (len(self.ids) + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE)


# session_id -> (card_id, expires_at, sender_id) for the "command then image" flow.
_pending_import: dict[str, tuple[str, float, str]] = _load_pending_imports()
# sender_id -> paged search results, advanced by sending `#`.
_search_pages: dict[str, SearchPage] = {}
# sender_id -> import id of the most recently imported/recognized card.
_last_import: dict[str, str] = {}
# session+sender -> DIY mode.  A mode stays active until `kards diy exit`.
_diy_mode: dict[str, dict] = {}
DIY_MODE_EXIT_ALIASES = {"exit", "quit", "退出", "退出diy", "结束", "bye", "bye diy"}
DIY_MODE_TIP = "已进入 DIY 模式，后续每条消息都修改并保存这张卡；单独发「退出」或 kards diy exit 退出"
# A wake prefix (`#`, `/`, `??`) may precede the command; anything else is art text.
_DIY_COMMAND_RE = re.compile(r"^\s*(?:[#/??]\s*)?kards\b", re.IGNORECASE)


def _diy_mode_key(event) -> str:
    """Key DIY mode by both session and sender so group users stay isolated."""
    try:
        session = str(event.get_session_id() or "")
    except Exception:
        session = ""
    try:
        sender = str(event.get_sender_id() or "")
    except Exception:
        sender = ""
    return f"{session}::{sender}" if session else sender


def _diy_next_id(designs: list) -> str:
    """Return the next unused DIY id, even if earlier cards were deleted."""
    used = {str(card.get("id", "")).strip().upper() for card in designs}
    index = 1
    while True:
        candidate = f"DIY-{index:02d}"
        if candidate not in used:
            return candidate
        index += 1


def _diy_save_card(entry: dict) -> None:
    """Upsert one DIY design into diy_cards.json, preserving the other cards."""
    designs = web_api._load_diy_cards()
    card_id = str(entry.get("id") or _diy_next_id(designs))
    saved = {k: v for k, v in entry.items() if k not in ("id", "art")}
    saved["id"] = card_id
    art = str(entry.get("art") or "")
    if art:
        saved["art"] = art
    designs = [
        card for card in designs
        if str(card.get("id", "")).strip().lower() != card_id.strip().lower()
    ]
    designs.append(saved)
    web_api.DIY_CARDS_FILE.write_text(
        json.dumps(designs, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _diy_enter_mode(event, card_id: str, spec: dict | None = None, art: str = "") -> None:
    """Bind one saved card to this sender/session for iterative editing."""
    _diy_mode[_diy_mode_key(event)] = {
        "id": str(card_id),
        "spec": dict(spec or {}),
        "art": str(art or ""),
        "started_at": time.time(),
    }


def _known_card_ids() -> set[str]:
    """Card art ids and short import ids currently present in the card table."""
    ids: set[str] = set()
    for import_id, row in deck_code.CARDS.items():
        short = str(import_id or "").strip().lower()
        if short:
            ids.add(short)
        if row:
            art_id = str(row[0] or "").strip().lower()
            if art_id:
                ids.add(art_id)
    return ids


def _save_pending_imports() -> None:
    """Write pending card imports atomically."""
    try:
        PENDING_IMPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = {
            key: [card_id, expires, sender]
            for key, (card_id, expires, sender) in _pending_import.items()
        }
        temp = PENDING_IMPORT_FILE.with_suffix(".tmp")
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(PENDING_IMPORT_FILE)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("Failed to save pending card imports: %s", exc)


def _arm_pending(session_key: str, card_id: str, sender: str) -> None:
    """Arm one session for card-image recognition for PENDING_IMPORT_TTL seconds."""
    key = str(session_key or "").strip() or str(sender or "").strip()
    if not key:
        return
    _pending_import[key] = (
        str(card_id or ""),
        time.time() + PENDING_IMPORT_TTL,
        str(sender or ""),
    )
    _save_pending_imports()


# sender_id -> {"temp_id": str, "cards": [entry, ...], "created_at": float}
# Recognized cards stay here until the user says 同意 / runs `kards confirm`.
TEMP_DRAFT_FILE = data_paths.TEMP_IMPORT_FILE


def _load_temp_drafts() -> dict[str, dict]:
    """Read card drafts waiting for the user's confirmation."""
    try:
        data = json.loads(TEMP_DRAFT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, dict] = {}
    for key, item in data.items():
        if isinstance(item, dict) and isinstance(item.get("cards"), list):
            result[str(key)] = item
    return result


_temp_drafts: dict[str, dict] = _load_temp_drafts()


def _save_temp_drafts() -> None:
    """Persist waiting card drafts atomically."""
    try:
        TEMP_DRAFT_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = TEMP_DRAFT_FILE.with_suffix(".tmp")
        temp.write_text(
            json.dumps(_temp_drafts, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(TEMP_DRAFT_FILE)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("Failed to save temporary card drafts: %s", exc)


def _new_temp_card_id() -> str:
    """Return an unused two-character base62 short code."""
    alphabet = string.ascii_letters + string.digits
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(2))
        if code not in deck_code.CARDS:
            return code


# ---------------------------------------------------------------------------
# Admin permission gate and review queue
#
# Operations that can change the shared card table (import / confirm / cancel /
# setid / fix) are restricted to administrators.  Requests from other senders
# are queued here instead of being applied, and an administrator approves or
# rejects them with `kards approve` / `kards reject`.
# ---------------------------------------------------------------------------


def _load_admin_ids() -> set[str]:
    """Read administrator sender ids from AstrBot's global configuration."""
    ids: set[str] = set()
    try:
        config_path = data_paths.DATA_DIR.parent.parent / "cmd_config.json"
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        raw = config.get("admins_id") or []
        if isinstance(raw, str):
            raw = [raw]
        for value in raw:
            if value:
                ids.add(str(value))
    except (OSError, ValueError, TypeError) as exc:
        logger.debug("Read admins_id failed: %s", exc)
    return ids


def _is_admin(event: AstrMessageEvent) -> bool:
    """Return True for AstrBot admins or configured administrator sender ids."""
    try:
        if event.is_admin():
            return True
    except Exception:
        pass
    return str(event.get_sender_id()) in _load_admin_ids()


def _load_admin_reviews() -> dict[str, dict]:
    try:
        data = json.loads(data_paths.ADMIN_REVIEW_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, dict)}


_admin_reviews: dict[str, dict] = _load_admin_reviews()


def _save_admin_reviews() -> None:
    try:
        data_paths.ADMIN_REVIEW_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = data_paths.ADMIN_REVIEW_FILE.with_suffix(".tmp")
        temp.write_text(
            json.dumps(_admin_reviews, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp.replace(data_paths.ADMIN_REVIEW_FILE)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("Failed to save admin review queue: %s", exc)


def _queue_admin_review(
    event, kind: str, detail: str = "", payload: dict | None = None
) -> str:
    """Queue a non-admin request and return its review id."""
    review_id = "RV-" + secrets.token_hex(3).upper()
    _admin_reviews[review_id] = {
        "id": review_id,
        "kind": kind,
        "sender": str(event.get_sender_id()),
        "session_id": str(event.get_session_id() or ""),
        "detail": str(detail or ""),
        "payload": payload or {},
        "created_at": time.time(),
        "status": "pending",
    }
    _save_admin_reviews()
    return review_id


def _require_admin(
    event, kind: str, detail: str = "", payload: dict | None = None
):
    """Return ``(allowed, review_id)``; queue a review when not allowed."""
    if _is_admin(event):
        return True, ""
    return False, _queue_admin_review(event, kind, detail, payload)


def _find_admin_review(review_id: str):
    """Look up a review id case-insensitively; return ``(key, entry)``."""
    review_id = str(review_id or "").strip()
    entry = _admin_reviews.get(review_id)
    if entry:
        return review_id, entry
    upper = review_id.upper()
    for key, item in _admin_reviews.items():
        if key.upper() == upper:
            return key, item
    return review_id, None


ADMIN_REVIEW_LABELS = {
    "import": "卡牌导入",
    "setid": "设置永久 ID",
    "fix": "修改卡牌字段",
    "confirm": "确认写入卡表",
    "cancel": "放弃卡牌草稿",
}


def _admin_review_notice(review_id: str, kind: str, detail: str = "") -> str:
    label = ADMIN_REVIEW_LABELS.get(kind, kind)
    shown = str(detail or "（无）").strip()
    if len(shown) > 200:
        shown = shown[:200] + "…"
    return (
        f"【管理员审核】{label}需要管理员权限，已提交审核\n"
        f"审核编号：{review_id}\n"
        f"提交内容：{shown}\n"
        f"管理员批准：kards approve {review_id}\n"
        f"管理员驳回：kards reject {review_id}\n"
        "查看待审：kards review"
    )


class _ReviewEvent:
    """Event shim used to replay an approved request with the original sender."""

    def __init__(self, event, sender: str, session_id: str = ""):
        self._event = event
        self._sender = str(sender or "")
        self._session_id = str(session_id or "")
        self.outputs: list[str] = []

    def is_admin(self):
        # An approved replay is executed with the administrator's authority.
        return True

    def get_sender_id(self):
        return self._sender

    def get_session_id(self):
        return self._session_id or self._event.get_session_id()

    def get_messages(self):
        return []

    def plain_result(self, text):
        self.outputs.append(str(text))
        return str(text)

    def chain_result(self, chain):
        self.outputs.append("（图片消息）")
        return None


DEFAULT_UID = "2127596945"
DEFAULT_MAX_ITEMS = 5
DEFAULT_CACHE_TTL = 300
DEFAULT_MAX_PICS = 9

NEWS_ALIASES = {"news", "dyn", "dynamic", "动态"}
NEWS_REFRESH_ALIASES = {"refresh", "刷新", "刷新动态", "更新动态"}
NEWS_LIST_ALIASES = {"动态列表", "newslist", "dynlist", "动态list"}
# Max number of dynamics the list command shows and `kards news n` accepts.
NEWS_LIST_LIMIT = 20
DECK_ALIASES = {
    "deck",
    "deckcode",
    "code",
    "卡组",
    "卡组代码",
    "卡组码",
}
MANUAL_SEARCH_ALIASES = {"s", "search", "find", "搜索", "查卡"}
MANUAL_ALIAS_ALIASES = {"alias", "aliases", "别名"}
MANUAL_IMPORT_ALIASES = {"import", "cardupdate", "导入卡牌", "更新卡牌"}
MANUAL_SETID_ALIASES = {"setid", "id", "permanentid", "永久id", "设置id", "设id"}
MANUAL_FIX_ALIASES = {"fix", "correct", "修正", "修改", "改卡", "纠错"}
MANUAL_DECK_ADD_ALIASES = {"add", "加卡", "加入卡组", "卡组加"}
MANUAL_DECK_REMOVE_ALIASES = {"del", "delete", "remove", "删卡", "移除卡", "卡组删"}
MANUAL_DECK_LIST_ALIASES = {"list", "ls", "卡组列表", "查看卡组", "卡组详情"}
MANUAL_DECK_CLEAR_ALIASES = {"clear", "清空", "清空卡组"}
MANUAL_DECK_SHOW_ALIASES = {"show", "render", "卡组图", "生成卡组图"}
MANUAL_DECK_MAIN_ALIASES = {"setmain", "主国", "设置主国"}
MANUAL_DECK_ALLY_ALIASES = {"setally", "盟国", "设置盟国"}
MANUAL_DIY_ALIASES = {"diy", "diycard", "diykards", "自制卡", "diy卡"}
MANUAL_CONFIRM_ALIASES = {"confirm", "确认导入", "同意导入"}
MANUAL_CANCEL_ALIASES = {"cancel", "取消导入"}
MANUAL_REVIEW_ALIASES = {"review", "reviews", "审核", "待审", "审核列表"}
MANUAL_APPROVE_ALIASES = {"approve", "批准", "通过", "审核通过"}
MANUAL_REJECT_ALIASES = {"reject", "驳回", "审核驳回"}
# Field definitions live in manual_deck so the chat command and web page share
# the same validation.
FIX_FIELDS = manual_deck.FIX_FIELDS
fix_field_key = manual_deck.fix_field_key

HELP_TEXT = (
    "KARDS B站助手\n"
    "kards news / kards dyn / kards 动态  查看 KARDS 官方最新动态\n"
    "kards news n                         只看倒数第 n 条动态（1-20）\n"
    "kards news list                      列出动态标题和序号\n"
    "kards now 关键词                     搜索 KARDS 相关视频\n"
    "kards deck 卡组码                    解析卡组码并生成卡图 + 信息图\n"
    "  也可用：kards 卡组代码 / kards code / kardsdeck 卡组码\n"
    "kards s 关键词                       搜索本地卡表（含别名/描述/标签/属性）\n"
    "kards alias 卡名 ～ 别名            给卡牌加别名\n"
    "kards alias list                     查看别名库\n"
    "kards alias del 别名                 删除别名\n"
    "【管理员权限】import / confirm / cancel / fix / setid 仅管理员可直接执行；\n"
    "  其他用户提交后进入审核队列，由管理员批准或驳回\n"
    "kards import                         生成两位临时短码，发卡图后先出草稿\n"
    "kards import 卡牌ID/JSON/文件名      按指定 ID 导入，或发完整卡图自动识别\n"
    "kards confirm / 回复“同意”           把草稿同步到正式卡表（确认后才写入）\n"
    "kards cancel                         放弃待确认的卡牌草稿\n"
    "kards fix <字段> <新值>              修改刚导入卡牌的识别错误\n"
    "kards fix <卡牌> <字段> <新值>       修改指定卡牌的字段\n"
    "kards setid 临时ID/卡名 永久ID       设置新卡永久 ID（2 位字母/数字）\n"
    "kards review                         管理员查看待审核请求\n"
    "kards approve <审核编号>             管理员批准请求（也可 kards reject 驳回）\n"
    "kards add 卡名 [x2]                  手动卡组加卡（衍生牌不可加入）\n"
    "kards del 卡名 [x2]                  手动卡组删卡\n"
    "kards list / kards show              查看手动卡组文字 / 图片\n"
    "kards clear                          清空手动卡组\n"
    "kards setmain 国家 / setally 国家    设置手动卡组主国 / 盟国\n"
    "kards diy                           新建一张卡并进入 DIY 模式（每次修改自动保存）\n"
    "kards diy new                        强制新建一张卡（不用上次的）\n"
    "kards diy last                       继续编辑最近保存的 DIY 卡\n"
    "kards diy 字段=值 ...                新建一张卡，保存并渲染，随后进入模式\n"
    "kards diy save 字段=值 ...           保存并渲染 DIY 卡\n"
    "kards diy show <ID>                  渲染并继续编辑已保存的 DIY 卡\n"
    "kards diy load <卡名/ID>             读取本地卡表 JSON 与卡图，作为新 DIY 卡继续编辑\n"
    "kards diy exit                       退出 DIY 模式；模式内也可单独发「退出」「exit」「结束」\n"
    "kards 刷新动态                       跳过缓存，重新拉取官方动态\n"
    "卡牌筛选与字段编辑也可在插件页面「手动卡组」操作"
)

DIY_HELP_TEXT = (
    "KARDS DIY 卡编辑器（一次进模式只编一张卡，每次修改自动保存）\n"
    "kards diy                              新建一张卡并进入模式\n"
    "kards diy new                          强制新建一张卡\n"
    "kards diy last                         继续编辑最近保存的卡\n"
    "kards diy exit                         退出模式（卡片已保存）\n"
    "模式内单独发 退出 / exit / 结束 / quit / bye 也能退出并保存\n"
    "kards diy help                         查看本帮助\n"
    "kards diy list                         列出已保存的 DIY 卡\n"
    "kards diy show <ID>                    渲染并继续编辑这张已保存的卡\n"
    "kards diy load <卡名/ID>               读取本地卡表 JSON 与卡图，存为新 DIY 卡并进入模式\n"
    "kards diy save 字段=值 ...             保存并渲染\n"
    "kards diy 字段=值 ...                  新建一张卡，保存并渲染后进入模式\n"
    "可带卡图：同一条消息附图，或先发命令再发图\n"
    "字段：名/卡名、英文名、阵营、类型、稀有度、费、攻、防、描述、关键词、id\n"
    "插画：插画=x，y~z（左移 x、上移 y、缩放 z，负数反向）\n"
    "  也可只写 插画=x，y 或 插画=~z；独立字段：插画x/左移、插画y/上移、缩放/放缩\n"
    "例：kards diy 名=闪电部队 阵营=德国 类型=坦克 稀有度=精英 费=3 攻=4 防=2 描述=闪击，部署时抽一张牌\n"
    "也可用 JSON：kards diy {\"name\":\"测试卡\",\"faction\":\"USA\",\"cost\":2}\n"
    "完整编辑请在插件页面打开「diy_editor」"
)

DIY_FIELD_ALIASES = {
    "name": "name", "名": "name", "卡名": "name", "名称": "name", "名称中文": "name",
    "name_en": "name_en", "英文名": "name_en", "英文": "name_en",
    "faction": "faction", "阵营": "faction", "国家": "faction",
    "card_type": "card_type", "type": "card_type", "类型": "card_type", "卡类": "card_type",
    "rarity": "rarity", "稀有度": "rarity",
    "cost": "cost", "费": "cost", "费用": "cost",
    "attack": "attack", "攻": "attack", "攻击": "attack",
    "defense": "defense", "防": "defense", "防御": "defense",
    "description": "description", "描述": "description", "效果": "description", "文本": "description",
    "keywords": "keywords", "关键词": "keywords", "词条": "keywords",
    "id": "id", "编号": "id",
    # Art placement: positive x = left, positive y = up; negatives reverse.
    "art_x": "art_x", "插画x": "art_x", "x": "art_x", "左移": "art_x",
    "art_y": "art_y", "插画y": "art_y", "y": "art_y", "上移": "art_y",
    "art_scale": "art_scale", "缩放": "art_scale", "放缩": "art_scale",
    "插画缩放": "art_scale", "zoom": "art_scale",
    # Compact form: 插画=x，y~z / 插画=x，y / 插画=~z
    "art_pos": "art_pos", "artpos": "art_pos", "artxy": "art_pos",
    "artoffset": "art_pos", "插画": "art_pos", "插画位置": "art_pos",
    "位移": "art_pos", "位置": "art_pos",
}


# Compact art placement: "x，y", "~z" or "x，y~z".  Coordinates may be
# negative; the tilde part is the zoom multiplier (z).
_ART_COMPACT_RE = re.compile(
    r"^\s*(?:([+-]?\d+(?:\.\d+)?)\s*[,，\s]+\s*([+-]?\d+(?:\.\d+)?))?"
    r"(?:\s*~\s*([+-]?\d+(?:\.\d+)?))?\s*$"
)


def _diy_parse_art_pos(value):
    """Expand the compact art placement form into the three numeric fields."""
    text = str(value or "").strip()
    match = _ART_COMPACT_RE.match(text)
    if not match or not any(match.groups()):
        return {}, "插画格式应为 插画=x，y 或 插画=~z 或 插画=x，y~z"
    x, y, z = match.groups()
    if (x is None) != (y is None):
        return {}, "插画坐标必须成对写 x，y"
    result: dict = {}
    if x is not None and y is not None:
        result["art_x"] = float(x)
        result["art_y"] = float(y)
    if z is not None:
        result["art_scale"] = float(z)
    return result, ""


# Common synonyms that are not in deck_code.TYPE_CN.  Without these an
# unknown value falls through to the countermeasure template by accident.
_TYPE_VALUE_ALIASES = {
    "炮兵": "artillery", "炮": "artillery", "火炮": "artillery",
    "火箭炮": "artillery", "自行火炮": "artillery",
    "反坦克炮": "artillery", "榴弹炮": "artillery",
    "歼击机": "fighter", "战斗机": "fighter", "战机": "fighter",
    "轰炸": "bomber", "反制牌": "countermeasure", "指令牌": "order",
}


def _diy_enum_value(value, cn_map, aliases=None):
    """Return the canonical English key for a Chinese label or English key."""
    text = str(value or "").strip()
    if text in cn_map:
        return text
    for key, cn in cn_map.items():
        if text == cn:
            return key
    if aliases and text in aliases:
        return aliases[text]
    return text


def _diy_parse_fields(text):
    """Parse ``kards diy`` arguments into a render spec.

    Supports JSON objects and ``字段=值`` pairs.  Separators are tolerant:
    ``=``, fullwidth ``＝``, ASCII ``:`` and Chinese ``：`` all work, with
    optional spaces around them.  Quoted values may contain spaces; unquoted
    values run until the next recognised field or end of input.
    """
    raw = (text or "").strip()
    if not raw:
        return {}, ""
    if raw.startswith("{"):
        try:
            spec = json.loads(raw)
        except ValueError as exc:
            return {}, "DIY JSON 解析失败：" + str(exc)
        if not isinstance(spec, dict):
            return {}, "DIY JSON 必须是对象"
        return dict(spec), ""

    aliases = sorted(DIY_FIELD_ALIASES, key=len, reverse=True)
    alias_re = "|".join(re.escape(item) for item in aliases)
    pair_re = re.compile(
        r"(?P<key>[^\s=＝:：]+)\s*[=＝:：]\s*"
        r"(?P<value>\"[^\"]*\"|'[^']*'|.*?(?=\s+(?:"
        + alias_re
        + r")\s*[=＝:：]|$))",
        re.IGNORECASE,
    )
    pairs = [(m.group("key"), m.group("value")) for m in pair_re.finditer(raw)]
    if not pairs:
        return {}, "没识别到字段，发送 kards diy 查看用法"
    spec: dict = {}
    for raw_key, raw_value in pairs:
        key = DIY_FIELD_ALIASES.get(str(raw_key).lower(), str(raw_key).lower())
        value = raw_value.strip().strip('"').strip("'")
        if key == "art_pos":
            expanded, pos_error = _diy_parse_art_pos(value)
            if pos_error:
                return {}, pos_error
            spec.update(expanded)
            continue
        if key == "faction":
            value = _diy_enum_value(value, deck_code.FACTION_CN)
        elif key == "card_type":
            value = _diy_enum_value(value, deck_code.TYPE_CN, _TYPE_VALUE_ALIASES)
        elif key == "rarity":
            value = _diy_enum_value(value, deck_code.RARITY_CN)
        elif key in ("cost", "attack", "defense"):
            try:
                value = int(value)
            except ValueError:
                pass
        elif key in ("art_x", "art_y", "art_scale"):
            try:
                value = float(value)
            except ValueError:
                pass
        elif key == "keywords":
            value = [item for item in re.split(r"[,，、\s]+", value) if item]
        spec[key] = value
    return spec, ""


def _diy_art_image(art):
    """Decode a data URL or raw base64 string into a PIL image."""
    text = str(art or "").strip()
    if not text:
        return None
    try:
        raw = text.split(",", 1)[1] if text.startswith("data:") else text
        return diy_card.art_from_bytes(base64.b64decode(raw))
    except Exception:
        return None


def fmt_num(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "-"
    if n >= 100000000:
        return "%.1f亿" % (n / 1e8)
    if n >= 10000:
        return "%.1f万" % (n / 1e4)
    return str(n)


def plain(s):
    return str(s).replace('<em class="keyword">', "").replace("</em>", "")


def _int_option(config, key, default, minimum=1):
    """Read a positive integer option from the plugin config."""
    try:
        value = config.get(key, default) if config else default
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _chain_with_pics(text, paths):
    """把一段文字和若干本地图片拼成一条消息链。"""
    chain = []
    if text:
        chain.append(Plain(text=text))
    for path in paths:
        try:
            chain.append(Image.fromFileSystem(path))
        except Exception:
            continue
    return chain


def _dynamic_text(index, item):
    """Build one text block for a dynamic: heading, title, body and link.

    Args:
        index: Display number of the dynamic.
        item: Parsed dynamic dictionary.

    Returns:
        A ``(heading, body)`` tuple ready for a single message chain.
    """
    head = f"{index}. 【{item.get('type_label') or '动态'}】"
    if item.get("pub_time"):
        head += " " + item["pub_time"]
    title = (item.get("title") or "").strip()
    body = (item.get("body") or "").strip()
    # B 站 opus 的正文常以标题开头，去掉重复，避免标题出现两次
    if title and body.startswith(title):
        body = body[len(title) :].lstrip()
    paras = dr.split_paragraphs(body)
    if title:
        paras = [title] + paras
    url = (item.get("url") or "").strip()
    if url:
        paras.append("链接: " + url)
    return head, "\n".join(paras)


@register(
    "bilikards",
    "Fma",
    "KARDS B站动态、视频查询与卡组代码图片解析",
    "v2.0.0",
)
class BiliKards(Star):
    def __init__(self, context, config=None):
        super().__init__(context, config)
        self.config = config or {}
        self.sess = None
        self.dyn = df.DynamicClient(
            uid=self.config.get("kards_uid") or DEFAULT_UID,
            cache_ttl=_int_option(
                self.config, "cache_ttl", DEFAULT_CACHE_TTL, minimum=0
            ),
            proxy=self.config.get("bili_proxy") or None,
            cookie=self.config.get("bili_cookie") or None,
        )
        self.images = ImageCache(
            max_images=_int_option(
                self.config, "max_pics_per_item", DEFAULT_MAX_PICS, minimum=0
            ),
        )
        try:
            web_api.register(self.context, self.config)
        except Exception as exc:
            self.logger.warning("Manual deck Web API registration failed: %s", exc)

    def _send_pics(self):
        value = self.config.get("send_pics", True) if self.config else True
        return value not in (False, 0, "false", "False", "0", "")

    async def sess_get(self):
        if self.sess is None or self.sess.closed:
            self.sess = aiohttp.ClientSession(headers=bf.HEAD)
        return self.sess

    def _deck_user_key(self, event):
        """Return the per-user deck key for a chat event."""
        try:
            platform = event.get_platform_id() or event.get_platform_name()
        except Exception:
            platform = ""
        try:
            sender = event.get_sender_id()
        except Exception:
            sender = ""
        return manual_deck.chat_user_key(platform, sender)

    def _card_label(self, import_id):
        """Render a compact one-line label for a card id."""
        info = deck_code.CARDS.get(import_id)
        if not info:
            return import_id
        name = info[1] or info[2] or import_id
        cost = f"{info[6]}K" if info[6] is not None else "?K"
        faction = deck_code.FACTION_CN.get(info[3], info[3])
        return f"{name}（{cost} · {faction}）"

    def _parse_card_arg(self, text):
        """Parse ``卡名`` or ``卡名 x2`` into a name and copy count."""
        raw = (text or "").strip()
        match = re.match(r"^(.*?)(?:\s*[x×*]\s*(\d+))?$", raw)
        name = match.group(1).strip() if match else raw
        try:
            count = int(match.group(2)) if match and match.group(2) else 1
        except (TypeError, ValueError):
            count = 1
        return name, max(1, count)

    async def _manual_add(self, event: AstrMessageEvent, kw):
        name, count = self._parse_card_arg(kw)
        if not name:
            yield event.plain_result("用法：kards add 卡名 [x2]")
            return
        matches = manual_deck.find_cards(name)
        if not matches:
            yield event.plain_result("没找到卡牌：" + name)
            return
        if len(matches) > 1:
            lines = ["找到多张卡牌，请写得更具体："]
            lines.extend("- " + self._card_label(item) for item in matches)
            yield event.plain_result("\n".join(lines))
            return

        import_id = matches[0]
        key = self._deck_user_key(event)
        current = manual_deck.load_deck(key)["cards"].get(import_id, 0)
        try:
            deck = manual_deck.add_to_deck(import_id, count, key)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return
        new_count = deck["cards"][import_id]
        if new_count <= current:
            yield event.plain_result(
                f"{self._card_label(import_id)} 已经带满 {manual_deck.MAX_COPIES} 张"
            )
            return
        total = sum(deck["cards"].values())
        yield event.plain_result(
            f"已添加 {new_count - current}x {self._card_label(import_id)}\n"
            f"当前 {new_count} 张 · 卡组共 {total} 张"
        )

    async def _manual_remove(self, event: AstrMessageEvent, kw):
        name, count = self._parse_card_arg(kw)
        if not name:
            yield event.plain_result("用法：kards del 卡名 [x2]")
            return
        matches = manual_deck.find_cards(name)
        if not matches:
            yield event.plain_result("没找到卡牌：" + name)
            return
        if len(matches) > 1:
            lines = ["找到多张卡牌，请写得更具体："]
            lines.extend("- " + self._card_label(item) for item in matches)
            yield event.plain_result("\n".join(lines))
            return

        import_id = matches[0]
        key = self._deck_user_key(event)
        deck = manual_deck.load_deck(key)
        current = deck["cards"].get(import_id, 0)
        if not current:
            yield event.plain_result(f"{self._card_label(import_id)} 不在手动卡组里")
            return
        remaining = max(0, current - count)
        if remaining:
            deck["cards"][import_id] = remaining
        else:
            deck["cards"].pop(import_id, None)
        manual_deck.save_deck(deck, key)
        total = sum(deck["cards"].values())
        yield event.plain_result(
            f"已移除 {current - remaining}x {self._card_label(import_id)}\n"
            f"卡组共 {total} 张"
        )

    async def _manual_list(self, event: AstrMessageEvent):
        deck = manual_deck.load_deck(self._deck_user_key(event))
        if not deck["cards"]:
            yield event.plain_result(
                "手动卡组是空的。\n"
                "用 kards add 卡名 添加，或打开插件页面「手动卡组」编辑。"
            )
            return
        decoded = manual_deck.build_decoded(deck)
        main_cn = deck_code.FACTION_CN.get(decoded.main_faction, decoded.main_faction)
        ally_cn = (
            deck_code.FACTION_CN.get(decoded.ally_faction, decoded.ally_faction)
            if decoded.ally_faction
            else "无"
        )
        yield event.plain_result(
            f"手动卡组 · {main_cn}/{ally_cn} · "
            f"{decoded.total} 张（含 HQ {decoded.total + deck_code.HQ_COUNT} 张）\n\n"
            + deck_code.format_deck(decoded)
        )

    async def _manual_clear(self, event: AstrMessageEvent):
        key = self._deck_user_key(event)
        deck = manual_deck.load_deck(key)
        deck["cards"] = {}
        manual_deck.save_deck(deck, key)
        yield event.plain_result("手动卡组已清空。")

    async def _manual_show(self, event: AstrMessageEvent):
        deck = manual_deck.load_deck(self._deck_user_key(event))
        try:
            decoded = manual_deck.build_decoded(deck)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return
        try:
            cards_path, info_path = await deck_image.render_deck_images(decoded)
        except Exception as exc:
            self.logger.warning("Manual deck image render failed: %s", exc)
            yield event.plain_result(deck_code.format_deck(decoded))
            return

        main_cn = deck_code.FACTION_CN.get(decoded.main_faction, decoded.main_faction)
        ally_cn = (
            deck_code.FACTION_CN.get(decoded.ally_faction, decoded.ally_faction)
            if decoded.ally_faction
            else "无"
        )
        caption = (
            f"手动卡组 · {main_cn}/{ally_cn} · "
            f"{decoded.total} 张（含 HQ {decoded.total + deck_code.HQ_COUNT} 张）"
            " · 卡图 + 信息图"
        )
        yield event.chain_result(
            _chain_with_pics(caption, [str(cards_path), str(info_path)])
        )

    def _search_items(self, ids: list[str]) -> list[dict]:
        """Build render items for a slice of search result ids."""
        items: list[dict] = []
        for import_id in ids:
            info = deck_code.CARDS.get(import_id)
            if not info:
                continue
            items.append(
                {
                    "id": import_id,
                    "card_id": info[0],
                    "name": info[1] or info[2] or import_id,
                    "faction": info[3],
                    "cost": info[6],
                }
            )
        return items

    async def _send_search_page(self, event: AstrMessageEvent, state: SearchPage):
        """Send one page of a paged card search."""
        start = (state.page - 1) * SEARCH_PAGE_SIZE
        ids = state.ids[start : start + SEARCH_PAGE_SIZE]
        lines = [
            f"搜索「{state.query}」：第 {state.page}/{state.pages} 页，"
            f"共 {len(state.ids)} 张"
        ]
        lines.extend(
            f"{number}. " + self._card_label(import_id)
            for number, import_id in enumerate(ids, 1)
        )
        if state.page < state.pages:
            lines.append("发送 # 查看下一页")
        else:
            lines.append("已经是最后一页，重新搜索可换关键词")
        text = "\n".join(lines)
        if not self._send_pics():
            yield event.plain_result(text)
            return

        try:
            path = await asyncio.to_thread(
                deck_image.render_search_grid,
                self._search_items(ids),
                state.query,
                SEARCH_PAGE_SIZE,
                state.page,
                len(state.ids),
            )
        except Exception as exc:
            self.logger.warning("Search grid render failed: %s", exc)
            yield event.plain_result(text)
            return

        self.logger.info(
            "Search grid: %s (%d bytes)", path, path.stat().st_size
        )
        # QQ Official C2C 在同一条消息里混排文字和图片时会走富媒体 msg_type=7，
        # 图片可能不显示。文字和卡图分两条发，图片单独发更稳。
        yield event.plain_result(text)
        yield event.chain_result([Image.fromFileSystem(str(path))])

    async def _manual_search(self, event: AstrMessageEvent, kw):
        name = (kw or "").strip()
        if not name:
            yield event.plain_result(
                "用法：kards s 关键词（也可用 search/搜索）\n"
                "支持空格分隔多条件（如：德国 坦克 6K2/5/4 闪击）\n"
                "费用/行动可用 6K 或 6K2，攻防可用 5/4 或 攻5 防4\n"
                f"结果超过 {SEARCH_PAGE_SIZE} 张时会自动分页，发送 # 翻页"
            )
            return
        matches = manual_deck.search_cards(name, limit=0)
        if not matches:
            matches = manual_deck.find_cards(name, limit=0, deep=True)
        if not matches:
            _search_pages.pop(str(event.get_sender_id()), None)
            yield event.plain_result("没找到卡牌：" + name)
            return

        state = SearchPage(
            query=name,
            ids=matches,
            page=1,
            expires_at=time.time() + SEARCH_PAGE_TTL,
        )
        _search_pages[str(event.get_sender_id())] = state
        if len(matches) == 1:
            async for result in self._send_single_card(event, state, 1, sole=True):
                yield result
            return
        async for result in self._send_search_page(event, state):
            yield result

    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE | filter.EventMessageType.GROUP_MESSAGE
    )
    async def search_next_page(self, event: AstrMessageEvent):
        """Advance a paged card search when the user sends ``#``."""
        raw = str(getattr(event.message_obj, "message_str", "") or "").strip()
        # `#` is also an AstrBot wake prefix, so it is removed from the
        # parsed text.  Read the original raw text to recognise it.
        for prefix in ("#", "／", "/"):
            if raw.startswith(prefix):
                raw = raw[len(prefix) :].strip()
        if raw not in {"#", "下一页", "next"}:
            return

        key = str(event.get_sender_id())
        state = _search_pages.get(key)
        if not state:
            return
        if time.time() > state.expires_at:
            _search_pages.pop(key, None)
            yield event.plain_result("搜索结果已过期，请重新发送 kards s 关键词")
            return
        if state.page >= state.pages:
            yield event.plain_result("已经是最后一页，重新搜索可换关键词")
            return
        state.page += 1
        async for result in self._send_search_page(event, state):
            yield result

    async def _send_single_card(
        self, event: AstrMessageEvent, state: SearchPage, number: int, sole: bool = False
    ):
        """Send one card's art and its full JSON metadata."""
        total = len(state.ids)
        start = (state.page - 1) * SEARCH_PAGE_SIZE
        page_ids = state.ids[start : start + SEARCH_PAGE_SIZE]
        if sole and total == 1:
            import_id = state.ids[0]
            scope = "唯一命中"
        elif 1 <= number <= len(page_ids):
            import_id = page_ids[number - 1]
            scope = f"本页第 {number} 个"
        elif 1 <= number <= total:
            import_id = state.ids[number - 1]
            scope = f"全部结果第 {number} 个"
        else:
            yield event.plain_result(
                f"序号超出范围：搜索「{state.query}」共 {total} 张，本页 {len(page_ids)} 张"
            )
            return

        info = deck_code.CARDS.get(import_id)
        if not info:
            yield event.plain_result(f"未找到卡牌：{import_id}")
            return

        yield event.plain_result(
            f"搜索「{state.query}」{scope}：{self._card_label(import_id)}"
        )

        if self._send_pics():
            try:
                path = await asyncio.to_thread(
                    deck_image.render_single_card,
                    self._search_items([import_id])[0],
                    state.query,
                    number,
                    total,
                )
                yield event.chain_result([Image.fromFileSystem(str(path))])
            except Exception as exc:
                self.logger.warning("Single card render failed: %s", exc)
                yield event.plain_result("单卡图渲染失败，仅发送 JSON 信息")

        catalog = next(
            (
                card
                for card in manual_deck.card_catalog()
                if card.get("id") == import_id
            ),
            None,
        )
        if catalog is None:
            catalog = {
                "id": import_id,
                "card_id": info[0],
                "name": info[1] or info[2] or import_id,
                "name_en": info[2] or "",
                "faction": deck_code.FACTION_CN.get(info[3], info[3]),
                "type": deck_code.TYPE_CN.get(info[4], info[4]),
                "rarity": deck_code.RARITY_CN.get(info[5], info[5]),
                "cost": info[6],
                "operation": info[7],
                "attack": info[8],
                "defense": info[9],
            }
        brief = {
            "稀有度": catalog.get("rarity_cn") or catalog.get("rarity") or "",
            "ID": catalog.get("id") or import_id,
            "国家": catalog.get("faction_cn") or catalog.get("faction") or "",
        }
        yield event.plain_result(json.dumps(brief, ensure_ascii=False, indent=2))

    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE | filter.EventMessageType.GROUP_MESSAGE
    )
    async def search_pick_card(self, event: AstrMessageEvent):
        """Send a single card image plus JSON when the user types `#第N个`."""
        raw = str(getattr(event.message_obj, "message_str", "") or "").strip()
        # Strip wake prefixes in any order: `#第1个`, `##第1个`, `/#第1个`.
        while True:
            stripped = False
            for prefix in ("#", "／", "/"):
                if raw.startswith(prefix):
                    raw = raw[len(prefix) :].strip()
                    stripped = True
            if not stripped:
                break
        match = re.fullmatch(r"第\s*(\d+)\s*个", raw)
        if not match:
            return

        sender = str(event.get_sender_id())
        state = _search_pages.get(sender)
        if not state:
            return
        if time.time() > state.expires_at:
            _search_pages.pop(sender, None)
            yield event.plain_result("搜索结果已过期，请重新发送 kards s 关键词")
            return
        async for result in self._send_single_card(event, state, int(match.group(1))):
            yield result

    async def _manual_alias(self, event: AstrMessageEvent, kw):
        text = (kw or "").strip()
        if not text or text.lower() in {"help", "帮助", "?"}:
            yield event.plain_result(
                "别名用法：\n"
                "kards alias 卡名 ～ 别名   添加别名\n"
                "kards alias list           查看别名库\n"
                "kards alias del 别名       删除别名\n"
                "也可以直接在插件页面「手动卡组」里编辑。"
            )
            return

        lowered = text.lower()
        if lowered in {"list", "ls", "列表"}:
            aliases = manual_deck.load_aliases()
            if not aliases:
                yield event.plain_result(
                    "别名库还是空的。\n"
                    "用 kards alias 卡名 ～ 别名 添加，或在插件页面编辑。"
                )
                return
            items = sorted(
                aliases.items(),
                key=lambda item: (
                    (deck_code.CARDS.get(item[0]) or ["", item[0]])[1] or item[0]
                ),
            )
            lines = [f"别名库（{sum(len(v) for v in aliases.values())} 条）："]
            for import_id, values in items[:30]:
                info = deck_code.CARDS.get(import_id)
                name = (info[1] or info[2] or import_id) if info else import_id
                lines.append(f"- {name}：{'、'.join(values)}")
            if len(items) > 30:
                lines.append(f"……还有 {len(items) - 30} 张卡，请在插件页面查看")
            yield event.plain_result("\n".join(lines))
            return

        for prefix in ("del ", "delete ", "删除 "):
            if lowered.startswith(prefix):
                alias = text[len(prefix) :].strip()
                ok, message = manual_deck.remove_alias("", alias)
                yield event.plain_result(
                    message if ok else f"{message}：{alias or '（空）'}"
                )
                return

        parts = re.split(r"\s*[～~]\s*", text, maxsplit=1)
        if len(parts) != 2:
            yield event.plain_result("用法：kards alias 卡名 ～ 别名")
            return
        left, right = parts[0].strip(), parts[1].strip()
        if not left or not right:
            yield event.plain_result("用法：kards alias 卡名 ～ 别名")
            return

        left_matches = manual_deck.find_cards(left, limit=10)
        right_matches = manual_deck.find_cards(right, limit=10)
        if len(left_matches) == 1:
            import_id, alias = left_matches[0], right
        elif len(right_matches) == 1:
            import_id, alias = right_matches[0], left
        else:
            candidates = left_matches or right_matches
            if candidates:
                lines = ["找到多张卡牌，请写得更具体："]
                lines.extend("- " + self._card_label(item) for item in candidates)
                yield event.plain_result("\n".join(lines))
            else:
                missing = left if not left_matches else right
                yield event.plain_result("没找到卡牌：" + missing)
            return

        ok, message = manual_deck.add_alias(import_id, alias)
        if not ok:
            yield event.plain_result(message)
            return
        yield event.plain_result(f"{message}：{self._card_label(import_id)} ～ {alias}")

    async def _manual_set_id(self, event: AstrMessageEvent, kw):
        text = (kw or "").strip()
        if not text or text.lower() in {"help", "帮助", "?"}:
            yield event.plain_result(
                "用法：kards setid <临时ID或卡名> <永久ID>\n"
                "示例：kards setid DIY-Ab12Cd Xy\n"
                "说明：新导入卡牌默认使用 DIY- 前缀临时 ID，永久 ID 需为 2 位字母/数字。"
            )
            return
        parts = text.rsplit(None, 1)
        if len(parts) != 2:
            yield event.plain_result("用法：kards setid <临时ID或卡名> <永久ID>")
            return
        allowed, review_id = _require_admin(event, "setid", text)
        if not allowed:
            yield event.plain_result(_admin_review_notice(review_id, "setid", text))
            return
        target, new_id = parts[0].strip(), parts[1].strip()
        sender = str(event.get_sender_id())
        draft = _temp_drafts.get(sender)
        if draft and target == draft.get("temp_id") and target not in deck_code.CARDS:
            if not manual_deck._is_valid_permanent_import_id(new_id):
                yield event.plain_result("永久 ID 必须是 2 位字母或数字")
                return
            for entry in draft.get("cards", []):
                if entry.get("importId") == target:
                    entry["importId"] = new_id
                if entry.get("cardId") == target:
                    entry["cardId"] = new_id
            draft["temp_id"] = new_id
            _save_temp_drafts()
            yield event.plain_result(
                f"已将待确认卡牌短码 {target} 改为 {new_id}；"
                "回复“同意”或发送 kards confirm 后同步到正式卡表。"
            )
            return
        try:
            result = manual_deck.set_permanent_id(target, new_id)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return
        except Exception as exc:
            self.logger.warning("Set permanent card id failed: %s", exc)
            yield event.plain_result("设置永久 ID 失败，请查看 AstrBot 日志")
            return
        yield event.plain_result(
            f"已设置永久 ID：{result['name']}\n{result['old_id']} → {result['new_id']}"
        )

    def _fix_card_id(self, ref):
        """Resolve a card reference to one import id.

        Args:
            ref: Import id, card id, Chinese/English name or alias.

        Returns:
            A ``(import_id, candidates)`` tuple. ``import_id`` is empty when
            the reference is ambiguous or unknown; ``candidates`` holds close
            matches so the caller can ask the user to be more specific.
        """
        text = str(ref or "").strip()
        if not text:
            return "", []
        if text in deck_code.CARDS:
            return text, []
        matches = manual_deck.find_cards(text, limit=2, deep=True)
        if len(matches) == 1:
            return matches[0], []
        return "", matches

    async def _manual_fix(self, event: AstrMessageEvent, kw):
        """Correct one field of an imported card, or list its current fields."""
        text = (kw or "").strip()
        fields = "、".join(item[1] for item in FIX_FIELDS.values())
        if not text or text.lower() in {"help", "帮助", "?"}:
            yield event.plain_result(
                "用法：\n"
                "kards fix <字段> <新值>         修改刚导入的卡牌\n"
                "kards fix <卡牌> <字段> <新值>  修改指定卡牌\n"
                "kards fix <卡牌>                查看卡牌当前字段\n"
                "例：kards fix 攻击 6 / kards fix 费用 5 / kards fix 虎E 稀有度 限定\n"
                f"可用字段：{fields}"
            )
            return

        tokens = text.split(None, 2)
        if fix_field_key(tokens[0]):
            ref = ""
            field_tok = tokens[0]
            value = " ".join(tokens[1:]).strip()
        else:
            ref = tokens[0]
            if len(tokens) < 2:
                import_id, candidates = self._fix_card_id(ref)
                if not import_id:
                    if candidates:
                        lines = ["找到多张卡牌，请写得更具体："]
                        lines.extend(
                            "- " + self._card_label(item) for item in candidates
                        )
                        yield event.plain_result("\n".join(lines))
                    else:
                        yield event.plain_result("没找到卡牌：" + ref)
                    return
                row = deck_code.CARDS.get(import_id) or []
                lines = [f"卡牌：{(row[1] or row[2]) if len(row) > 2 else import_id}"]
                for _, (_, label, index, _) in FIX_FIELDS.items():
                    shown = row[index] if len(row) > index else ""
                    if isinstance(shown, list):
                        shown = "、".join(str(item) for item in shown)
                    lines.append(f"- {label}：{shown}")
                yield event.plain_result("\n".join(lines))
                return
            field_tok = tokens[1]
            value = tokens[2].strip() if len(tokens) > 2 else ""

        field = fix_field_key(field_tok)
        if not field:
            yield event.plain_result(f"未知字段：{field_tok}\n可用字段：{fields}")
            return
        if not value:
            yield event.plain_result("请填写新值。")
            return

        allowed, review_id = _require_admin(event, "fix", text)
        if not allowed:
            yield event.plain_result(_admin_review_notice(review_id, "fix", text))
            return

        if not ref:
            ref = _last_import.get(str(event.get_sender_id()), "")
            if not ref:
                yield event.plain_result(
                    "还没有最近导入的卡牌，请用 kards fix <卡牌> <字段> <新值>"
                )
                return
        import_id, candidates = self._fix_card_id(ref)
        if not import_id:
            if candidates:
                lines = ["找到多张卡牌，请写得更具体："]
                lines.extend("- " + self._card_label(item) for item in candidates)
                yield event.plain_result("\n".join(lines))
            else:
                yield event.plain_result("没找到卡牌：" + ref)
            return

        try:
            result = manual_deck.apply_card_fix(import_id, field, value)
        except ValueError as exc:
            yield event.plain_result("修改失败：" + str(exc))
            return
        except Exception as exc:
            self.logger.warning("Card fix failed: %s", exc)
            yield event.plain_result("修改失败，请查看 AstrBot 日志")
            return

        summary = result["summary"]
        yield event.plain_result(
            f"已修改 {result['name']} 的{result['label']}：{result['value']}\n"
            f"更新 {summary['updated']} · 共 {summary['total']} 张\n"
            f"继续修正可发：kards fix <字段> <新值>"
        )

    async def _manual_import(self, event: AstrMessageEvent, kw):
        text = (kw or "").strip()
        allowed, review_id = _require_admin(event, "import", text)
        if not allowed:
            images = [item for item in event.get_messages() if isinstance(item, Image)]
            if images:
                try:
                    image_path = await images[0].convert_to_file_path()
                    image_data = Path(image_path).read_bytes()
                    if (
                        text
                        and not text.startswith(("[", "{"))
                        and re.fullmatch(r"[0-9A-Za-z_\-]+", text)
                    ):
                        review_card_id = text
                    else:
                        review_card_id = _new_temp_card_id()
                    recognized = await web_api.recognize_card(
                        image_data, review_card_id, commit=False
                    )
                    entries = recognized.get("draft_entries") or []
                    review = _admin_reviews.get(review_id) or {}
                    review.setdefault("payload", {})["preapproved_cards"] = entries
                    review.setdefault("payload", {})["note"] = (
                        f"附图识别 {len(entries)} 张，批准后直接写入卡表"
                    )
                    _admin_reviews[review_id] = review
                    _save_admin_reviews()
                except Exception as exc:
                    self.logger.warning("Review image recognition failed: %s", exc)
            yield event.plain_result(_admin_review_notice(review_id, "import", text))
            return
        # `kards import <card_id>` arms this session for one minute.  Every image
        # from the same sender in the same session during that window is imported.
        if (
            text
            and not text.startswith(("[", "{"))
            and re.fullmatch(r"[0-9A-Za-z_\-]+", text)
        ):
            sender = str(event.get_sender_id())
            session_key = str(event.get_session_id() or "") or sender
            images = [item for item in event.get_messages() if isinstance(item, Image)]
            _arm_pending(session_key, text, sender)
            if images:
                self.logger.info("Card import image in command: id=%s", text)
                async for result in self._import_card_image(event, images[0], text):
                    yield result
                return
            self.logger.info(
                "Card import armed for session %s by %s: %s", session_key, sender, text
            )
            yield event.plain_result(
                f"已记录卡牌 ID：{text}。请在 {PENDING_IMPORT_TTL // 60} 分钟内"
                "于本会话发送完整卡图（含卡名、费用、攻防和描述文本框）；"
                "这段时间内发送的多张图片会按此 ID 逐张识别。"
                "只发卡面插画、手机截图或动图无法识别。"
            )
            return
        if not text:
            # No id supplied: allocate a fresh two-character base62 short code,
            # recognize the following images as a draft, then wait for 同意.
            sender = str(event.get_sender_id())
            session_key = str(event.get_session_id() or "") or sender
            code = _new_temp_card_id()
            _temp_drafts[sender] = {
                "temp_id": code,
                "cards": [],
                "created_at": time.time(),
            }
            _save_temp_drafts()
            _arm_pending(session_key, code, sender)
            images = [item for item in event.get_messages() if isinstance(item, Image)]
            if images:
                self.logger.info("Card import image with generated id=%s", code)
                async for result in self._import_card_image(event, images[0], code):
                    yield result
                return
            yield event.plain_result(
                f"已生成临时短码：{code}。请在 {PENDING_IMPORT_TTL // 60} 分钟内"
                "于本会话发送完整卡图（含卡名、费用、攻防和描述文本框）。"
                "识别后先不写入卡表；回复“同意”或发送 kards confirm 才会同步正式 ID。"
                "临时短码可用 kards setid <短码> <正式ID> 提前指定；"
                "取消请发送 kards cancel。"
            )
            return
        elif text.startswith("[") or text.startswith("{"):
            payload = text
            source = "聊天消息"
        else:
            path = Path(text)
            if not path.is_absolute():
                path = data_paths.DATA_DIR / path
                if not path.exists():
                    legacy_path = data_paths.PLUGIN_DIR / Path(text)
                    if legacy_path.exists():
                        path = legacy_path
            try:
                path = path.resolve()
                allowed_roots = (
                    data_paths.DATA_DIR.resolve(),
                    data_paths.PLUGIN_DIR.resolve(),
                )
                if not any(
                    path == root or root in path.parents for root in allowed_roots
                ):
                    raise ValueError("outside allowed roots")
            except (OSError, ValueError):
                yield event.plain_result("只允许读取数据目录或插件目录内的文件。")
                return
            if not path.is_file():
                yield event.plain_result("文件不存在：" + str(path))
                return
            payload = path.read_text(encoding="utf-8")
            source = str(path)

        try:
            summary = manual_deck.import_cards(payload)
        except ValueError as exc:
            yield event.plain_result("导入失败：" + str(exc))
            return
        except Exception as exc:
            self.logger.warning("Card import failed: %s", exc)
            yield event.plain_result("导入失败，请查看 AstrBot 日志")
            return

        if summary.get("cards"):
            _last_import[str(event.get_sender_id())] = summary["cards"][0]["id"]
        lines = [
            f"卡牌导入完成（{source}）",
            f"新增 {summary['added']} · 更新 {summary['updated']} · "
            f"未变 {summary['unchanged']} · 共 {summary['total']} 张",
        ]
        labels = {"added": "新增", "updated": "更新", "unchanged": "未变"}
        for item in summary["cards"][:12]:
            label = labels.get(item["action"], item["action"])
            lines.append(f"- [{label}] {item['name']}（{item['id']}）")
        if len(summary["cards"]) > 12:
            lines.append(f"……还有 {len(summary['cards']) - 12} 张")
        if summary.get("backup"):
            lines.append("备份：" + summary["backup"])
        lines.append("识别有误可用 kards fix <字段> <新值> 修正")
        yield event.plain_result("\n".join(lines))

    async def _import_card_image(self, event, image, card_id):
        """Recognize one image and merge the extracted card into the table.

        Args:
            event: Message event used for the reply.
            image: AstrBot ``Image`` component taken from the message.
            card_id: Card art id supplied with the import command.
        """
        try:
            path = await image.convert_to_file_path()
            data = Path(path).read_bytes()
        except Exception as exc:
            self.logger.warning("Card image download failed: %s", exc)
            yield event.plain_result("卡图下载失败，请重新发送。")
            return
        sender = str(event.get_sender_id())
        draft = _temp_drafts.get(sender)
        is_draft = bool(draft and draft.get("temp_id") == card_id)
        try:
            summary = await web_api.recognize_card(data, card_id, commit=not is_draft)
        except web_api.CardImportError as exc:
            yield event.plain_result(f"卡图导入失败：{exc}")
            return
        except Exception as exc:
            self.logger.warning("Card image import failed: %s", exc)
            yield event.plain_result("卡图导入失败，请查看 AstrBot 日志。")
            return

        results = summary.get("results") or [summary]
        imported = [item for item in results if not item.get("error")]
        if imported and imported[0].get("import_id"):
            _last_import[sender] = imported[0]["import_id"]
        if is_draft:
            entries = summary.get("draft_entries") or []
            if entries:
                cards = draft.setdefault("cards", [])
                seen = {
                    (str(item.get("cardId") or ""), str(item.get("name_zh") or ""))
                    for item in cards
                }
                for entry in entries:
                    key = (
                        str(entry.get("cardId") or ""),
                        str(entry.get("name_zh") or ""),
                    )
                    if key not in seen:
                        cards.append(entry)
                        seen.add(key)
                draft.setdefault("created_at", time.time())
                _save_temp_drafts()
        lines = [
            (
                f"卡图识别草稿（临时短码 {card_id}）：识别到 {len(results)} 张"
                if is_draft
                else f"卡牌导入完成：识别到 {len(results)} 张"
            ),
            f"新增 {summary['added']} · 更新 {summary['updated']} · "
            f"未变 {summary['unchanged']} · 共 {summary['total']} 张",
        ]
        for item in imported:
            extracted = item.get("extracted") or {}
            name = (
                extracted.get("name_zh")
                or extracted.get("name_en")
                or item.get("card_id", card_id)
            )
            details = []
            for label, key in (
                ("阵营", "faction"),
                ("类型", "type"),
                ("稀有度", "rarity"),
                ("费用", "cost"),
                ("攻击", "attack"),
                ("防御", "defense"),
            ):
                value = extracted.get(key)
                if value not in (None, ""):
                    details.append(f"{label} {value}")
            suffix = " · " + " ".join(details) if details else ""
            lines.append(f"- {name}（{item.get('card_id', card_id)}）{suffix}")
        for error in summary.get("errors") or []:
            lines.append("- 失败：" + str(error))
        if summary.get("backup"):
            lines.append("备份：" + summary["backup"])
        if is_draft:
            lines.append(
                "尚未写入卡表。确认无误请回复“同意”或发送 kards confirm 同步正式 ID；"
                "如需改号发送 kards setid " + card_id + " <正式ID>；放弃发送 kards cancel。"
            )
        else:
            lines.append(
                "识别有误可用 kards fix <字段> <新值> 修正；临时 ID 可用 kards setid 改永久 ID"
            )
        yield event.plain_result("\n".join(lines))

    async def _manual_set_faction(self, event: AstrMessageEvent, kw, ally=False):
        value = (kw or "").strip()
        deck_key = self._deck_user_key(event)
        if ally and value in {"无", "none", "clear", "清除"}:
            deck = manual_deck.load_deck(deck_key)
            deck["ally"] = ""
            manual_deck.save_deck(deck, deck_key)
            yield event.plain_result("已清除盟国设置。")
            return
        english = value if value in deck_code.FACTION_CODE else ""
        if not english:
            for key, chinese in deck_code.FACTION_CN.items():
                if value == chinese:
                    english = key
                    break
        if not english:
            yield event.plain_result(
                "未知国家："
                + value
                + "\n可用："
                + "、".join(deck_code.FACTION_CN.values())
            )
            return

        deck = manual_deck.load_deck(deck_key)
        if ally:
            deck["ally"] = english
            if deck["main"] == english:
                deck["main"] = ""
        else:
            deck["main"] = english
            if deck["ally"] == english:
                deck["ally"] = ""
        manual_deck.save_deck(deck, deck_key)
        label = "盟国" if ally else "主国"
        chinese = deck_code.FACTION_CN.get(english, english)
        yield event.plain_result(f"已设置{label}：{chinese}")

    async def _diy_render(self, event: AstrMessageEvent, spec: dict, art="", caption=""):
        """Render and send a DIY card image, optionally with a text caption."""
        try:
            art_img = _diy_art_image(art)
            png = await asyncio.to_thread(diy_card.render_card_png, spec, art_img)
            cache = data_paths.IMAGE_CACHE_DIR
            cache.mkdir(parents=True, exist_ok=True)
            out = cache / f"diy_{secrets.token_hex(8)}.png"
            out.write_bytes(png)
            parts = []
            if caption:
                parts.append(Plain(caption))
            parts.append(Image.fromFileSystem(str(out)))
            yield event.chain_result(parts)
        except Exception as exc:
            self.logger.warning("DIY card render failed: %s", exc)
            text = "DIY 卡渲染失败，请检查字段是否正确"
            if caption:
                text = caption + "\n" + text
            yield event.plain_result(text)

    async def _diy_art_from_event(self, event: AstrMessageEvent) -> str:
        """Return the first attached image as a data URL, if any."""
        try:
            images = [item for item in event.get_messages() if isinstance(item, Image)]
        except Exception:
            images = []
        if not images:
            return ""
        try:
            path = await images[0].convert_to_file_path()
            raw = Path(path).read_bytes()
            suffix = Path(path).suffix.lower()
            mime = "image/png" if suffix == ".png" else "image/jpeg"
            return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
        except Exception as exc:
            self.logger.warning("DIY attached art decode failed: %s", exc)
            return ""

    async def _diy_exit(self, event: AstrMessageEvent):
        """Leave DIY mode after a final defensive save of the bound card."""
        key = _diy_mode_key(event)
        state = _diy_mode.pop(key, None)
        if not state:
            yield event.plain_result("当前不在 DIY 模式")
            return
        entry = dict(state["spec"])
        entry["id"] = state["id"]
        if state.get("art"):
            entry["art"] = state["art"]
        _diy_save_card(entry)
        name = str(
            state["spec"].get("name")
            or state["spec"].get("name_zh")
            or "未命名"
        )
        art_note = "（含卡图）" if state.get("art") else ""
        yield event.plain_result(
            f"已退出 DIY 模式，卡片 {state['id']}「{name}」已保存{art_note}\n"
            f"继续编辑：kards diy show {state['id']} 或 kards diy last\n"
            "新建另一张：kards diy new"
        )

    async def _manual_diy(self, event: AstrMessageEvent, kw):
        """Chat entry point for the DIY card editor.

        One mode session edits exactly one saved card.  Every change is
        written to diy_cards.json immediately so nothing is lost on exit.
        """
        text = (kw or "").strip()
        parts = text.split(None, 1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub in DIY_MODE_EXIT_ALIASES:
            async for result in self._diy_exit(event):
                yield result
            return

        designs = web_api._load_diy_cards()
        if (not sub and not rest) or sub in {"new", "新建", "create"}:
            card_id = _diy_next_id(designs)
            _diy_save_card({"id": card_id})
            _diy_enter_mode(event, card_id)
            tail = (
                f"继续编辑上次的卡：kards diy last\n" if designs else ""
            )
            yield event.plain_result(
                f"已进入 DIY 模式，正在编辑 {card_id}\n"
                "模式内每条消息只修改这张卡，并自动保存，例如：\n"
                "名=闪电部队 阵营=德国 类型=坦克 费=3 攻=4 防=2 描述=闪击\n"
                "直接附图可当卡图；也可用 JSON：{\"name\":\"测试卡\",\"cost\":2}\n"
                "kards diy help 查看用法 | kards diy list 查看已保存\n"
                + tail
                + "单独发「退出」「exit」「结束」或 kards diy exit 退出模式（卡片保留）"
            )
            return

        if sub in {"last", "最近", "继续", "resume", "上次"}:
            if not designs:
                yield event.plain_result("还没有保存的 DIY 卡，发送 kards diy 新建一张")
                return
            hit = designs[-1]
            card_id = str(hit.get("id") or "")
            spec = {k: v for k, v in hit.items() if k not in ("id", "art")}
            art = str(hit.get("art") or "")
            caption = f"继续编辑：{card_id}\n" + DIY_MODE_TIP
            async for result in self._diy_render(event, spec, art, caption=caption):
                yield result
            _diy_enter_mode(event, card_id, spec, art)
            return

        if sub in {"help", "帮助", "用法", "?"}:
            yield event.plain_result(DIY_HELP_TEXT)
            return

        if sub in {"list", "列表", "ls"}:
            if not designs:
                yield event.plain_result("还没有保存的 DIY 卡，发送 kards diy 查看用法")
                return
            lines = [f"已保存 {len(designs)} 张 DIY 卡："]
            for card in designs:
                name = card.get("name") or "未命名"
                faction = deck_code.FACTION_CN.get(card.get("faction"), card.get("faction", ""))
                kind = deck_code.TYPE_CN.get(card.get("card_type"), card.get("card_type", ""))
                lines.append(f"{card.get('id')} | {name} | {faction} {kind}")
            lines.append("继续编辑某张：kards diy show <ID>")
            yield event.plain_result("\n".join(lines))
            return

        if sub in {"show", "渲染", "查看"} and rest:
            target = rest.strip().strip('"').strip("'")
            hit = next(
                (c for c in designs if str(c.get("id", "")).lower() == target.lower()),
                None,
            )
            if hit is None:
                yield event.plain_result(f"没找到保存的 DIY 卡：{target}")
                return
            card_id = str(hit.get("id") or "")
            spec = {k: v for k, v in hit.items() if k not in ("id", "art")}
            art = str(hit.get("art") or "")
            caption = f"继续编辑：{card_id}\n" + DIY_MODE_TIP
            async for result in self._diy_render(event, spec, art, caption=caption):
                yield result
            _diy_enter_mode(event, card_id, spec, art)
            return

        if sub in {"load", "读取", "载入", "加载", "读卡", "from"} and rest:
            target = rest.strip().strip('"').strip("'")
            loaded = web_api.local_card_design(target)
            if not loaded.get("found"):
                yield event.plain_result(str(loaded.get("error") or "未找到卡牌"))
                return
            source_spec = dict(loaded["spec"])
            card_id = _diy_next_id(designs)
            spec = dict(source_spec)
            spec["id"] = card_id
            art = str(loaded.get("art") or "")
            entry = dict(spec)
            if art:
                entry["art"] = art
            _diy_save_card(entry)
            art_note = (
                f"已附带本地卡图：{loaded['card_id']}"
                if art else "本地卡图不存在，可发一张图片作为插画"
            )
            yield event.plain_result(
                "本地卡表 JSON：\n"
                + json.dumps(loaded["json"], ensure_ascii=False, indent=2)
            )
            caption = (
                f"已从本地卡表载入「{loaded['name']}」（{loaded['import_id']}）"
                f"→ {card_id}\n{art_note}\n" + DIY_MODE_TIP
            )
            async for result in self._diy_render(event, spec, art, caption=caption):
                yield result
            _diy_enter_mode(event, card_id, spec, art)
            return

        save = sub in {"save", "保存", "存"}
        payload = rest if save else text
        spec, error = _diy_parse_fields(payload)
        if error:
            yield event.plain_result(error + "\n" + DIY_HELP_TEXT)
            return
        if not spec:
            yield event.plain_result(DIY_HELP_TEXT)
            return
        card_id = str(spec.pop("id", "") or "").strip()
        art = str(spec.pop("art", "") or "")
        if not art:
            art = await self._diy_art_from_event(event)
        card_id = (card_id if save else "") or _diy_next_id(designs)
        entry = dict(spec)
        entry["id"] = card_id
        if art:
            entry["art"] = art
        _diy_save_card(entry)
        save_note = f"已保存 DIY 卡：{card_id}" if save else f"已新建并保存：{card_id}"
        caption = save_note + "\n" + DIY_MODE_TIP + f" 当前：{card_id}"
        async for result in self._diy_render(event, spec, art, caption=caption):
            yield result
        _diy_enter_mode(event, card_id, spec, art)

    async def _deck_reply(self, event: AstrMessageEvent, code):
        """Decode a deck code and yield a caption plus a rendered image."""
        code = (code or "").strip()
        if not code:
            yield event.plain_result(
                "请提供 KARDS 卡组导入码，例如：\n"
                "kards deck %%53|5ucCbn;5W6LctbjdicAbO;6Q7qcEd7ddd4;cy"
            )
            return
        try:
            deck = deck_code.decode_deck_code(code)
        except deck_code.DeckCodeError as exc:
            yield event.plain_result("卡组码解析失败：" + str(exc))
            return
        except Exception as exc:
            self.logger.warning("Deck code parse failed: %s", exc)
            yield event.plain_result("卡组码解析失败，请检查代码是否完整")
            return

        main_cn = deck_code.FACTION_CN.get(deck.main_faction, deck.main_faction)
        ally_cn = (
            deck_code.FACTION_CN.get(deck.ally_faction, deck.ally_faction)
            if deck.ally_faction
            else "无"
        )
        caption = (
            f"KARDS 卡组 · {main_cn}/{ally_cn} · "
            f"{deck.total} 张（含 HQ {deck.total + deck_code.HQ_COUNT} 张）"
        )
        if deck.warnings:
            caption += " · 有校验提示"

        use_image = self.config.get("deck_image", True) if self.config else True
        if not use_image or use_image in (False, 0, "false", "False", "0", ""):
            yield event.plain_result(deck_code.format_deck(deck))
            return

        try:
            cards_path, info_path = await deck_image.render_deck_images(deck)
        except Exception as exc:
            self.logger.warning("Deck image render failed: %s", exc)
            yield event.plain_result(deck_code.format_deck(deck))
            return

        yield event.chain_result(
            _chain_with_pics(
                caption + " · 卡图 + 信息图", [str(cards_path), str(info_path)]
            )
        )

    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE | filter.EventMessageType.GROUP_MESSAGE
    )
    @filter.command("kardsdeck")
    async def kardsdeck(self, event: AstrMessageEvent, code: GreedyStr):
        async for result in self._deck_reply(event, code):
            yield result

    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE | filter.EventMessageType.GROUP_MESSAGE
    )
    async def import_pending_card_image(self, event: AstrMessageEvent):
        """Import images sent after ``kards import <id>``.

        A command arms the current session for one minute.  Every image from
        the same sender in that session during the window is imported with the
        same card id.  Without a pending command, only a caption/file name
        containing a *known* card id is accepted; random images and generated
        file names are ignored.

        Args:
            event: Message event carrying the card image.
        """
        sender = str(event.get_sender_id())
        session_key = str(event.get_session_id() or "")
        # In DIY mode every image is card art, never a card-table import.
        if _diy_mode_key(event) in _diy_mode:
            return
        text = (event.get_message_str() or "").strip()
        images = [item for item in event.get_messages() if isinstance(item, Image)]
        if not images:
            return
        # Messages that still carry the command are handled by `kards` itself.
        if re.search(r"\bkards\b", text, re.IGNORECASE):
            return

        self.logger.debug(
            "Card image hook: sender=%s images=%d pending=%s text=%r",
            sender,
            len(images),
            list(_pending_import),
            text[:80],
        )

        card_id = ""
        matched_key = ""
        expires_at = 0.0
        for key in (session_key, sender):
            if not key:
                continue
            pending = _pending_import.get(key)
            if not pending:
                continue
            pending_id, pending_expiry, allowed_sender = pending
            if allowed_sender and allowed_sender != sender:
                continue
            card_id, expires_at, matched_key = pending_id, pending_expiry, key
            break

        if not matched_key:
            # 没有 pending 时，只允许“说明文字/文件名 = 卡表里已有的卡牌 ID”。
            # QQ 随机文件名和机器人生成的拼图名都不在卡表里，会被安全忽略。
            candidates = []
            if re.fullmatch(r"[0-9A-Za-z_\-]{2,}", text):
                candidates.append(text)
            raw_name = str(getattr(images[0], "file", "") or "")
            name = re.sub(r"\.[^.]+$", "", Path(raw_name).name)
            if re.fullmatch(r"[0-9A-Za-z_\-]{2,}", name):
                candidates.append(name)
            known = _known_card_ids()
            for candidate in candidates:
                if candidate.lower() in known:
                    card_id = candidate
                    break
            if not card_id:
                self.logger.debug(
                    "Ignore image without known card id: sender=%s candidates=%s",
                    sender,
                    candidates,
                )
                return
        if matched_key and time.time() > expires_at:
            _pending_import.pop(matched_key, None)
            _save_pending_imports()
            yield event.plain_result(
                "卡图导入已超时，请重新发送 kards import <卡牌ID>。"
            )
            return

        # Keep the pending state until its one-minute expiry so several images
        # sent in the same session can be imported with the same card id.
        self.logger.info(
            "Card image import started: sender=%s session=%s id=%s",
            sender,
            session_key,
            card_id,
        )
        for image in images:
            async for result in self._import_card_image(event, image, card_id):
                yield result

    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE | filter.EventMessageType.GROUP_MESSAGE
    )
    async def diy_mode_message(self, event: AstrMessageEvent):
        """Apply every message to the single card bound to this DIY session."""
        key = _diy_mode_key(event)
        state = _diy_mode.get(key)
        if not state:
            return
        raw = str(getattr(getattr(event, "message_obj", None), "message_str", "") or "")
        text = (raw or str(event.get_message_str() or "")).strip()
        # Wake prefixes may or may not be stripped upstream; accept both forms.
        while True:
            stripped = False
            for prefix in ("#", "／", "/", "?"):
                if text.startswith(prefix):
                    text = text[len(prefix) :].strip()
                    stripped = True
            if not stripped:
                break
        # Commands are handled by the `kards` handler, not as card fields.
        if _DIY_COMMAND_RE.match(text):
            return
        # Standalone exit words leave the mode without a `kards diy` prefix.
        if text.replace(" ", "").replace("\u3000", "").lower() in DIY_MODE_EXIT_ALIASES:
            async for result in self._diy_exit(event):
                yield result
            return
        attached_art = await self._diy_art_from_event(event)
        if not text and not attached_art:
            return
        if text:
            delta, error = _diy_parse_fields(text)
            if error:
                yield event.plain_result(error + "\n" + DIY_HELP_TEXT)
                return
            if not delta and not attached_art:
                yield event.plain_result(DIY_HELP_TEXT)
                return
        else:
            delta = {}
        # The card id is fixed for the whole session; later messages only edit it.
        delta.pop("id", None)
        field_art = str(delta.pop("art", "") or "")
        if attached_art:
            state["art"] = attached_art
        elif field_art:
            state["art"] = field_art
        state["spec"].update(delta)
        entry = dict(state["spec"])
        entry["id"] = state["id"]
        if state["art"]:
            entry["art"] = state["art"]
        _diy_save_card(entry)
        caption = f"已保存并更新：{state['id']}"
        async for result in self._diy_render(
            event, dict(state["spec"]), state["art"], caption=caption
        ):
            yield result

    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE | filter.EventMessageType.GROUP_MESSAGE
    )
    @filter.command("kards")
    async def kards(self, event: AstrMessageEvent, args: GreedyStr):
        parts = (args or "").strip().split(None, 1)
        sub = parts[0].lower() if parts else ""
        kw = parts[1].strip() if len(parts) > 1 else ""

        # Manual deck edits are local and cheap, so they bypass the API cooldown.
        cheap_manual = (
            MANUAL_SEARCH_ALIASES
            | MANUAL_ALIAS_ALIASES
            | MANUAL_IMPORT_ALIASES
            | MANUAL_CONFIRM_ALIASES
            | MANUAL_CANCEL_ALIASES
            | MANUAL_REVIEW_ALIASES
            | MANUAL_APPROVE_ALIASES
            | MANUAL_REJECT_ALIASES
            | MANUAL_SETID_ALIASES
            | MANUAL_FIX_ALIASES
            | MANUAL_DECK_ADD_ALIASES
            | MANUAL_DECK_REMOVE_ALIASES
            | MANUAL_DECK_LIST_ALIASES
            | MANUAL_DECK_CLEAR_ALIASES
            | MANUAL_DECK_SHOW_ALIASES
            | MANUAL_DECK_MAIN_ALIASES
            | MANUAL_DECK_ALLY_ALIASES
            | MANUAL_DIY_ALIASES
        )
        if sub not in cheap_manual:
            uid = event.get_sender_id()
            now = time.time()
            if now - _last.get(uid, 0) < COOLDOWN:
                yield event.plain_result("冷却中，稍候再试")
                return
            _last[uid] = now

        if sub in MANUAL_SEARCH_ALIASES:
            async for result in self._manual_search(event, kw):
                yield result
            return

        if sub in MANUAL_ALIAS_ALIASES:
            async for result in self._manual_alias(event, kw):
                yield result
            return

        if sub in MANUAL_IMPORT_ALIASES:
            async for result in self._manual_import(event, kw):
                yield result
            return

        if sub in MANUAL_CONFIRM_ALIASES:
            async for result in self._confirm_draft(event):
                yield result
            return

        if sub in MANUAL_CANCEL_ALIASES:
            async for result in self._cancel_draft(event):
                yield result
            return

        if sub in MANUAL_REVIEW_ALIASES:
            async for result in self._manual_review_list(event):
                yield result
            return

        if sub in MANUAL_APPROVE_ALIASES:
            async for result in self._manual_review_approve(event, kw):
                yield result
            return

        if sub in MANUAL_REJECT_ALIASES:
            async for result in self._manual_review_reject(event, kw):
                yield result
            return

        if sub in MANUAL_SETID_ALIASES:
            async for result in self._manual_set_id(event, kw):
                yield result
            return

        if sub in MANUAL_FIX_ALIASES:
            async for result in self._manual_fix(event, kw):
                yield result
            return

        if sub in MANUAL_DECK_ADD_ALIASES:
            async for result in self._manual_add(event, kw):
                yield result
            return

        if sub in MANUAL_DECK_REMOVE_ALIASES:
            async for result in self._manual_remove(event, kw):
                yield result
            return

        if sub in MANUAL_DECK_LIST_ALIASES:
            async for result in self._manual_list(event):
                yield result
            return

        if sub in MANUAL_DECK_CLEAR_ALIASES:
            async for result in self._manual_clear(event):
                yield result
            return

        if sub in MANUAL_DECK_SHOW_ALIASES:
            async for result in self._manual_show(event):
                yield result
            return

        if sub in MANUAL_DECK_MAIN_ALIASES:
            async for result in self._manual_set_faction(event, kw, ally=False):
                yield result
            return

        if sub in MANUAL_DECK_ALLY_ALIASES:
            async for result in self._manual_set_faction(event, kw, ally=True):
                yield result
            return

        if sub in MANUAL_DIY_ALIASES:
            async for result in self._manual_diy(event, kw):
                yield result
            return

        if sub in DECK_ALIASES:
            async for result in self._deck_reply(event, kw):
                yield result
            return

        if sub in NEWS_REFRESH_ALIASES:
            self.dyn.clear_cache()
            sub = "news"

        if sub in NEWS_LIST_ALIASES:
            sub, kw = "news", "list"

        if sub in NEWS_ALIASES:
            arg = (kw or "").strip()
            if arg.lower() in {"list", "ls", "列表"}:
                try:
                    items = await self.dyn.fetch(NEWS_LIST_LIMIT)
                except Exception as exc:
                    self.logger.warning("Failed to fetch Bilibili dynamics: %s", exc)
                    items = []
                if not items:
                    yield event.plain_result("官方动态拉取失败，请稍后再试")
                    return
                lines = [f"KARDS 官方动态（最新 {len(items)} 条）："]
                for i, item in enumerate(items, 1):
                    title = re.sub(r"\s+", " ", (item.get("title") or "")).strip()
                    if not title:
                        body = (item.get("body") or "").strip().split("\n", 1)[0]
                        body = re.sub(r"\s+", " ", body).strip()
                        title = body[:28] + ("…" if len(body) > 28 else "")
                    when = item.get("pub_time") or ""
                    label = item.get("type_label") or "动态"
                    suffix = f" · {label}" + (f" · {when}" if when else "")
                    lines.append(f"{i}. {title or '（无文字动态）'}{suffix}")
                lines.append("发送 kards news <序号> 查看对应动态")
                yield event.plain_result("\n".join(lines))
                return

            requested = 0
            if arg:
                if not arg.isdigit() or not 1 <= int(arg) <= NEWS_LIST_LIMIT:
                    yield event.plain_result(
                        f"序号范围是 1-{NEWS_LIST_LIMIT}，例如 kards news 3\n"
                        "想看全部标题请发送 kards news list"
                    )
                    return
                requested = int(arg)

            limit = requested or _int_option(
                self.config, "max_items", DEFAULT_MAX_ITEMS
            )
            try:
                items = await self.dyn.fetch(limit)
            except Exception as exc:
                self.logger.warning("Failed to fetch Bilibili dynamics: %s", exc)
                items = []
            if not items:
                yield event.plain_result("官方动态拉取失败，请稍后再试")
                return
            start_index = 1
            if requested:
                if requested > len(items):
                    yield event.plain_result(
                        f"只拉到 {len(items)} 条动态，没有倒数第 {requested} 条"
                    )
                    return
                # The n-th newest item is the last one in an n-item fetch.
                items = items[requested - 1 : requested]
                start_index = requested

            # 一条动态只发一条消息：正文保持完整，真实配图拼成长图一起发。
            # 不再按 200 字拆成十几条、也不再用纯白占位图凑数，避免刷屏。
            with_pics = self._send_pics()
            sent = 0
            for offset, item in enumerate(items):
                index = start_index + offset
                head, body = _dynamic_text(index, item)
                paths = []
                if with_pics:
                    try:
                        paths = await self.images.cache_item(item)
                    except Exception as exc:
                        self.logger.warning("配图缓存失败: %s", exc)
                        paths = []
                paths = [q for q in paths if q]
                sheets = []
                if paths:
                    try:
                        sheets = self.images.merge_pics(paths)
                    except Exception as exc:
                        self.logger.warning("配图合并失败: %s", exc)
                        sheets = paths
                yield event.chain_result(_chain_with_pics(head + "\n" + body, sheets))
                sent += 1

            if not sent:
                yield event.plain_result("KARDS官方暂无新动态")
                return
            return

        if sub != "now" or not kw:
            yield event.plain_result(HELP_TEXT)
            return

        try:
            s = await self.sess_get()
            res = await bf.search(s, kw)
        except Exception:
            yield event.plain_result("请求失败，稍后再试")
            return

        if not res:
            yield event.plain_result("无结果: " + kw)
            return

        lines = ["kards now " + kw]
        for i, r in enumerate(res[:3], 1):
            lines.append(f"{i} {plain(r.get('title') or '-')}")
            lines.append(f"   {r.get('author') or '-'}  播放 {fmt_num(r.get('play'))}")
        yield event.plain_result("\n".join(lines))

    async def _confirm_draft(self, event: AstrMessageEvent):
        """Commit a recognized draft into the official local card table."""
        sender = str(event.get_sender_id())
        detail = "待确认草稿 " + str((_temp_drafts.get(sender) or {}).get("temp_id", ""))
        allowed, review_id = _require_admin(event, "confirm", detail)
        if not allowed:
            yield event.plain_result(_admin_review_notice(review_id, "confirm", detail))
            return
        draft = _temp_drafts.pop(sender, None)
        if not draft or not draft.get("cards"):
            _save_temp_drafts()
            yield event.plain_result("没有待确认的卡牌草稿。")
            return
        try:
            summary = manual_deck.import_cards(draft["cards"])
        except ValueError as exc:
            _temp_drafts[sender] = draft
            _save_temp_drafts()
            yield event.plain_result("同步卡表失败：" + str(exc))
            return
        except Exception as exc:
            _temp_drafts[sender] = draft
            _save_temp_drafts()
            self.logger.warning("Confirm card draft failed: %s", exc)
            yield event.plain_result("同步卡表失败，请查看 AstrBot 日志")
            return
        _save_temp_drafts()
        if summary.get("cards"):
            _last_import[sender] = summary["cards"][0]["id"]
        lines = [
            f"已同意并同步正式 ID：{draft.get('temp_id', '')}",
            f"新增 {summary['added']} · 更新 {summary['updated']} · "
            f"未变 {summary['unchanged']} · 共 {summary['total']} 张",
        ]
        for item in summary["cards"][:12]:
            lines.append(f"- {item['name']}（{item['id']}）")
        if summary.get("backup"):
            lines.append("备份：" + summary["backup"])
        yield event.plain_result("\n".join(lines))

    async def _cancel_draft(self, event: AstrMessageEvent):
        """Discard the waiting card draft for this sender."""
        sender = str(event.get_sender_id())
        detail = "待放弃草稿 " + str((_temp_drafts.get(sender) or {}).get("temp_id", ""))
        allowed, review_id = _require_admin(event, "cancel", detail)
        if not allowed:
            yield event.plain_result(_admin_review_notice(review_id, "cancel", detail))
            return
        draft = _temp_drafts.pop(sender, None)
        _save_temp_drafts()
        if not draft:
            yield event.plain_result("没有待确认的卡牌草稿。")
            return
        yield event.plain_result(
            f"已放弃临时短码 {draft.get('temp_id', '')} 的卡牌草稿，卡表未改动。"
        )

    async def _manual_review_list(self, event: AstrMessageEvent):
        """List requests waiting for administrator approval."""
        if not _is_admin(event):
            yield event.plain_result("审核列表仅管理员可用")
            return
        pending = [
            item
            for item in _admin_reviews.values()
            if item.get("status") == "pending"
        ]
        if not pending:
            yield event.plain_result("暂无待审核请求")
            return
        pending.sort(key=lambda item: item.get("created_at", 0))
        lines = [f"待审核 {len(pending)} 条："]
        for item in pending:
            label = ADMIN_REVIEW_LABELS.get(item.get("kind"), item.get("kind"))
            note = (item.get("payload") or {}).get("note") or ""
            suffix = f"（{note}）" if note else ""
            lines.append(
                f"- {item['id']} [{label}] 提交者 {item.get('sender', '')}："
                f"{item.get('detail', '')}{suffix}"
            )
        lines.append("批准：kards approve <编号>；驳回：kards reject <编号>")
        yield event.plain_result("\n".join(lines))

    async def _manual_review_approve(self, event: AstrMessageEvent, kw):
        """Replay an approved request with the original sender's identity."""
        if not _is_admin(event):
            yield event.plain_result("审核操作仅管理员可用")
            return
        review_id, entry = _find_admin_review(kw)
        if not entry:
            yield event.plain_result("审核编号不存在：" + review_id)
            return
        if entry.get("status") != "pending":
            yield event.plain_result(
                f"审核 {review_id} 已处理（{entry.get('status')}）"
            )
            return
        shim = _ReviewEvent(event, entry.get("sender", ""), entry.get("session_id", ""))
        try:
            payload = entry.get("payload") or {}
            preapproved = payload.get("preapproved_cards")
            if preapproved is not None:
                summary = manual_deck.import_cards(preapproved)
                shim.outputs.append(
                    "卡牌导入完成（审核批准）："
                    f"新增 {summary['added']} · 更新 {summary['updated']} · "
                    f"未变 {summary['unchanged']}"
                )
            else:
                kind = entry.get("kind")
                if kind == "confirm":
                    async for _ in self._confirm_draft(shim):
                        pass
                elif kind == "cancel":
                    async for _ in self._cancel_draft(shim):
                        pass
                elif kind == "setid":
                    async for _ in self._manual_set_id(shim, entry.get("detail", "")):
                        pass
                elif kind == "fix":
                    async for _ in self._manual_fix(shim, entry.get("detail", "")):
                        pass
                elif kind == "import":
                    async for _ in self._manual_import(shim, entry.get("detail", "")):
                        pass
                else:
                    yield event.plain_result("未知审核类型：" + str(kind))
                    return
        except Exception as exc:
            self.logger.warning("Approve admin review failed: %s", exc)
            yield event.plain_result("批准失败：" + str(exc))
            return
        entry["status"] = "approved"
        entry["approved_by"] = str(event.get_sender_id())
        entry["resolved_at"] = time.time()
        _save_admin_reviews()
        lines = [f"已批准审核 {review_id}"]
        lines.extend(shim.outputs or ["（无回执）"])
        yield event.plain_result("\n".join(lines))

    async def _manual_review_reject(self, event: AstrMessageEvent, kw):
        """Reject a queued request without touching the card table."""
        if not _is_admin(event):
            yield event.plain_result("审核操作仅管理员可用")
            return
        review_id, entry = _find_admin_review(kw)
        if not entry:
            yield event.plain_result("审核编号不存在：" + review_id)
            return
        if entry.get("status") != "pending":
            yield event.plain_result(
                f"审核 {review_id} 已处理（{entry.get('status')}）"
            )
            return
        entry["status"] = "rejected"
        entry["rejected_by"] = str(event.get_sender_id())
        entry["resolved_at"] = time.time()
        _save_admin_reviews()
        yield event.plain_result(
            f"已驳回审核 {review_id}，卡表与草稿均未改动"
        )

    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE | filter.EventMessageType.GROUP_MESSAGE
    )
    async def confirm_draft_message(self, event: AstrMessageEvent):
        """Accept a plain “同意” after a draft import, or “取消” to discard it."""
        sender = str(event.get_sender_id())
        if sender not in _temp_drafts:
            return
        raw = str(getattr(event.message_obj, "message_str", "") or "").strip()
        for prefix in ("#", "／", "/"):
            if raw.startswith(prefix):
                raw = raw[len(prefix) :].strip()
        if raw in {"同意", "确认", "确认导入", "好的"}:
            async for result in self._confirm_draft(event):
                yield result
        elif raw in {"取消", "撤销", "不要"}:
            async for result in self._cancel_draft(event):
                yield result

    # ---------- LLM tools ----------
    # Every chat command is also exposed as a function-calling tool so the LLM
    # can run the exact same logic without asking the user to type `#kards ...`.

    async def _tool_kards(self, event: AstrMessageEvent, sub: str, kw: str = ""):
        """Internal helper: dispatch a tool call through the normal command path."""
        args = " ".join(part for part in (sub, str(kw or "").strip()) if part)
        async for result in self.kards(event, GreedyStr(args)):
            yield result

    @llm_tool(name="kards_help")
    async def tool_help(self, event: AstrMessageEvent):
        """查看 KARDS 助手支持的全部命令和用法。"""
        yield event.plain_result(HELP_TEXT)

    @llm_tool(name="kards_deck_parse")
    async def tool_deck_parse(self, event: AstrMessageEvent, code: str):
        """解析 KARDS 卡组码，生成卡表、卡图和信息图。

        Args:
            code(string): 卡组码，通常以 %% 开头。
        """
        async for result in self._tool_kards(event, "deck", code):
            yield result

    @llm_tool(name="kards_news")
    async def tool_news(self, event: AstrMessageEvent, index: str):
        """查看 KARDS 官方 B 站动态；index 为空表示最新若干条。

        Args:
            index(string): 倒数第几条动态（1-20），留空查看最新动态，填 list 查看标题列表。
        """
        async for result in self._tool_kards(event, "news", index):
            yield result

    @llm_tool(name="kards_news_refresh")
    async def tool_news_refresh(self, event: AstrMessageEvent):
        """跳过缓存，重新拉取 KARDS 官方 B 站动态。"""
        async for result in self._tool_kards(event, "刷新动态"):
            yield result

    @llm_tool(name="kards_video_search")
    async def tool_video_search(self, event: AstrMessageEvent, keyword: str):
        """搜索 B 站上的 KARDS 相关视频。

        Args:
            keyword(string): 视频搜索关键词。
        """
        async for result in self._tool_kards(event, "now", keyword):
            yield result

    @llm_tool(name="kards_card_search")
    async def tool_card_search(self, event: AstrMessageEvent, query: str):
        """搜索本地 KARDS 卡表，支持名称、别名、描述、标签和属性筛选。

        Args:
            query(string): 搜索词，如“德国 坦克 6K2/5/4 闪击”。
        """
        async for result in self._manual_search(event, query):
            yield result

    @llm_tool(name="kards_alias_add")
    async def tool_alias_add(self, event: AstrMessageEvent, card: str, alias: str):
        """给一张卡牌添加别名。

        Args:
            card(string): 卡牌名称或导入 ID。
            alias(string): 要添加的别名。
        """
        async for result in self._manual_alias(event, f"{card} ～ {alias}"):
            yield result

    @llm_tool(name="kards_diy")
    async def tool_diy(self, event: AstrMessageEvent, spec: str):
        """渲染 DIY KARDS 卡面；支持字段=值、JSON、list、show、save。

        Args:
            spec(string): 如“名=测试卡 阵营=美国 费=3”；list 列出已保存；show <ID> 渲染；save 字段=值 保存并渲染。
        """
        async for result in self._manual_diy(event, spec):
            yield result

    @llm_tool(name="kards_alias_list")
    async def tool_alias_list(self, event: AstrMessageEvent):
        """查看当前已保存的全部卡牌别名。"""
        async for result in self._manual_alias(event, "list"):
            yield result

    @llm_tool(name="kards_alias_delete")
    async def tool_alias_delete(self, event: AstrMessageEvent, alias: str):
        """删除一个卡牌别名。

        Args:
            alias(string): 要删除的别名。
        """
        async for result in self._manual_alias(event, f"del {alias}"):
            yield result

    @llm_tool(name="kards_import")
    async def tool_import(self, event: AstrMessageEvent, payload: str):
        """导入卡牌数据，或生成两位临时 ID 后等待发送卡图识别。

        Args:
            payload(string): 卡牌 ID、JSON 数组或数据文件名；留空则生成两位 base62 临时 ID。
        """
        async for result in self._manual_import(event, payload):
            yield result

    @llm_tool(name="kards_setid")
    async def tool_setid(self, event: AstrMessageEvent, target: str, permanent_id: str):
        """把临时卡牌 ID 设置成正式的 2 位字母/数字 ID。

        Args:
            target(string): 临时 ID 或卡牌名称。
            permanent_id(string): 新的 2 位正式 ID。
        """
        async for result in self._manual_set_id(event, f"{target} {permanent_id}"):
            yield result

    @llm_tool(name="kards_fix")
    async def tool_fix(self, event: AstrMessageEvent, args: str):
        """修正最近识别卡牌或指定卡牌的识别错误字段。

        Args:
            args(string): 如“阵营 德国”或“卡牌名 费用 3”。
        """
        async for result in self._manual_fix(event, args):
            yield result

    @llm_tool(name="kards_deck_add")
    async def tool_deck_add(self, event: AstrMessageEvent, card: str, count: float = 1):
        """向当前用户的手动卡组添加卡牌。

        Args:
            card(string): 卡牌名称或导入 ID。
            count(number): 添加张数，默认 1。
        """
        amount = max(1, int(count or 1))
        async for result in self._manual_add(event, f"{card} x{amount}"):
            yield result

    @llm_tool(name="kards_deck_remove")
    async def tool_deck_remove(self, event: AstrMessageEvent, card: str, count: float = 1):
        """从当前用户的手动卡组移除卡牌。

        Args:
            card(string): 卡牌名称或导入 ID。
            count(number): 移除张数，默认 1。
        """
        amount = max(1, int(count or 1))
        async for result in self._manual_remove(event, f"{card} x{amount}"):
            yield result

    @llm_tool(name="kards_deck_list")
    async def tool_deck_list(self, event: AstrMessageEvent):
        """以文字列出当前用户的手动卡组。"""
        async for result in self._manual_list(event):
            yield result

    @llm_tool(name="kards_deck_show")
    async def tool_deck_show(self, event: AstrMessageEvent):
        """生成当前用户手动卡组的卡图。"""
        async for result in self._manual_show(event):
            yield result

    @llm_tool(name="kards_deck_clear")
    async def tool_deck_clear(self, event: AstrMessageEvent):
        """清空当前用户的手动卡组。"""
        async for result in self._manual_clear(event):
            yield result

    @llm_tool(name="kards_main_faction")
    async def tool_main_faction(self, event: AstrMessageEvent, faction: str):
        """设置当前用户手动卡组的主国。

        Args:
            faction(string): 国家名称，如“德国”。
        """
        async for result in self._manual_set_faction(event, faction, ally=False):
            yield result

    @llm_tool(name="kards_ally_faction")
    async def tool_ally_faction(self, event: AstrMessageEvent, faction: str):
        """设置当前用户手动卡组的盟国。

        Args:
            faction(string): 国家名称，如“苏联”。
        """
        async for result in self._manual_set_faction(event, faction, ally=True):
            yield result

    @llm_tool(name="kards_confirm")
    async def tool_confirm(self, event: AstrMessageEvent):
        """同意并写入上次识别后待确认的卡牌到正式卡表。"""
        async for result in self._confirm_draft(event):
            yield result

    @llm_tool(name="kards_cancel")
    async def tool_cancel(self, event: AstrMessageEvent):
        """取消上次识别后待确认的卡牌草稿。"""
        async for result in self._cancel_draft(event):
            yield result

    @llm_tool(name="kards_review")
    async def tool_review(self, event: AstrMessageEvent):
        """管理员查看待审核的卡牌操作请求。"""
        async for result in self._manual_review_list(event):
            yield result

    @llm_tool(name="kards_approve")
    async def tool_approve(self, event: AstrMessageEvent, review_id: str):
        """管理员批准一条待审核请求，按原提交者身份执行。

        Args:
            review_id(string): 审核编号，如 RV-a1b2c3。
        """
        async for result in self._manual_review_approve(event, review_id):
            yield result

    @llm_tool(name="kards_reject")
    async def tool_reject(self, event: AstrMessageEvent, review_id: str):
        """管理员驳回一条待审核请求，不改动卡表。

        Args:
            review_id(string): 审核编号，如 RV-a1b2c3。
        """
        async for result in self._manual_review_reject(event, review_id):
            yield result

    async def terminate(self):
        if self.sess and not self.sess.closed:
            await self.sess.close()
        await self.dyn.close()
