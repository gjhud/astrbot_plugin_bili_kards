"""Detect and crop KARDS card images before vision recognition.

An uploaded screenshot may contain borders, UI text, or several cards.  This
module uses only Pillow and NumPy to find rectangular regions with a card-like
aspect ratio and returns them as separate JPEG images so the vision model sees
one card at a time.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os

import numpy as np
from PIL import Image

from astrbot.api import logger

CARD_RATIO = 0.715
DETECT_MAX_SIDE = 600
MIN_CARD_COVERAGE = 0.45
MAX_CARDS = 12
# AI gives a rough box; search this fraction of each side around it for the
# real card border. Keep the search bounded so a bad box cannot wander into
# unrelated UI elements.
REFINE_SEARCH_RATIO = 0.10
MAX_REFINE_PX = 80
# KARDS cards are close to this ratio. Only correct large deviations so
# slightly tilted/photographed cards are not forcibly distorted.
RATIO_TOLERANCE = 0.12


def _dilate(mask: np.ndarray, iterations: int = 2) -> np.ndarray:
    for _ in range(iterations):
        grown = mask.copy()
        grown[:, 1:] |= mask[:, :-1]
        grown[:, :-1] |= mask[:, 1:]
        grown[1:, :] |= mask[:-1, :]
        grown[:-1, :] |= mask[1:, :]
        mask = grown
    return mask


def _content_mask(image: Image.Image) -> np.ndarray:
    arr = np.asarray(image, dtype=np.float32) / 255.0
    gray = arr.mean(axis=2)
    grad_x = np.zeros_like(gray)
    grad_y = np.zeros_like(gray)
    grad_x[:, 1:] = np.abs(np.diff(gray, axis=1))
    grad_y[1:, :] = np.abs(np.diff(gray, axis=0))
    maximum = arr.max(axis=2)
    minimum = arr.min(axis=2)
    saturation = (maximum - minimum) / (maximum + 0.02)
    return _dilate((np.maximum(grad_x, grad_y) > 0.06) | (saturation > 0.5))


def _coerce_box(box: object, size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """Normalize a model-provided box to clamped pixel coordinates."""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in box):
        return None
    width, height = size
    x1, y1, x2, y2 = [float(v) for v in box]
    values = (x1, y1, x2, y2)
    # 模型可能返回 0-1 比例，也可能直接返回像素坐标
    if all(-0.01 <= v <= 1.01 for v in values):
        x1, x2 = x1 * width, x2 * width
        y1, y2 = y1 * height, y2 * height
    left, right = sorted((x1, x2))
    top, bottom = sorted((y1, y2))
    left = max(0, min(width - 1, int(left)))
    right = max(0, min(width, int(right)))
    top = max(0, min(height - 1, int(top)))
    bottom = max(0, min(height, int(bottom)))
    box_w, box_h = right - left, bottom - top
    if box_w < 20 or box_h < 20:
        return None
    ratio = box_w / box_h
    if not 0.55 <= ratio <= 0.95:
        logger.warning("AI 检测框比例 %.3f 超出 0.55-0.95，丢弃", ratio)
        return None
    return (left, top, right, bottom)


def _vertical_edge_score(
    arr: np.ndarray, x: int, y0: int, y1: int
) -> float:
    """Score how likely column x is the left/right border of a card."""
    height, width, _ = arr.shape
    if y1 <= y0 or x < 0 or x >= width:
        return -1.0
    strip = arr[y0:y1]
    left = strip[:, max(0, x - 1)]
    right = strip[:, min(width - 1, x + 1)]
    column_gradients = np.abs(right - left)
    gradient = float(column_gradients.mean())
    # A real card border is a long, consistent edge. Interior artwork can be
    # brighter locally but is not continuous from top to bottom.
    consistency = float(
        np.mean(column_gradients > max(0.035, gradient * 0.6))
    )
    gradient = gradient * (0.4 + 0.6 * consistency)
    inner_x0, inner_x1 = max(0, x + 2), min(width, x + 9)
    outer_x0, outer_x1 = max(0, x - 9), min(width, x - 1)
    color = 0.0
    if inner_x1 > inner_x0 and outer_x1 > outer_x0:
        inner = strip[:, inner_x0:inner_x1].mean(axis=(0, 1))
        outer = strip[:, outer_x0:outer_x1].mean(axis=(0, 1))
        color = float(np.abs(inner - outer).mean())
    return gradient + 0.35 * color


def _horizontal_edge_score(
    arr: np.ndarray, y: int, x0: int, x1: int
) -> float:
    """Score how likely row y is the top/bottom border of a card."""
    height, width, _ = arr.shape
    if x1 <= x0 or y < 0 or y >= height:
        return -1.0
    strip = arr[:, x0:x1]
    above = strip[max(0, y - 1), :]
    below = strip[min(height - 1, y + 1), :]
    row_gradients = np.abs(below - above)
    gradient = float(row_gradients.mean())
    consistency = float(np.mean(row_gradients > max(0.035, gradient * 0.6)))
    gradient = gradient * (0.4 + 0.6 * consistency)
    inner_y0, inner_y1 = max(0, y + 2), min(height, y + 9)
    outer_y0, outer_y1 = max(0, y - 9), min(height, y - 1)
    color = 0.0
    if inner_y1 > inner_y0 and outer_y1 > outer_y0:
        inner = strip[inner_y0:inner_y1, :].mean(axis=(0, 1))
        outer = strip[outer_y0:outer_y1, :].mean(axis=(0, 1))
        color = float(np.abs(inner - outer).mean())
    return gradient + 0.35 * color


def _best_edge(
    rough: int,
    low: int,
    high: int,
    scorer,
) -> int:
    """Pick the strongest border near a rough coordinate."""
    span = max(1, high - low)
    best = rough
    best_score = float("-inf")
    for position in range(low, high + 1):
        score = scorer(position) - 0.12 * abs(position - rough) / span
        if score > best_score:
            best_score = score
            best = position
    return best


def _correct_ratio(
    left: int,
    top: int,
    right: int,
    bottom: int,
    image_size: tuple[int, int],
) -> tuple[int, int, int, int]:
    """Nudge a box toward the KARDS ratio only when it is clearly off."""
    width_px, height_px = image_size
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return left, top, right, bottom

    target_width = height * CARD_RATIO
    if width > target_width * (1 + RATIO_TOLERANCE):
        new_width = max(20, int(target_width * (1 + RATIO_TOLERANCE / 2)))
        delta = (width - new_width) // 2
        left, right = left + delta, right - delta
    elif width < target_width * (1 - RATIO_TOLERANCE):
        new_width = int(target_width * (1 - RATIO_TOLERANCE / 2))
        delta = max(0, (new_width - width) // 2)
        left, right = max(0, left - delta), min(width_px, right + delta)

    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return left, top, right, bottom
    target_height = width / CARD_RATIO
    if height > target_height * (1 + RATIO_TOLERANCE):
        new_height = max(20, int(target_height * (1 + RATIO_TOLERANCE / 2)))
        delta = (height - new_height) // 2
        top, bottom = top + delta, bottom - delta
    elif height < target_height * (1 - RATIO_TOLERANCE):
        new_height = int(target_height * (1 - RATIO_TOLERANCE / 2))
        delta = max(0, (new_height - height) // 2)
        top, bottom = max(0, top - delta), min(height_px, bottom + delta)

    return (
        max(0, min(width_px - 1, int(left))),
        max(0, min(height_px - 1, int(top))),
        max(1, min(width_px, int(right))),
        max(1, min(height_px, int(bottom))),
    )


def refine_box(
    image: Image.Image, box: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    """Snap a rough card box to the nearest visible border.

    The AI detector is good at locating a card but often adds a margin or
    stops at an inner frame. This searches a small window around each side for
    the strongest colour/gradient edge, then makes a conservative aspect-ratio
    correction.
    """
    image = image.convert("RGB")
    width_px, height_px = image.size
    left, top, right, bottom = box
    box_w, box_h = right - left, bottom - top
    if box_w < 40 or box_h < 40:
        return box

    arr = np.asarray(image, dtype=np.float32) / 255.0
    search_x = max(4, min(MAX_REFINE_PX, int(box_w * REFINE_SEARCH_RATIO)))
    search_y = max(4, min(MAX_REFINE_PX, int(box_h * REFINE_SEARCH_RATIO)))

    margin_y = max(1, int(box_h * 0.12))
    margin_x = max(1, int(box_w * 0.12))
    y0, y1 = top + margin_y, bottom - margin_y
    x0, x1 = left + margin_x, right - margin_x
    if y1 - y0 < 10:
        y0, y1 = top, bottom
    if x1 - x0 < 10:
        x0, x1 = left, right

    new_left = _best_edge(
        left,
        max(0, left - search_x),
        min(width_px - 1, left + search_x),
        lambda x: _vertical_edge_score(arr, x, y0, y1),
    )
    new_right = _best_edge(
        right,
        max(0, right - search_x),
        min(width_px - 1, right + search_x),
        lambda x: _vertical_edge_score(arr, x, y0, y1),
    )
    new_top = _best_edge(
        top,
        max(0, top - search_y),
        min(height_px - 1, top + search_y),
        lambda y: _horizontal_edge_score(arr, y, x0, x1),
    )
    new_bottom = _best_edge(
        bottom,
        max(0, bottom - search_y),
        min(height_px - 1, bottom + search_y),
        lambda y: _horizontal_edge_score(arr, y, x0, x1),
    )

    # A weak/ambiguous edge can make the search jump to an internal frame.
    # Reject implausible axes instead of returning a broken crop.
    if new_right - new_left < box_w * 0.6:
        new_left, new_right = left, right
    if new_bottom - new_top < box_h * 0.6:
        new_top, new_bottom = top, bottom

    refined = _correct_ratio(
        new_left, new_top, new_right, new_bottom, image.size
    )
    logger.debug("card box refined: %s -> %s", box, refined)
    return refined


def _dedupe_boxes(boxes: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Keep the largest non-duplicate boxes."""
    ordered = sorted(
        boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True
    )
    kept: list[tuple[int, int, int, int]] = []
    for box in ordered:
        bx0, by0, bx1, by1 = box
        duplicated = False
        for ox0, oy0, ox1, oy1 in kept:
            iw = min(bx1, ox1) - max(bx0, ox0)
            ih = min(by1, oy1) - max(by0, oy0)
            if iw > 0 and ih > 0:
                area = max(1, (bx1 - bx0) * (by1 - by0))
                if iw * ih / area > 0.6:
                    duplicated = True
                    break
        if not duplicated:
            kept.append(box)
        if len(kept) >= MAX_CARDS:
            break
    return kept


