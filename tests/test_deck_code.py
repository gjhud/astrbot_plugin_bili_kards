"""Tests for the lightweight deck-code parser."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deck_code
from deck_code import (
    DeckCodeError,
    decode_deck_code,
    format_deck,
    keyword_label,
    normalize_deck_code,
)

CODE = "%%21|at;4g5h;;112b"


class DeckCodeTest(unittest.TestCase):
    def test_normalize_accepts_link_locale_separator_and_save_code(self):
        self.assertEqual(normalize_deck_code(CODE), CODE)
        self.assertEqual(
            normalize_deck_code("https://example.com/#deck_code=" + CODE), CODE
        )
        self.assertEqual(normalize_deck_code(CODE.replace("|", "～")), CODE)
        self.assertEqual(normalize_deck_code(CODE + "~123~abc"), CODE)

    def test_decode_counts_and_factions(self):
        deck = decode_deck_code(CODE)
        self.assertEqual(deck.main_faction, "Britain")
        self.assertEqual(deck.ally_faction, "Germany")
        self.assertEqual(deck.total, 13)
        self.assertEqual(len(deck.cards), 5)
        self.assertTrue(deck.warnings)

    def test_missing_separator_is_rejected(self):
        with self.assertRaises(DeckCodeError):
            decode_deck_code("%%21;at;4g5h;;112b")

    def test_format_includes_stats(self):
        text = format_deck(decode_deck_code(CODE))
        self.assertIn("KARDS 卡组代码解析", text)
        self.assertIn("第 1 空降旅", text)
        self.assertIn("费用曲线", text)

    def test_keyword_labels_use_official_chinese_names(self):
        self.assertEqual(keyword_label("fury"), "奋战")
        self.assertEqual(keyword_label("salvage"), "收缴")
        self.assertEqual(keyword_label("狂怒"), "奋战")
        self.assertEqual(keyword_label("狂热"), "奋战")
        self.assertEqual(keyword_label("回收"), "收缴")
        self.assertEqual(keyword_label("heavyArmor:2"), "重甲 2")

    def test_rarity_labels_and_aliases(self):
        self.assertEqual(deck_code.RARITY_CN["Standard"], "普通")
        self.assertEqual(deck_code.RARITY_ALIASES["标准"], "Standard")
        self.assertEqual(deck_code.RARITY_ALIASES["普通"], "Standard")

    def test_card_table_loaded_from_data_dir(self):
        self.assertGreater(len(deck_code.CARDS), 1000)
        self.assertIn("11", deck_code.CARDS)


if __name__ == "__main__":
    unittest.main()
