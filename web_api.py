"""WebUI bridge routes for the manual KARDS deck builder."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import secrets
from pathlib import Path

from PIL import Image

from astrbot.api import logger
from astrbot.api.web import error_response, file_response, json_response, request

from . import card_crop, data_paths, deck_code, deck_image, manual_deck

THUMB_DIR = data_paths.THUMB_DIR

ROUTE_PREFIXES = ("astrbot_plugin_bili_kards", "bilikards")

# Filled in by :func:`register` so the image-import route can call the
# configured vision provider through the plugin context.
_context = None
_plugin_config: dict = {}

# Prefer a fast, reliable image model.  Reasoning-heavy models can exceed the
# provider request timeout and make the whole import look broken.
DEFAULT_VISION_PROVIDER = "openai-responses/Sage-S2.1-Flash"
# Tried in order when the configured provider is missing, times out, or does
# not return usable card data.  Every entry must accept image input.
VISION_FALLBACK_PROVIDERS = ("openai-responses/Sage-S2.1-Flash",)
# Agents-A1 is declared image-capable but the current gateway rejects its
# multimodal requests (HTTP 400, invalid message content).  Skip it for card
# recognition instead of wasting time on a request that cannot succeed.
VISION_EXCLUDED_PROVIDERS = {"openai-responses/Agents-A1"}
VISION_TIMEOUT = 75
VISION_MAX_ATTEMPTS = 3
MAX_IMAGE_SIDE = 1600


def _faction_name(value: object) -> str:
    """Map an English or Chinese faction name to the English key."""
    text = str(value or "").strip()
    if text in deck_code.FACTION_CODE:
        return text
    for english, chinese in deck_code.FACTION_CN.items():
        if text == chinese:
            return english
    return ""


async def get_catalog():
    """Return every locally known card for the page catalog."""
    cards = manual_deck.card_catalog()
    keywords: dict[str, str] = {}
    for card in cards:
        for key, label in zip(
            card.get("keywords") or [], card.get("keywords_cn") or []
        ):
            keywords.setdefault(key, label)
    return json_response(
        {
            "cards": cards,
            "factions": [
                {"value": key, "label": label}
                for key, label in deck_code.FACTION_CN.items()
            ],
            "types": [
                {"value": key, "label": label}
                for key, label in deck_code.TYPE_CN.items()
            ],
            "rarities": [
                {"value": key, "label": label}
                for key, label in deck_code.RARITY_CN.items()
            ],
            "keywords": [
                {"value": key, "label": label}
                for key, label in sorted(keywords.items(), key=lambda item: item[1])
            ],
            "fix_fields": [
                {"value": key, "label": item[1]}
                for key, item in manual_deck.FIX_FIELDS.items()
            ],
        }
    )


async def get_aliases():
    """Return the user-maintained alias library."""
    aliases = manual_deck.load_aliases()
    items = []
    for import_id, values in aliases.items():
        info = deck_code.CARDS.get(import_id)
        items.append(
            {
                "id": import_id,
                "name": (info[1] or info[2] or import_id) if info else import_id,
                "aliases": values,
            }
        )
    items.sort(key=lambda item: item["name"])
    return json_response(
        {
            "items": items,
            "total": sum(len(item["aliases"]) for item in items),
        }
    )


async def add_alias():
    """Add one alias to a card."""
    body = await request.json({})
    body = body if isinstance(body, dict) else {}
    import_id = str(body.get("id") or "").strip()
    alias = str(body.get("alias") or "").strip()
    ok, message = manual_deck.add_alias(import_id, alias)
    if not ok:
        return error_response(message, status_code=400)
    return json_response(
        {
            "ok": True,
            "message": message,
            "id": import_id,
            "aliases": manual_deck.load_aliases().get(import_id, []),
        }
    )


async def fix_card():
    """Correct one field of a card from the manual-deck page."""
    body = await request.json({})
    body = body if isinstance(body, dict) else {}
    import_id = str(body.get("id") or "").strip()
    field = manual_deck.fix_field_key(body.get("field"))
    if not import_id or not field:
        return error_response("请选择卡牌和要修改的字段", status_code=400)
    try:
        result = manual_deck.apply_card_fix(import_id, field, body.get("value"))
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except Exception as exc:
        logger.warning("Card fix via web failed: %s", exc)
        return error_response("修改失败，请查看 AstrBot 日志", status_code=500)
    return json_response({"ok": True, **result})


async def remove_alias():
    """Remove one alias from a card."""
    body = await request.json({})
    body = body if isinstance(body, dict) else {}
    import_id = str(body.get("id") or "").strip()
    alias = str(body.get("alias") or "").strip()
    ok, message = manual_deck.remove_alias(import_id, alias)
    if not ok:
        return error_response(message, status_code=400)
    return json_response(
        {
            "ok": True,
            "message": message,
            "id": import_id,
            "aliases": manual_deck.load_aliases().get(import_id, []),
        }
    )


async def import_cards():
    """Import official card data into the local card table."""
    body = await request.json({})
    if isinstance(body, list):
        payload = body
    elif isinstance(body, dict):
        payload = body.get("cards")
        if payload is None:
            payload = body.get("text") or body.get("json") or body
    else:
        payload = body
    if isinstance(payload, str) and not payload.strip():
        return error_response("请粘贴卡牌 JSON 或上传 .json 文件", status_code=400)
    try:
        summary = manual_deck.import_cards(payload)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except Exception as exc:
        logger.warning("Card import failed: %s", exc)
        return error_response("导入失败，请查看 AstrBot 日志", status_code=500)
    return json_response(summary)


def _deck_user_key() -> str:
    """Return the deck key for the authenticated dashboard user."""
    try:
        username = request.username
    except RuntimeError:
        username = ""
    return manual_deck.web_user_key(username)


async def get_deck():
    """Return the current user's manual deck."""
    deck = manual_deck.load_deck(_deck_user_key())
    return json_response(manual_deck.deck_payload(deck))