def _fallback_boxes(image: Image.Image) -> list[tuple[int, int, int, int]]:
    """传统检测；仍检测不到时把整张图作为单卡处理。"""
    boxes = _detect_card_coordinates_legacy(image)
    if boxes:
        return [refine_box(image, box) for box in boxes]
    return [refine_box(image, (0, 0, image.width, image.height))]


async def detect_card_coordinates(
    image_bytes: bytes,
    context: object = None,
    provider: str = "openai-responses/Sage-S2.1-Flash",
    reference_image_path: str = None,
) -> list[tuple[int, int, int, int]]:
    """Detect card locations in an image using AI model and return their coordinates.

    Args:
        image_bytes: Image bytes in any format Pillow can read.
        context: LLM provider context for calling vision models.
        provider: Vision provider ID to use for detection.
        reference_image_path: Path to a reference card image for visual guidance.

    Returns:
        List of tuples (x0, y0, x1, y1) representing card bounding boxes
        in the original image coordinates.
    """
    # 先加载图片，验证格式
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            frames = getattr(source, "n_frames", 1)
            source.load()
            image = source.convert("RGB")
    except (OSError, ValueError) as exc:
        raise ValueError("无法识别该图片，请上传 JPG/PNG/WebP 卡图") from exc

    if frames > 1:
        raise ValueError("动图不是卡图，请发送静态 JPG/PNG/WebP 卡图")
    if min(image.size) < 200 or image.width * image.height < 40000:
        raise ValueError("图片太小，无法识别卡图；请发送清晰完整的 JPG/PNG/WebP 卡图")

    # 使用 AI 模型检测坐标
    if context is None:
        # 如果没有 context，回退到传统检测
        return _detect_card_coordinates_legacy(image)

    # 将待检测图片编码为 base64
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=95, optimize=True)
    image_data = base64.b64encode(buffer.getvalue()).decode("utf-8")
    image_url = f"data:image/jpeg;base64,{image_data}"

    # 准备参考图片
    reference_urls = []
    if reference_image_path and os.path.exists(reference_image_path):
        try:
            with Image.open(reference_image_path) as ref_img:
                ref_buffer = io.BytesIO()
                ref_img.convert("RGB").save(ref_buffer, "JPEG", quality=95, optimize=True)
                ref_data = base64.b64encode(ref_buffer.getvalue()).decode("utf-8")
                reference_urls.append(f"data:image/jpeg;base64,{ref_data}")
        except Exception as e:
            logger.warning("加载参考卡图失败: %s", e)

    # 构建 prompt。不要给模型固定的 15%/85% 示例，否则它会照抄示例比例，
    # 而不是按图片里卡牌的真实边缘定位。
    prompt = (
        f"你是一个精确的卡图标注助手。图片尺寸为 {image.width}x{image.height} 像素。\n"
        "请检测图片中所有完整可见的 KARDS 卡牌正面，并返回每张卡牌最外层可见边缘的边界框。\n"
        "只输出一个 JSON 数组，不要包含任何其他文字、解释或 Markdown。\n"
        "格式：[[x1, y1, x2, y2], ...]\n"
        "  - (x1, y1) 是卡牌左上角，(x2, y2) 是卡牌右下角\n"
        "  - 坐标系原点 (0, 0) 在图片左上角，x 向右、y 向下\n"
        "  - 坐标是相对于图片宽高归一化的比例值（0.0-1.0），保留四位小数即可\n"
        "要求：\n"
        "  - 边界必须贴合卡牌最外层边框，包含卡名、费用、插画、描述文本和攻防数值\n"
        "  - 不要把周围背景、桌面、阴影、手机 UI 或聊天窗口算进边界\n"
        "  - 也不要只框住内部插画或内部装饰框\n"
        "  - 不要套用任何固定比例或固定位置，完全以这张图片里的实际卡牌为准\n"
        "如果图片中没有完整卡牌，返回 []。\n"
    )

    # 合并所有图片 URL
    all_image_urls = [image_url] + reference_urls

    try:
        logger.info("调用 vision model 检测卡图坐标，provider=%s, 图片尺寸=%dx%d", provider, image.width, image.height)
        response = await asyncio.wait_for(
            context.llm_generate(
                chat_provider_id=provider,
                prompt=prompt,
                image_urls=all_image_urls,
            ),
            timeout=30,
        )

        text = getattr(response, "completion_text", "") or ""
        logger.info("模型原始响应: %s", text[:500])
        # 解析 JSON 数组
        try:
            # 移除可能的 markdown 代码块标记
            text = text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.startswith("```"):
                text = text[3:]
            text = text.strip()
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

            boxes = json.loads(text)
            logger.info("解析到的坐标框: %s", boxes)
            if isinstance(boxes, list):
                raw_boxes = [
                    b for b in (_coerce_box(box, image.size) for box in boxes) if b
                ]
                refined_boxes = [refine_box(image, box) for box in raw_boxes]
                result = _dedupe_boxes(refined_boxes)
                logger.info("精修后的坐标: %s", refined_boxes)
                logger.info("最终返回的坐标: %s", result)
                if result:
                    return result
                logger.warning("AI 未返回有效卡牌框，回退到本地检测")
        except json.JSONDecodeError:
            logger.warning("模型返回内容不是合法 JSON，回退到本地检测")

        return _fallback_boxes(image)

    except Exception as exc:
        logger.warning("视觉模型检测失败，回退到本地检测: %s", exc)
        return _fallback_boxes(image)


