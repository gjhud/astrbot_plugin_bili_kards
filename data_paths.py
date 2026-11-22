"""Persistent storage paths for the bilikards plugin.

All runtime data is stored under ``StarTools.get_data_dir("bilikards")``,
which resolves to ``data/plugin_data/bilikards``.  The plugin directory keeps
only code, bundled seed assets and WebUI files.  Legacy files under the plugin
directory are migrated once when this module is first imported.
"""

from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path

PLUGIN_NAME = "bilikards"
PLUGIN_DIR = Path(__file__).resolve().parent


def _resolve_data_dir() -> Path:
    """Resolve the plugin data directory.

    Returns:
        Absolute path to ``data/plugin_data/bilikards``.
    """
    # AstrBot modules are already loaded when this file is imported by the
    # plugin.  Standalone tools/tests use the relative fallback instead.
    if any(name == "astrbot" or name.startswith("astrbot.") for name in sys.modules):
        try:
            from astrbot.api.star import StarTools

            return Path(StarTools.get_data_dir(PLUGIN_NAME)).resolve()
        except Exception:
            pass
    # Standalone fallback: <AstrBot>/data/plugins/<plugin> -> <AstrBot>/data.
    fallback = PLUGIN_DIR.parent.parent / "plugin_data" / PLUGIN_NAME
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback.resolve()


DATA_DIR = _resolve_data_dir()

DECK_FILE = DATA_DIR / "manual_deck.json"
DECK_DB = DATA_DIR / "manual_decks.sqlite3"
ALIAS_FILE = DATA_DIR / "card_aliases.json"
CARDS_FILE = DATA_DIR / "deck_cards.json"
PENDING_IMPORT_FILE = DATA_DIR / "pending_imports.json"
NEW_CARDS_FILE = DATA_DIR / "new_cards.json"

THUMB_DIR = DATA_DIR / "deck_card_thumbs"
CARD_CACHE_DIR = DATA_DIR / "deck_card_cache"
IMAGE_CACHE_DIR = DATA_DIR / "deck_images"
PICTURE_DIR = DATA_DIR / "picture"
MONITOR_DIR = DATA_DIR / "monitor"
TEMP_DIR = DATA_DIR / "temp"

BLANK_IMAGE = PICTURE_DIR / "blank.png"
MIGRATION_MARKER = DATA_DIR / ".storage_migrated_v1"


def _unique_target(path: Path) -> Path:
    """Return a non-clobbering destination for a migrated file or directory.

    Args:
        path: Preferred destination path.

    Returns:
        A path that does not exist yet.
    """
    if not path.exists():
        return path
    suffix = f"legacy_{uuid.uuid4().hex[:8]}"
    return path.with_name(f"{path.name}.{suffix}")


def _merge_move(source: Path, destination: Path) -> None:
    """Move children of ``source`` into ``destination`` without overwriting.

    Args:
        source: Legacy directory to drain.
        destination: Target directory inside the plugin data directory.
    """
    destination.mkdir(parents=True, exist_ok=True)
    for item in list(source.iterdir()):
        target = _unique_target(destination / item.name)
        try:
            shutil.move(str(item), str(target))
        except OSError:
            continue
    try:
        source.rmdir()
    except OSError:
        pass


def _ensure_cards_file() -> None:
    """Make sure the runtime card table exists in the plugin data directory."""
    if CARDS_FILE.is_file() and CARDS_FILE.stat().st_size > 0:
        return
    CARDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    seed = PLUGIN_DIR / "deck_cards.json"
    if seed.is_file():
        shutil.copy2(seed, CARDS_FILE)
    else:
        CARDS_FILE.write_text('{"meta": {}, "cards": {}}', encoding="utf-8")


def initialize() -> None:
    """Create the data directory and migrate legacy plugin-directory data."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not MIGRATION_MARKER.exists():
        legacy_data = PLUGIN_DIR / "data"
        if legacy_data.is_dir():
            _merge_move(legacy_data, DATA_DIR)
        legacy_picture = PLUGIN_DIR / "picture"
        if legacy_picture.is_dir():
            _merge_move(legacy_picture, PICTURE_DIR)
        try:
            MIGRATION_MARKER.write_text("v1\n", encoding="utf-8")
        except OSError:
            pass
    _ensure_cards_file()


initialize()