async def add_card():
    """Add copies of one card to the current user's manual deck."""
    body = await request.json({})
    body = body if isinstance(body, dict) else {}
    import_id = str(body.get("id") or "").strip()
    try:
        deck = manual_deck.add_to_deck(
            import_id, body.get("count", 1), _deck_user_key()
        )
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    return json_response(manual_deck.deck_payload(deck))


async def remove_card():
    """Remove copies of one card from the current user's manual deck."""
    body = await request.json({})
    body = body if isinstance(body, dict) else {}
    import_id = str(body.get("id") or "").strip()
    deck = manual_deck.remove_from_deck(
        import_id, body.get("count", 1), _deck_user_key()
    )
    return json_response(manual_deck.deck_payload(deck))


async def set_factions():
    """Set or clear the current user's faction overrides."""
    body = await request.json({})
    body = body if isinstance(body, dict) else {}
    main = _faction_name(body.get("main"))
    ally = _faction_name(body.get("ally"))
    if body.get("main") and not main:
        return error_response("未知主国", status_code=400)
    if body.get("ally") and not ally:
        return error_response("未知盟国", status_code=400)
    if ally == main:
        ally = ""

    key = _deck_user_key()
    deck = manual_deck.load_deck(key)
    deck["main"] = main
    deck["ally"] = ally
    manual_deck.save_deck(deck, key)
    return json_response(manual_deck.deck_payload(deck))


async def clear_deck():
    """Remove every card from the current user's manual deck."""
    key = _deck_user_key()
    deck = manual_deck.load_deck(key)
    deck["cards"] = {}
    manual_deck.save_deck(deck, key)
    return json_response(manual_deck.deck_payload(deck))


async def render_deck():
    """Render the current user's manual deck and return both images."""
    deck = manual_deck.load_deck(_deck_user_key())
    try:
        decoded = manual_deck.build_decoded(deck)
        cards_path, info_path = await deck_image.render_deck_images(decoded)
    except ValueError as exc:
        return error_response(str(exc), status_code=400)
    except Exception as exc:
        logger.warning("Manual deck render failed: %s", exc)
        return error_response("生成图片失败，请查看 AstrBot 日志", status_code=500)

    def data_url(path: Path) -> str:
        return "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode(
            "ascii"
        )

    return json_response(
        {
            "cards": data_url(cards_path),
            "info": data_url(info_path),
            "code": decoded.raw,
            "deck": manual_deck.deck_payload(deck),
        }
    )


