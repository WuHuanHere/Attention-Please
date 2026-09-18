"""静态检查: 别把 pack()/grid() 的元组选项写进 tk 控件构造函数。

`tk.Frame(..., pady=(0, 10))` 会抛 `TclError: bad screen distance "0 10"` ——
而且是在窗口**已经画出来之后**才抛, 所以表现为"第一次打开看起来正常, 之后整个 UI 全死"
(2026-09-17 的真实事故: 日报窗口画完正文后构造底部按钮栏时炸掉, 轮询链永久断掉)。

这条测试一秒钟就能跑、不需要窗口, 所以能进常规测试套件 —— 真正的窗口版回归在
tests/test_ui_lifecycle.py(需要 AP_UI_TESTS=1)。
"""
from __future__ import annotations

import pathlib
import re
import unittest

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "attention_please"

WIDGET_CTOR = re.compile(r"tk\.(Frame|Label|Button|Toplevel|Text|Entry|Canvas|Checkbutton|Menu)\s*\(")
TUPLE_PADDING = re.compile(r"\b(?:padx|pady)\s*=\s*\(")


def call_spans(text: str, start: int) -> int:
    """返回从 start(左括号位置)起配对的右括号下标。"""
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
    return len(text)


class TestNoTuplePaddingInConstructors(unittest.TestCase):
    def test_all_widget_constructors(self):
        offenders: list[str] = []
        for path in sorted(SRC.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for match in WIDGET_CTOR.finditer(text):
                open_paren = match.end() - 1
                close = call_spans(text, open_paren)
                body = text[open_paren:close]
                if TUPLE_PADDING.search(body):
                    line = text[:match.start()].count("\n") + 1
                    snippet = " ".join(body.split())[:90]
                    offenders.append(f"{path.name}:{line}  {snippet}")
        self.assertEqual(
            offenders, [],
            "控件的 padx/pady 只接受整数, 元组是 pack()/grid() 的选项。\n"
            "写成构造函数参数会在窗口画出来之后抛 TclError, 导致整个 UI 永久失效。\n"
            "把元组挪到 .pack(...) / .grid(...) 里:\n  " + "\n  ".join(offenders))

    def test_scanner_actually_detects_the_known_bad_pattern(self):
        """自检: 这条规则本身要能抓到当年那行代码。"""
        bad = 'bar = tk.Frame(win, bg="#1b1b1b", padx=12, pady=(0, 10))'
        m = WIDGET_CTOR.search(bad)
        self.assertIsNotNone(m)
        body = bad[m.end() - 1:call_spans(bad, m.end() - 1)]
        self.assertTrue(TUPLE_PADDING.search(body))

    def test_scanner_accepts_valid_usage(self):
        good = 'bar.pack(fill="x", pady=(0, 10))\nw = tk.Frame(win, padx=12, pady=10)'
        for m in WIDGET_CTOR.finditer(good):
            body = good[m.end() - 1:call_spans(good, m.end() - 1)]
            self.assertFalse(TUPLE_PADDING.search(body))


if __name__ == "__main__":
    unittest.main()