def _detect_card_coordinates_legacy(image: Image.Image) -> list[tuple[int, int, int, int]]:
    """传统检测算法（当 AI 不可用时使用）"""
    # 使用与 crop_card_images 相同的检测逻辑
    scale = min(1.0, DETECT_MAX_SIDE / max(image.size))
    small = image
    if scale < 1.0:
        small = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.LANCZOS,
        )

    mask = _content_mask(small)
    boxes = _component_boxes(mask)
    if not boxes:
        return []

    # 将小图的坐标转换回原图坐标
    result = []
    for x0, y0, x1, y1 in boxes:
        left = int(x0 / scale)
        top = int(y0 / scale)
        right = int(x1 / scale)
        bottom = int(y1 / scale)
        result.append((left, top, right, bottom))
    return result


def _component_boxes(mask: np.ndarray) -> list[tuple[int, int, int, int]]:
    height, width = mask.shape
    parent: list[int] = []
    rank: list[int] = []
    runs: list[tuple[int, int, int, int, int]] = []

    def find(label: int) -> int:
        while parent[label] != label:
            parent[label] = parent[parent[label]]
            label = parent[label]
        return label

    def union(left: int, right: int) -> None:
        a, b = find(left), find(right)
        if a == b:
            return
        if rank[a] < rank[b]:
            a, b = b, a
        parent[b] = a
        if rank[a] == rank[b]:
            rank[a] += 1

    previous: list[tuple[int, int, int]] = []
    for y in range(height):
        padded = np.concatenate(([False], mask[y], [False]))
        starts = np.flatnonzero(padded[1:] & ~padded[:-1])
        ends = np.flatnonzero(~padded[1:] & padded[:-1])
        current: list[tuple[int, int, int]] = []
        for x0, x1 in zip(starts, ends):
            label = -1
            for prev_label, prev_x0, prev_x1 in previous:
                if prev_x0 <= x1 and x0 <= prev_x1:
                    if label < 0:
                        label = prev_label
                    else:
                        union(label, prev_label)
            if label < 0:
                label = len(parent)
                parent.append(label)
                rank.append(0)
            current.append((label, int(x0), int(x1)))
            runs.append((label, int(x0), int(x1), y, int(x1 - x0)))
        previous = current

    stats: dict[int, list[int]] = {}
    for label, x0, x1, y, count in runs:
        root = find(label)
        stat = stats.setdefault(root, [x0, y, x1, y + 1, 0])
        stat[0] = min(stat[0], x0)
        stat[1] = min(stat[1], y)
        stat[2] = max(stat[2], x1)
        stat[3] = max(stat[3], y + 1)
        stat[4] += count

    boxes: list[tuple[int, int, int, int]] = []
    for x0, y0, x1, y1, count in stats.values():
        box_w, box_h = x1 - x0, y1 - y0
        if box_w < width * 0.08 or box_h < height * 0.1:
            continue
        ratio = box_w / box_h
        if not 0.58 <= ratio <= 0.88:
            continue
        if count / (box_w * box_h) < MIN_CARD_COVERAGE:
            continue
        boxes.append((x0, y0, x1, y1))
    if not boxes:
        return boxes
    boxes.sort(key=lambda box: (box[2] - box[0]) * (box[3] - box[1]), reverse=True)
    kept: list[tuple[int, int, int, int]] = []
    for box in boxes[:MAX_CARDS]:
        if any(
            min(box[2], other[2]) > max(box[0], other[0])
            and min(box[3], other[3]) > max(box[1], other[1])
            for other in kept
        ):
            continue
        kept.append(box)
    return kept


