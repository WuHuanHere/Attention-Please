"""纯逻辑单测: 窗口标题判定(M2)。

跑法: .\\.venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.wordlist import TitleVerdict, classify  # noqa: E402

WHITE = ["Visual Studio Code", "- Code", "百度网盘", "考研", "单词", "Bilibili 课堂"]
BLACK = ["哔哩哔哩", "bilibili", "微信", "Steam", "小说"]


class TestClassify(unittest.TestCase):
    def test_empty_title_is_unknown(self):
        for t in (None, "", "   "):
            self.assertIs(classify(t, WHITE, BLACK).verdict, TitleVerdict.UNKNOWN)

    def test_whitelist_hit_is_focus(self):
        m = classify("main.py - Visual Studio Code", WHITE, BLACK)
        self.assertIs(m.verdict, TitleVerdict.FOCUS)
        self.assertEqual(m.source, "whitelist")

    def test_blacklist_hit_is_distraction(self):
        m = classify("哔哩哔哩 (゜-゜)つロ 干杯~", WHITE, BLACK)
        self.assertIs(m.verdict, TitleVerdict.DISTRACT)
        self.assertTrue(m.is_distraction)
        self.assertEqual(m.matched, "哔哩哔哩")

    def test_whitelist_wins_over_blacklist(self):
        """B站网课必须算专注 —— 否则你一天会被念几十次。"""
        m = classify("考研数学强化 - Bilibili 课堂", WHITE, BLACK)
        self.assertIs(m.verdict, TitleVerdict.FOCUS)

    def test_case_insensitive(self):
        m = classify("STEAM COMMUNITY", WHITE, BLACK)
        self.assertIs(m.verdict, TitleVerdict.DISTRACT)

    def test_unknown_is_not_distraction(self):
        """按你选的"浏览器默认算专注": 新标签页/查资料不该被念。"""
        m = classify("新标签页 - Google Chrome", WHITE, BLACK)
        self.assertIs(m.verdict, TitleVerdict.UNKNOWN)
        self.assertFalse(m.is_distraction)

    def test_whitespace_word_is_ignored(self):
        m = classify("随便什么窗口", ["  "], [])
        self.assertIs(m.verdict, TitleVerdict.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