async def card_thumb():
    """Serve one bundled card thumbnail by card id."""
    card_id = str(request.query.get("card_id") or "").strip().lower()
    if not card_id or not card_id.replace("_", "").replace("-", "").isalnum():
        return error_response("无效卡图", status_code=400)
    names = [card_id]
    if card_id.endswith("_vet"):
        names.append(card_id[:-4])
    for name in names:
        path = THUMB_DIR / f"{name}.jpg"
        if path.is_file():
            return file_response(path, content_type="image/jpeg")
    return error_response("卡图不存在", status_code=404)


def _clean_card_id(value: object) -> str:
    """Normalize a user-supplied card id for use as a thumbnail filename."""
    text = str(value or "").strip()
    if not text or len(text) > 96:
        return ""
    if not re.fullmatch(r"[0-9A-Za-z_\-]+", text):
        return ""
    return text


async def card_image_data():
    """Return one bundled card thumbnail as a data URL for WebUI preview."""
    card_id = _clean_card_id(str(request.query.get("card_id") or "").strip().lower())
    if not card_id:
        return error_response("无效卡图", status_code=400)
    names = [card_id]
    if card_id.endswith("_vet"):
        names.append(card_id[:-4])
    for name in names:
        # 与聊天端 _local_thumb 相同的本地查找顺序，覆盖 deck_card_cache 缓存
        candidates = (
            (THUMB_DIR / f"{name}.jpg", "image/jpeg"),
            (data_paths.CARD_CACHE_DIR / f"{name}.webp", "image/webp"),
            (data_paths.CARD_CACHE_DIR / f"{name}.jpg", "image/jpeg"),
            (data_paths.CARD_CACHE_DIR / f"{name}.png", "image/png"),
        )
        for path, mime in candidates:
            if path.is_file():
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
                return json_response(
                    {"image": f"data:{mime};base64," + encoded, "card_id": card_id}
                )
    return json_response({"image": "", "card_id": card_id})


def _normalize_image(raw: bytes) -> tuple[bytes, str]:
    """Convert raw image bytes to JPEG and return bytes plus a data URL.

    Args:
        raw: Original image bytes in any format Pillow can read.

    Returns:
        ``(jpeg_bytes, jpeg_data_url)``.

    Raises:
        ValueError: If the bytes are empty or not a readable image.
    """
    if not raw:
        raise ValueError("卡图内容为空")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            image = image.convert("RGB")
            if max(image.size) > MAX_IMAGE_SIDE:
                image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=92, optimize=True)
    except (OSError, ValueError) as exc:
        raise ValueError("无法识别该图片，请上传 JPG/PNG/WebP 卡图") from exc

    data = buffer.getvalue()
    encoded = base64.b64encode(data).decode("ascii")
    return data, "data:image/jpeg;base64," + encoded