def crop_card_images(raw: bytes) -> list[bytes]:
    """Split an upload into one cropped JPEG per detected card.

    Args:
        raw: Original image bytes in any format Pillow can read.

    Returns:
        JPEG bytes for each detected card.  Returns the normalized whole image
        when no card-like region is found.

    Raises:
        ValueError: If the bytes are empty or not a readable image.
    """
    if not raw:
        raise ValueError("卡图内容为空")
    try:
        with Image.open(io.BytesIO(raw)) as source:
            frames = getattr(source, "n_frames", 1)
            source.load()
            image = source.convert("RGB")
    except (OSError, ValueError) as exc:
        raise ValueError("无法识别该图片，请上传 JPG/PNG/WebP 卡图") from exc

    if frames > 1:
        raise ValueError("动图不是卡图，请发送静态 JPG/PNG/WebP 卡图")
    if min(image.size) < 200 or image.width * image.height < 40000:
        raise ValueError("图片太小，无法识别卡图；请发送清晰完整的 JPG/PNG/WebP 卡图")

    scale = min(1.0, DETECT_MAX_SIDE / max(image.size))
    small = image
    if scale < 1.0:
        small = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.LANCZOS,
        )

    boxes = _component_boxes(_content_mask(small))
    if not boxes:
        boxes = [(0, 0, small.width, small.height)]
    boxes = [refine_box(small, box) for box in boxes]

    crops: list[bytes] = []
    for x0, y0, x1, y1 in boxes:
        left = int(x0 / scale)
        top = int(y0 / scale)
        right = int(x1 / scale)
        bottom = int(y1 / scale)
        pad_x = max(1, int((right - left) * 0.01))
        pad_y = max(1, int((bottom - top) * 0.01))
        crop = image.crop(
            (
                max(0, left - pad_x),
                max(0, top - pad_y),
                min(image.width, right + pad_x),
                min(image.height, bottom + pad_y),
            )
        )
        buffer = io.BytesIO()
        crop.save(buffer, "JPEG", quality=92, optimize=True)
        crops.append(buffer.getvalue())
    return crops
