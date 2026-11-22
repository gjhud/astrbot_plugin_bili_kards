"""Detect and crop KARDS card images before vision recognition.

An uploaded screenshot may contain borders, UI text, or several cards.  This
module uses only Pillow and NumPy to find rectangular regions with a card-like
aspect ratio and returns them as separate JPEG images so the vision model sees
one card at a time.
"""

from __future__ import annotations

import io

import numpy as np
from PIL import Image

CARD_RATIO = 0.715
DETECT_MAX_SIDE = 600
MIN_CARD_COVERAGE = 0.45
MAX_CARDS = 12


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
            source.load()
            image = source.convert("RGB")
    except (OSError, ValueError) as exc:
        raise ValueError("无法识别该图片，请上传 JPG/PNG/WebP 卡图") from exc

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

    crops: list[bytes] = []
    for x0, y0, x1, y1 in boxes:
        left = int(x0 / scale)
        top = int(y0 / scale)
        right = int(x1 / scale)
        bottom = int(y1 / scale)
        pad_x = max(0, int((right - left) * 0.02))
        pad_y = max(0, int((bottom - top) * 0.02))
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
