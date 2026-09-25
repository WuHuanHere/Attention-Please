"""日报预览渲染: markdown -> 块 -> 表格排版(纯逻辑, 不需要窗口)。

用户 2026-09-25 的原话:「可以将专注日报界面的内容渲染成 md 的预览的画面吗, 不然这个
表格不太方便看」。原来查看器只是把 `**`/`` ` ``/`#` 删掉后原样显示, 八列宽的表变成
一堆对不齐的 `| a | b |`。

这一层守三件事:
  1. 日报里用到的 markdown 子集**全都认得**(标题/引用/列表/表格/分隔线/行内强调);
  2. **认不出来的一律当普通段落** —— 宁可显示得朴素, 也不能因为格式没料到就吞内容;
  3. 表格排版按**真实字体度量**算列宽(汉字自然就是两个西文字符宽), 不靠空格凑。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please import mdview  # noqa: E402
from attention_please.config import Config  # noqa: E402
from attention_please.report import build_report  # noqa: E402
from attention_please.store import Store  # noqa: E402


def fake_measure(text: str) -> int:
    """假的字体度量: 汉字算 2 个西文字符宽(真渲染时用的是 Font.measure)。"""
    return sum(2 if ord(c) > 0x2000 else 1 for c in text) * 8


class TestSpans(unittest.TestCase):
    def test_plain_text_is_one_span(self):
        self.assertEqual(mdview.parse_spans("普通文字"), [mdview.Span("普通文字")])

    def test_bold_code_italic(self):
        spans = mdview.parse_spans("有 **粗** 和 `码` 和 *斜* 三种")
        self.assertEqual([(s.text, s.bold, s.code, s.italic) for s in spans],
                         [("有 ", False, False, False),
                          ("粗", True, False, False),
                          (" 和 ", False, False, False),
                          ("码", False, True, False),
                          (" 和 ", False, False, False),
                          ("斜", False, False, True),
                          (" 三种", False, False, False)])

    def test_asterisk_that_is_not_emphasis_stays_literal(self):
        """`2 * 3` 里的星号没有配对的另一半, 不能当成斜体记号吃掉。"""
        self.assertEqual(mdview.span_text(mdview.parse_spans("2 * 3 = 6")), "2 * 3 = 6")

    def test_bold_wins_over_the_asterisks_inside_it(self):
        spans = mdview.parse_spans("**a*b*c**")
        self.assertEqual(len(spans), 1)
        self.assertTrue(spans[0].bold)
        self.assertEqual(spans[0].text, "a*b*c")

    def test_empty_text(self):
        self.assertEqual(mdview.span_text(mdview.parse_spans("")), "")


class TestBlocks(unittest.TestCase):
    def test_headings_keep_their_level(self):
        blocks = mdview.parse("# 一级\n\n### 三级")
        self.assertEqual([(type(b).__name__, b.level) for b in blocks],
                         [("Heading", 1), ("Heading", 3)])

    def test_bullets_and_indentation(self):
        blocks = mdview.parse("- 顶层\n  - 缩进一层\n- 又一个顶层")
        self.assertEqual([(b.level, mdview.span_text(b.spans)) for b in blocks],
                         [(0, "顶层"), (1, "缩进一层"), (0, "又一个顶层")])

    def test_each_quote_line_is_its_own_block(self):
        """日报里的每条 ⚠️ 都是独立提示, 合并成一段会读成一长句。"""
        blocks = mdview.parse("> 第一条\n> 第二条")
        self.assertEqual([mdview.span_text(b.spans) for b in blocks], ["第一条", "第二条"])

    def test_rule(self):
        self.assertIsInstance(mdview.parse("---")[0], mdview.Rule)

    def test_wrapped_paragraph_lines_are_joined(self):
        blocks = mdview.parse("第一行\n第二行\n\n另一段")
        self.assertEqual([mdview.span_text(b.spans) for b in blocks], ["第一行 第二行", "另一段"])

    def test_unknown_markup_is_kept_as_a_paragraph(self):
        """认不出来也要**显示出来**, 不能吞。"""
        blocks = mdview.parse("####### 七个井号不是标题\n\n[链接](http://x)")
        self.assertEqual([type(b).__name__ for b in blocks], ["Paragraph", "Paragraph"])
        self.assertIn("七个井号", mdview.span_text(blocks[0].spans))
        self.assertIn("链接", mdview.span_text(blocks[1].spans))

    def test_adjacent_plain_lines_join_into_one_paragraph(self):
        """markdown 语义: 中间没有空行的连续几行是**同一段**。"""
        blocks = mdview.parse("上半句\n下半句")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(mdview.span_text(blocks[0].spans), "上半句 下半句")


class TestTables(unittest.TestCase):
    MD = ("| 时段 | 有效专注 |\n"
          "|---|---|\n"
          "| 08:30–10:30 高数 | **1 小时** |\n"
          "| 10:40–11:30 数学 | 0 分钟 |\n")

    def test_header_rows_and_bold_cells(self):
        tbl = mdview.parse(self.MD)[0]
        self.assertIsInstance(tbl, mdview.Table)
        self.assertEqual([mdview.span_text(c) for c in tbl.header], ["时段", "有效专注"])
        self.assertEqual(len(tbl.rows), 2)
        self.assertEqual(mdview.span_text(tbl.rows[0][1]), "1 小时")
        self.assertTrue(tbl.rows[0][1][0].bold, "单元格里的 **加粗** 没有被解析")

    def test_separator_row_is_not_rendered_as_a_row(self):
        tbl = mdview.parse(self.MD)[0]
        self.assertEqual(len(tbl.rows), 2)
        for row in [tbl.header] + tbl.rows:
            for cell in row:
                self.assertNotIn("-", mdview.span_text(cell).replace("–", ""))

    def test_short_rows_are_padded_to_the_header_width(self):
        """手写的 markdown 少写一列时, 渲染层不该为它写防御代码。"""
        tbl = mdview.parse("| a | b | c |\n|---|---|---|\n| 1 |\n")[0]
        self.assertEqual([len(r) for r in tbl.rows], [3])
        self.assertEqual([mdview.span_text(c) for c in tbl.rows[0]], ["1", "", ""])

    def test_pipe_line_without_a_separator_is_a_paragraph(self):
        blocks = mdview.parse("这里有一个 | 竖线, 但它不是表格")
        self.assertEqual([type(b).__name__ for b in blocks], ["Paragraph"])

    def test_rule_right_after_a_table_is_not_a_table(self):
        blocks = mdview.parse("| a | b |\n---\n正文")
        self.assertEqual([type(b).__name__ for b in blocks], ["Paragraph", "Rule", "Paragraph"])

    def test_column_widths_take_the_widest_cell_plus_padding(self):
        tbl = mdview.parse(self.MD)[0]
        widths = mdview.column_widths(tbl, fake_measure, pad=16)
        self.assertEqual(len(widths), 2)
        # 第一列最宽的是 "08:30–10:30 高数", 第二列最宽的是表头 "有效专注"
        self.assertEqual(widths[0], fake_measure("08:30–10:30 高数") + 16)
        self.assertEqual(widths[1], fake_measure("有效专注") + 16)

    def test_column_widths_do_not_depend_on_column_count(self):
        """列宽是"每列自己的最宽单元格", 所以列多了不会把某一列挤没。"""
        md = "| a | bbbb |\n|---|---|\n| aaaaaaaa | b |\n"
        widths = mdview.column_widths(mdview.parse(md)[0], fake_measure, pad=0)
        self.assertEqual(widths, [8 * 8, 4 * 8])


class TestRealReport(unittest.TestCase):
    """拿**真实生成**的日报过一遍解析器 —— 格式变了这里会立刻红。"""

    DAY = "2026-09-18"
    RAW = {
        "general": {"tick_hz": 5},
        "schedule": {"block": [
            {"name": "高数强化听课", "start": "08:30", "end": "10:30", "kind": "focus"},
            {"name": "英语单词", "start": "15:10", "end": "15:40", "kind": "focus",
             "allow_phone": True},
        ]},
        "report": {"report_dir": "data/reports"},
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.cfg = Config.from_dict(self.RAW, path=root / "config.toml")
        self.store = Store(root / "data" / "events.sqlite3")
        for i in range(300):
            self.store.coverage_tick(_at(9, 0), pose_hit=True, face_hit=True, judging=True)
        self.store.event("episode_end", at=_at(9, 5), signal="screen", duration=120)
        self.store.event("away_end", at=_at(9, 20), duration=300)
        self.store.event("episode_start", at=_at(9, 30), signal="screen")
        self.store.event("nudge", at=_at(9, 30), signal="screen", level=1, detail="微信")
        self.store.event("pause_end", at=_at(9, 40), duration=600, detail="接电话")
        self.md = build_report(self.cfg, self.store, self.DAY, now=_at(23, 0))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_every_section_heading_is_recognized(self):
        heads = [mdview.span_text(b.spans) for b in mdview.parse(self.md)
                 if isinstance(b, mdview.Heading)]
        self.assertIn("2026-09-18 专注日报", heads)
        self.assertIn("各时段专注情况(按作息表)", heads)
        self.assertIn("数据可信度", heads)

    def test_the_block_table_keeps_its_shape(self):
        tables = [b for b in mdview.parse(self.md) if isinstance(b, mdview.Table)]
        block_tbl = [t for t in tables
                     if mdview.span_text(t.header[0]) == "时段"]
        self.assertEqual(len(block_tbl), 1, "「各时段专注情况」那张表没被认出来")
        tbl = block_tbl[0]
        self.assertEqual([mdview.span_text(c) for c in tbl.header],
                         ["时段", "计划", "实际监控", "有效专注", "专注率",
                          "分心", "看不清", "离开"])
        # 两个作息块 + 合计
        self.assertEqual(len(tbl.rows), 3)
        self.assertEqual(mdview.span_text(tbl.rows[-1][0]), "合计")
        self.assertTrue(tbl.rows[-1][0][0].bold, "合计那一行该是粗体")

    def test_no_raw_markdown_markers_survive(self):
        """渲染层拿到的每一段文字都不该再带 markdown 记号。"""
        for blk in mdview.parse(self.md):
            cells = ([blk.header] + blk.rows) if isinstance(blk, mdview.Table) else None
            texts = ([mdview.span_text(c) for row in cells for c in row] if cells
                     else [mdview.span_text(blk.spans)] if hasattr(blk, "spans") else [])
            for t in texts:
                self.assertNotIn("**", t, f"粗体记号漏到了渲染层: {t!r}")
                self.assertNotIn("|", t, f"表格竖线漏到了渲染层: {t!r}")
                self.assertFalse(t.startswith("#"), f"标题记号漏到了渲染层: {t!r}")

    def test_quotes_and_bullets_and_paragraphs_all_present(self):
        kinds = {type(b).__name__ for b in mdview.parse(self.md)}
        self.assertTrue({"Heading", "Table", "Quote", "Bullet", "Paragraph"} <= kinds,
                        f"有块类型没被解析出来: {kinds}")


def _at(hh: int, mm: int, ss: int = 0):
    from datetime import datetime
    return datetime(2026, 9, 18, hh, mm, ss)


if __name__ == "__main__":
    unittest.main()