def _decode_upload_image(data_url: object) -> tuple[bytes, str]:
    """Decode a base64 image upload and return JPEG bytes plus a data URL.

    Args:
        data_url: A ``data:image/...;base64,`` string or bare base64 text.

    Returns:
        ``(jpeg_bytes, jpeg_data_url)``.

    Raises:
        ValueError: If the payload is empty, not base64, or not an image.
    """
    text = str(data_url or "").strip()
    if not text:
        raise ValueError("请上传卡图")
    if text.startswith("data:"):
        _, _, text = text.partition(",")
    try:
        raw = base64.b64decode(text, validate=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("卡图不是有效的 base64 数据") from exc
    return _normalize_image(raw)


def _vision_providers() -> list[dict]:
    """List configured providers that accept image input."""
    manager = getattr(_context, "provider_manager", None)
    configs = getattr(manager, "providers_config", None) or []
    providers = []
    for item in configs:
        if not isinstance(item, dict):
            continue
        if item.get("enable") is False:
            continue
        modalities = item.get("modalities") or []
        if "image" not in modalities:
            continue
        providers.append(
            {
                "id": str(item.get("id") or ""),
                "model": str(item.get("model") or ""),
                "source": str(item.get("provider_source_id") or ""),
            }
        )
    return providers


async def get_vision_providers():
    """Return image-capable providers and the configured default."""
    configured = str(_plugin_config.get("vision_provider_id") or "").strip()
    providers = _vision_providers()
    known = {item["id"] for item in providers}
    default = configured if configured in known else ""
    if not default and DEFAULT_VISION_PROVIDER in known:
        default = DEFAULT_VISION_PROVIDER
    if not default and providers:
        default = providers[0]["id"]
    return json_response(
        {
            "providers": providers,
            "default": default,
            "configured": configured,
        }
    )


def _extract_json_object(text: str) -> dict:
    """Parse the first JSON object in a model response."""
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("模型没有返回 JSON")
    try:
        data = json.loads(cleaned[start : end + 1])
    except ValueError as exc:
        raise ValueError("模型返回的 JSON 无法解析") from exc
    if not isinstance(data, dict):
        raise ValueError("模型返回的不是 JSON 对象")
    return data


def _normalize_keywords(values: object) -> list[str]:
    """Map Chinese or un-normalized ability labels to official keys."""
    if isinstance(values, str):
        values = re.split(r"[,，;；\s]+", values)
    if not isinstance(values, list):
        return []

    label_to_key = {label: key for key, label in deck_code.KEYWORD_CN.items()}
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = ""
        if text in deck_code.KEYWORD_CN:
            key = text
        elif text in label_to_key:
            key = label_to_key[text]
        elif text in deck_code.KEYWORD_ALIASES:
            key = deck_code.KEYWORD_ALIASES[text]
        else:
            match = re.fullmatch(
                r"(heavyArmor|intel)\s*[:：]?\s*([123])?", text, re.IGNORECASE
            )
            if not match:
                match = re.fullmatch(r"(重甲|情报)\s*[:：]?\s*([123])?", text)
            if match:
                base = match.group(1)
                base = {
                    "heavyarmor": "heavyArmor",
                    "intel": "intel",
                    "重甲": "heavyArmor",
                    "情报": "intel",
                }.get(base.lower(), base)
                key = base + (":" + match.group(2) if match.group(2) else "")
        if key and key not in result:
            result.append(key)
    return result


def _normalize_rarity(value: object) -> str:
    """Map Chinese or legacy rarity labels to the stored English key."""
    text = str(value or "").strip()
    if not text:
        return ""
    alias = deck_code.RARITY_ALIASES.get(text)
    if alias:
        return alias
    for key, label in deck_code.RARITY_CN.items():
        if text.lower() in (key.lower(), label.lower()):
            return key
    return text


def _vision_prompt() -> str:
    factions = "、".join(deck_code.FACTION_CN)
    types = "、".join(deck_code.TYPE_CN)
    rarities = "、".join(deck_code.RARITY_CN.values())
    keywords = "、".join(
        f"{label}（{key}）" for key, label in deck_code.KEYWORD_CN.items()
    )
    return (
        "你是 KARDS 卡牌识别助手。图片是一张 KARDS 卡牌的正面，请读取卡面信息。\n"
        "只输出一个 JSON 对象，不要输出 markdown 或解释。字段如下：\n"
        '{"name_zh": "中文名", "name_en": "英文名", "faction": "阵营", '
        '"type": "类型", "rarity": "稀有度", "cost": 费用数字, '
        '"operation_cost": 行动费用数字, "attack": 攻击数字, '
        '"defense": 防御数字, "description": "卡牌描述原文", '
        '"keywords": ["关键字"], "tags": ["标签文本"]}\n'
        f"faction 只能取：{factions}。\n"
        f"type 只能取：{types}。\n"
        f"rarity 只能取：{rarities}（普通就是 Standard，也接受「标准」，衍生牌对应 Token，也可写「衍生」）。\n"
        "稀有度请按卡面底部中央的稀有度标记判读，不要根据卡牌强弱猜测："
        "4 个灰色小方块是 Standard，3 个黄色小方块是 Limited，"
        "2 个带星银色小方块是 Special，1 个带星金色小方块是 Elite；"
        "卡面没有任何稀有度标记时是衍生牌，rarity 填「衍生牌」或「衍生」（Token），"
        "不要填 null；只有标记模糊到无法判断外观时才填 null。\n"
        f"keywords 只能取：{keywords}，重甲/情报带等级写成 heavyArmor:1、intel:2。\n"
        "词条名称按官方中文写：fury 是「奋战」，salvage 是「收缴」；"
        "不要写「狂怒」「狂热」「回收」。\n"
        "费用在卡牌左上角，攻击在左下角，防御在右下角，请逐项核对数字，不要猜测。\n"
        "无法确定的字段填 null。description 保留卡面上的中文描述，不要翻译或改写。\n"
        "tags 请根据卡图和卡面效果生成用于搜索的标签文本（中文短词/短语），可包含兵种、定位、效果关键词、"
        "主题等；没有可补充标签时返回空数组。\n"
        "如果图片不是完整的 KARDS 卡牌正面（例如只有卡面插画、手机截图、"
        "聊天记录、表情包或动图），只输出 {\"error\":\"not_a_card\"}。"
    )


class CardImportError(Exception):
    """Raised when a card image cannot be recognized or imported.

    Attributes:
        status_code: HTTP status code to use when serving the web route.
    """

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _vision_candidates(preferred: object = "") -> list[str]:
    """Return the image providers to try, best first.

    Args:
        preferred: Provider explicitly requested by the caller.

    Returns:
        Up to :data:`VISION_MAX_ATTEMPTS` provider ids that accept images.
    """
    known = [
        item["id"]
        for item in _vision_providers()
        if item["id"] and item["id"] not in VISION_EXCLUDED_PROVIDERS
    ]
    known_set = set(known)
    ordered: list[str] = []

    def add(value: object) -> None:
        text = str(value or "").strip()
        if text and text in known_set and text not in ordered:
            ordered.append(text)

    add(preferred)
    add(_plugin_config.get("vision_provider_id"))
    add(DEFAULT_VISION_PROVIDER)
    for value in VISION_FALLBACK_PROVIDERS:
        add(value)
    for value in known:
        add(value)
    return ordered[:VISION_MAX_ATTEMPTS]


async def _recognize_with_provider(chosen: str, image_url: str) -> tuple[dict, str]:
    """Ask one vision provider to read a card image.

    Args:
        chosen: Provider id to call.
        image_url: JPEG data URL of the card image.

    Returns:
        ``(fields, raw_text)`` for a usable recognition result.

    Raises:
        asyncio.TimeoutError: If the provider takes too long.
        ValueError: If the model output is not a JSON object.
        CardImportError: If the model did not return a card name.
    """
    response = await asyncio.wait_for(
        _context.llm_generate(
            chat_provider_id=chosen,
            prompt=_vision_prompt(),
            image_urls=[image_url],
        ),
        timeout=VISION_TIMEOUT,
    )
    text = (getattr(response, "completion_text", "") or "").strip()
    fields = _extract_json_object(text)
    flag = str(fields.get("error") or "").strip().lower()
    if flag in {"not_a_card", "not_card", "not a card"}:
        raise CardImportError(
            "图片不是完整 KARDS 卡牌正面。请发送包含卡名、费用、攻防和描述文本的完整卡图；"
            "只有插画、手机截图或表情包无法识别。"
        )
    if not fields.get("name_zh") and not fields.get("name_en"):
        raise CardImportError(
            "没有读出卡牌名称。请发送清晰、完整的 KARDS 卡图"
            "（含卡名、费用、攻防和描述文本框）；只有插画或截图无法导入。"
        )
    return fields, text


async def _recognize_one_card(
    image_bytes: bytes,
    card_id: str,
    provider: object = "",
    existing_id: str = "",
    commit: bool = True,
) -> dict:
    """Recognize one cropped card image and optionally merge it into the table.

    Args:
        image_bytes: JPEG bytes of one cropped card.
        card_id: Card art id to write.
        provider: Optional vision provider id.
        existing_id: Import id to update instead of adding a new row.
        commit: Write to the card table now; False only builds a draft entry.

    Returns:
        Import summary for that single card.

    Raises:
        CardImportError: If recognition or import fails.
    """
    encoded = base64.b64encode(image_bytes).decode("ascii")
    image_url = "data:image/jpeg;base64," + encoded

    candidates = _vision_candidates(provider)
    if not candidates:
        raise CardImportError(
            "没有可用的视觉模型，请先在 AstrBot 中添加支持图片的模型，"
            "或在本插件配置里填写 vision_provider_id"
        )

    fields: dict = {}
    text = ""
    chosen = ""
    last_error = ""
    for candidate in candidates:
        try:
            fields, text = await _recognize_with_provider(candidate, image_url)
            chosen = candidate
            break
        except asyncio.TimeoutError:
            last_error = f"{candidate} 识别超时"
            logger.warning("Card image recognition via %s timed out", candidate)
        except ValueError as exc:
            last_error = f"{candidate} 返回格式错误：{exc}"
            logger.warning(
                "Card image recognition via %s returned invalid JSON: %s",
                candidate,
                exc,
            )
        except CardImportError as exc:
            last_error = f"{candidate}：{exc}"
            logger.warning("Card image recognition via %s failed: %s", candidate, exc)
        except Exception as exc:
            last_error = f"{candidate} 调用失败：{exc}"
            logger.warning("Card image recognition via %s failed: %s", candidate, exc)

    if not chosen:
        raise CardImportError(
            "视觉模型识别失败，请稍后重试或更换 vision_provider_id"
            + (f"（{last_error}）" if last_error else ""),
            status_code=502,
        )

    if "rarity" in fields:
        fields["rarity"] = _normalize_rarity(fields["rarity"])

    entry: dict = {"cardId": card_id}
    if card_id.startswith("DIY-"):
        entry["importId"] = card_id
    mapping = {
        "name_zh": "name_zh",
        "name_en": "name_en",
        "faction": "faction",
        "type": "type",
        "rarity": "rarity",
        "cost": "cost",
        "operation_cost": "operation_cost",
        "attack": "attack",
        "defense": "defense",
        "description": "description",
    }
    for source, target in mapping.items():
        value = fields.get(source)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        entry[target] = value

    keywords = _normalize_keywords(fields.get("keywords"))
    if keywords:
        entry["keywordsNormalized"] = keywords
    raw_tags = fields.get("tags")
    if isinstance(raw_tags, str):
        raw_tags = re.split(r"[,，;；、\n]+", raw_tags)
    tags: list[str] = []
    if isinstance(raw_tags, list):
        for tag in raw_tags:
            tag = str(tag or "").strip()
            if tag and tag not in tags:
                tags.append(tag)
    if tags:
        entry["tags"] = tags
    if not entry.get("name_zh") and not entry.get("name_en"):
        raise CardImportError("没有识别出卡牌名称，请换一张更清晰的卡图", 422)

    # Prefer updating an existing row when the card id is already known.
    if not existing_id:
        for import_id, row in deck_code.CARDS.items():
            if isinstance(row, list) and row and str(row[0]) == card_id:
                existing_id = import_id
                break
    if existing_id:
        entry["importId"] = existing_id
    elif not commit and re.fullmatch(r"[0-9A-Za-z]{2}", card_id):
        # A generated two-character short code is both the art id and the
        # import id until the user confirms and we sync it to the card table.
        entry["importId"] = card_id

    if not commit:
        draft_name = entry.get("name_zh") or entry.get("name_en") or card_id
        summary = {
            "added": 1,
            "updated": 0,
            "unchanged": 0,
            "total": 1,
            "cards": [{"id": entry.get("importId", card_id), "name": draft_name, "action": "added"}],
            "backup": "",
            "draft_entry": entry,
        }
    else:
        try:
            summary = manual_deck.import_cards([entry])
        except ValueError as exc:
            raise CardImportError(str(exc)) from exc
        except Exception as exc:
            logger.warning("Card image import failed: %s", exc)
            raise CardImportError(
                "写入卡表失败，请查看 AstrBot 日志", status_code=500
            ) from exc

    thumb_path = THUMB_DIR / f"{card_id.lower()}.jpg"
    try:
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        thumb_path.write_bytes(image_bytes)
    except OSError as exc:
        logger.warning("Card thumbnail write failed: %s", exc)

    summary.update(
        {
            "card_id": card_id,
            "import_id": existing_id or (summary.get("cards") or [{}])[0].get("id", ""),
            "provider": chosen,
            "extracted": {
                "name_zh": entry.get("name_zh", ""),
                "name_en": entry.get("name_en", ""),
                "faction": entry.get("faction", ""),
                "type": entry.get("type", ""),
                "rarity": entry.get("rarity", ""),
                "cost": entry.get("cost"),
                "attack": entry.get("attack"),
                "defense": entry.get("defense"),
                "description": entry.get("description", ""),
                "keywords": keywords,
                "tags": tags,
            },
            "thumb": f"manual/thumb?card_id={card_id}",
        }
    )
    return summary


def _temp_card_id() -> str:
    """Return an unused temporary id for an extra card in a batch."""
    while True:
        card_id = "DIY-" + secrets.token_hex(4).upper()
        if card_id not in deck_code.CARDS:
            return card_id


def _merge_recognition(results: list[dict]) -> dict:
    """Merge per-card import summaries into one response.

    Args:
        results: Summaries returned for each cropped card.

    Returns:
        Combined summary with a ``results`` list for all cards.
    """
    merged: dict = {
        "added": 0,
        "updated": 0,
        "unchanged": 0,
        "total": 0,
        "cards": [],
        "results": results,
        "errors": [],
        "backup": "",
        "draft_entries": [],
    }
    for result in results:
        if result.get("error"):
            merged["errors"].append(result["error"])
            continue
        for key in ("added", "updated", "unchanged"):
            merged[key] += int(result.get(key) or 0)
        merged["total"] = int(result.get("total") or merged["total"])
        merged["cards"].extend(result.get("cards") or [])
        if result.get("draft_entry"):
            merged["draft_entries"].append(result["draft_entry"])
        if result.get("backup"):
            merged["backup"] = result["backup"]
    first = next((item for item in results if not item.get("error")), {})
    for key in ("card_id", "import_id", "provider", "extracted", "thumb"):
        if key in first:
            merged[key] = first[key]
    return merged


async def recognize_card(
    image: object, card_id: object, provider: object = "", commit: bool = True
) -> dict:
    """Crop an upload into cards and recognize each card separately.

    Uses AI coordinate detection first, then local cropping, then recognition.

    Args:
        image: Raw image bytes or a ``data:image/...;base64,``/base64 string.
        card_id: Card art id supplied by the user for the first card.
        provider: Optional vision provider id; falls back to plugin config.
        commit: Write recognized cards to the table; False returns drafts only.

    Returns:
        Combined import summary with one entry per detected card.

    Raises:
        CardImportError: If the id, image, crop, or every recognition fails.
    """
    card_id = _clean_card_id(card_id)
    if not card_id:
        raise CardImportError("请填写卡牌 ID（只能包含字母、数字、下划线和短横线）")
    if _context is None:
        raise CardImportError("插件上下文未就绪，请重载插件", status_code=503)

    existing_id = ""
    if len(card_id) == 2 and card_id in deck_code.CARDS:
        existing_id = card_id
        card_id = str(deck_code.CARDS[card_id][0] or card_id)

    try:
        if isinstance(image, (bytes, bytearray)):
            image_bytes, _ = _normalize_image(bytes(image))
        else:
            image_bytes, _ = _decode_upload_image(image)

        # 使用 AI 模型检测卡图坐标（参考图不存在时不传，避免干扰模型）
        reference_card_path = os.path.join(
            os.path.dirname(__file__), "deck_card_thumbs", "reference.jpg"
        )
        if not os.path.exists(reference_card_path):
            reference_card_path = None

        coordinates = await card_crop.detect_card_coordinates(
            image_bytes,
            context=_context,
            provider=provider or DEFAULT_VISION_PROVIDER,
            reference_image_path=reference_card_path,
        )
        if not coordinates:
            logger.warning("AI 检测返回空坐标列表")
            raise CardImportError("未检测到卡图，请确保图片包含清晰的卡牌")
        else:
            logger.info("AI 检测到的坐标: %s", coordinates)
    except ValueError as exc:
        raise CardImportError(str(exc)) from exc

    # 手动裁剪卡图（使用与 crop_card_images 相同的逻辑）
    crops: list[bytes] = []
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            frames = getattr(source, "n_frames", 1)
            source.load()
            image_orig = source.convert("RGB")
    except (OSError, ValueError) as exc:
        raise CardImportError("无法识别该图片，请上传 JPG/PNG/WebP 卡图") from exc

    if frames > 1:
        raise CardImportError("动图不是卡图，请发送静态 JPG/PNG/WebP 卡图")
    if min(image_orig.size) < 200 or image_orig.width * image_orig.height < 40000:
        raise CardImportError("图片太小，无法识别卡图；请发送清晰完整的 JPG/PNG/WebP 卡图")

    # 对每个坐标进行裁剪。坐标已由 detect_card_coordinates 做过边缘精修，
    # 这里只保留 1 像素安全边，避免之前固定 5% padding 把背景一起带进来。
    for x0, y0, x1, y1 in coordinates:
        crop = image_orig.crop(
            (
                max(0, x0 - 1),
                max(0, y0 - 1),
                min(image_orig.width, x1 + 1),
                min(image_orig.height, y1 + 1),
            )
        )
        buffer = io.BytesIO()
        crop.save(buffer, "JPEG", quality=92, optimize=True)
        crops.append(buffer.getvalue())

    results: list[dict] = []
    for index, crop in enumerate(crops):
        current_id = card_id if index == 0 else _temp_card_id()
        try:
            result = await _recognize_one_card(
                crop,
                current_id,
                provider,
                existing_id if index == 0 else "",
                commit,
            )
            result["index"] = index + 1
            results.append(result)
        except CardImportError as exc:
            results.append(
                {
                    "index": index + 1,
                    "card_id": current_id,
                    "error": str(exc),
                    "extracted": {},
                    "cards": [],
                }
            )

    merged = _merge_recognition(results)
    if not any(not item.get("error") for item in results):
        raise CardImportError(
            (merged["errors"] or ["卡图识别失败"])[0], status_code=502
        )
    return merged


async def import_card_image():
    """Read a card image with the vision model and merge it into the table."""
    body = await request.json({})
    body = body if isinstance(body, dict) else {}
    try:
        summary = await recognize_card(
            body.get("image"),
            body.get("id") or body.get("card_id"),
            body.get("provider"),
        )
    except CardImportError as exc:
        return error_response(str(exc), status_code=exc.status_code)
    return json_response(summary)


ROUTES = [
    ("manual/catalog", get_catalog, ["GET"], "手动卡组：卡牌目录"),
    ("manual/aliases", get_aliases, ["GET"], "手动卡组：别名库"),
    ("manual/alias/add", add_alias, ["POST"], "手动卡组：添加别名"),
    ("manual/alias/remove", remove_alias, ["POST"], "手动卡组：删除别名"),
    ("manual/cards/fix", fix_card, ["POST"], "手动卡组：修改卡牌字段"),
    ("manual/cards/import", import_cards, ["POST"], "手动卡组：导入官方卡牌"),
    (
        "manual/cards/import-image",
        import_card_image,
        ["POST"],
        "手动卡组：上传卡图识别导入",
    ),
    (
        "manual/vision/providers",
        get_vision_providers,
        ["GET"],
        "手动卡组：视觉模型列表",
    ),
    ("manual/deck", get_deck, ["GET"], "手动卡组：当前卡组"),
    ("manual/add", add_card, ["POST"], "手动卡组：添加卡牌"),
    ("manual/remove", remove_card, ["POST"], "手动卡组：移除卡牌"),
    ("manual/set", set_factions, ["POST"], "手动卡组：设置阵营"),
    ("manual/clear", clear_deck, ["POST"], "手动卡组：清空"),
    ("manual/render", render_deck, ["POST"], "手动卡组：生成图片"),
    ("manual/thumb", card_thumb, ["GET"], "手动卡组：卡图"),
    ("manual/card-image", card_image_data, ["GET"], "手动卡组：单卡预览图"),
]


def register(context, config=None) -> None:
    """Register the page bridge routes on the AstrBot context.

    Args:
        context: The plugin ``Context`` instance.
        config: The plugin configuration dictionary.
    """
    global _context, _plugin_config
    _context = context
    _plugin_config = dict(config or {})
    register_api = getattr(context, "register_web_api", None)
    if not callable(register_api):
        logger.warning(
            "This AstrBot version does not support register_web_api; "
            "the manual deck page is unavailable."
        )
        return
    for prefix in ROUTE_PREFIXES:
        for route, handler, methods, desc in ROUTES:
            register_api(f"/{prefix}/{route}", handler, methods, desc)
    logger.info("Manual deck Web API routes registered")
