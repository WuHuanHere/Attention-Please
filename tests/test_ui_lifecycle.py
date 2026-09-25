"""UI 生命周期测试: 窗口开→关(X 按钮)→再开, 必须还能开。

守的是 2026-09-17 你报的那个 bug:
  "打开一次今日日报, 关闭, 再打开就打不开了; 暂停学习的弹窗也弹不出来"

根因有两个, 都在 Tk 的用法上:
  1. `root.after()` 写在轮询回调最后一行 → 任何一次异常都会让轮询链**永久断掉**
     (mainloop 还活着, 但再也没人处理请求);
  2. 点右上角 X 关窗口时 Tk 直接销毁 widget, 而我们的 `state["win"]` 还指着它 →
     下一轮拿它算倒计时抛 TclError → 触发第 1 条。

这些测试要真的创建窗口, 所以默认跳过(免得跑单测时满屏弹窗)。
手动跑: `$env:AP_UI_TESTS=1; .\.venv\Scripts\python.exe -m unittest tests.test_ui_lifecycle -v`
"""
from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import time
import unittest
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.ui import AlertRequest, AlertUI  # noqa: E402

UI_TESTS = os.environ.get("AP_UI_TESTS") == "1"


def wait_for(predicate, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@unittest.skipUnless(UI_TESTS, "需要真实窗口; 设 AP_UI_TESTS=1 才跑")
class TestUiLifecycle(unittest.TestCase):
    def setUp(self):
        self.ui = AlertUI()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            self.ui.destroy_current_window_for_test()
            time.sleep(0.2)
        except Exception:  # noqa: BLE001
            pass

    # ---- 核心回归: 日报 ----
    def test_report_x_close_then_reopen(self):
        self.ui.show_report("日报", "# 标题\n正文", "")
        self.assertTrue(wait_for(lambda: self.ui.windows_shown >= 1),
                        f"第一次日报就没打开: {self.ui.last_ui_error}")
        first = self.ui.windows_shown

        self.ui.destroy_current_window_for_test()      # 模拟点右上角 X
        time.sleep(0.8)

        self.ui.show_report("日报", "第二次", "")
        self.assertTrue(
            wait_for(lambda: self.ui.windows_shown > first),
            f"X 关闭后日报再也打不开: windows_shown={self.ui.windows_shown}, "
            f"ui_error={self.ui.last_ui_error!r}, thread_alive={self.ui.alive}")

    def test_report_button_close_then_reopen(self):
        """按钮关闭(正常路径)也必须能再开。"""
        self.ui.show_report("日报", "第一次", "")
        self.assertTrue(wait_for(lambda: self.ui.windows_shown >= 1))
        self.ui.destroy_current_window_for_test()
        time.sleep(0.5)
        self.ui.show_report("日报", "第二次", "")
        self.assertTrue(wait_for(lambda: self.ui.windows_shown >= 2))

    # ---- 核心回归: 暂停弹窗 ----
    def test_pause_dialog_still_works_after_report(self):
        self.ui.show_report("日报", "正文", "")
        self.assertTrue(wait_for(lambda: self.ui.windows_shown >= 1))
        self.ui.destroy_current_window_for_test()
        time.sleep(0.5)

        self.ui.ask_reason("暂停监控", "为什么要暂停?")
        self.assertTrue(
            wait_for(lambda: self.ui.windows_shown >= 2),
            f"日报关掉后暂停弹窗弹不出来了: {self.ui.last_ui_error!r}")

    def test_pause_dialog_x_close_then_reopen(self):
        self.ui.ask_reason("暂停监控", "理由?")
        self.assertTrue(wait_for(lambda: self.ui.windows_shown >= 1))
        self.ui.destroy_current_window_for_test()
        time.sleep(0.5)
        self.ui.ask_reason("暂停监控", "理由?")
        self.assertTrue(wait_for(lambda: self.ui.windows_shown >= 2),
                        f"X 关掉暂停框后再也弹不出来: {self.ui.last_ui_error!r}")

    # ---- 提醒弹窗 ----
    def test_alert_autoclose_then_next_alert(self):
        self.ui.show(AlertRequest(title="提醒1", body="b", signal="screen", level=1,
                                  auto_close_seconds=1.0))
        self.assertTrue(wait_for(lambda: self.ui.windows_shown >= 1))
        time.sleep(1.8)                                # 等它自动关
        self.ui.show(AlertRequest(title="提醒2", body="b", signal="screen", level=2,
                                  auto_close_seconds=1.0))
        self.assertTrue(wait_for(lambda: self.ui.windows_shown >= 2),
                        f"自动关闭后再弹不出来: {self.ui.last_ui_error!r}")

    def test_alert_x_close_then_next_alert(self):
        """X 关掉带倒计时的提醒窗 —— 这是最危险的路径:
        死 widget + autoclose=True 会让轮询链整条断掉。"""
        self.ui.show(AlertRequest(title="提醒1", body="b", signal="screen", level=1,
                                  auto_close_seconds=60))
        self.assertTrue(wait_for(lambda: self.ui.windows_shown >= 1))
        self.ui.destroy_current_window_for_test()
        time.sleep(1.0)                                # 让旧的倒计时逻辑至少跑 5 轮
        self.ui.show(AlertRequest(title="提醒2", body="b", signal="screen", level=2,
                                  auto_close_seconds=60))
        self.assertTrue(wait_for(lambda: self.ui.windows_shown >= 2),
                        f"X 关掉提醒窗后轮询链断了: {self.ui.last_ui_error!r}")

    def test_no_ui_error_recorded(self):
        self.ui.show_report("日报", "正文", "")
        wait_for(lambda: self.ui.windows_shown >= 1)
        self.ui.destroy_current_window_for_test()
        time.sleep(1.0)
        self.assertEqual(self.ui.last_ui_error, "",
                         f"UI 线程报了错: {self.ui.last_ui_error}")

    # ---- 暂停输入框被顶掉 / 点 X: 必须发 reason_cancelled ----
    def test_alert_replacing_the_pause_dialog_reports_cancellation(self):
        """正在输暂停理由时来了升级提醒 -> 输入框被顶掉, 必须发 reason_cancelled。

        以前这种情况**什么都不发**: runtime 一直以为你没暂停, 库和日志里一个字都没有,
        而用户看着框没了, 以为暂停生效了, 起身走人, 然后被"离开太久"提醒。
        """
        self.ui.ask_reason("暂停监控", "为什么?")
        self.assertTrue(wait_for(lambda: self.ui.windows_shown >= 1))
        time.sleep(0.4)
        self.assertEqual(self.ui.drain(), [], "还没被顶掉就不该有事件")

        self.ui.show(AlertRequest(title="提醒", body="b", signal="screen", level=2,
                                  auto_close_seconds=60))
        self.assertTrue(wait_for(lambda: self.ui.windows_shown >= 2))
        time.sleep(0.5)
        kinds = [k for k, _ in self.ui.drain()]
        self.assertIn("reason_cancelled", kinds,
                      f"暂停框被顶掉了却没有通知 runtime: {kinds}")

    def test_x_close_of_pause_dialog_reports_cancellation(self):
        self.ui.ask_reason("暂停监控", "为什么?")
        self.assertTrue(wait_for(lambda: self.ui.windows_shown >= 1))
        time.sleep(0.4)
        self.ui.drain()
        self.ui.destroy_current_window_for_test()       # 模拟点 X
        time.sleep(0.6)
        kinds = [k for k, _ in self.ui.drain()]
        self.assertEqual(kinds, ["reason_cancelled"],
                         f"X 关掉暂停框没有(或重复)通知 runtime: {kinds}")

    def test_x_close_of_a_plain_alert_is_not_a_cancellation(self):
        """对照组: 关掉普通提醒窗不该被当成"取消暂停"。"""
        self.ui.show(AlertRequest(title="提醒", body="b", signal="screen", level=2,
                                  auto_close_seconds=60))
        self.assertTrue(wait_for(lambda: self.ui.windows_shown >= 1))
        time.sleep(0.4)
        self.ui.drain()
        self.ui.destroy_current_window_for_test()
        time.sleep(0.6)
        kinds = [k for k, _ in self.ui.drain()]
        self.assertNotIn("reason_cancelled", kinds, f"普通提醒窗被误判成取消暂停: {kinds}")


@unittest.skipUnless(UI_TESTS, "需要真实窗口; 设 AP_UI_TESTS=1 才跑")
class TestReportPreviewRenders(unittest.TestCase):
    """日报预览: 真在 Tk 里渲染一遍, 检查**渲染结果**而不只是"窗口开了"。

    用户 2026-09-25 的要求是「渲染成 md 的预览的画面」, 所以必须断言:
      * 表格变成 tab 停靠位对齐的行(没有残留的 `|`), 表头/合计是粗体;
      * 太宽时**缩字号**而不是让它折行(表一折行就彻底没法看);
      * 真实日报能完整渲染, 且一个 `ui_error` 都不留。
    """

    def setUp(self):
        import tkinter as tk

        self.tk = tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.body = tk.Text(self.root, wrap="word", width=80, height=30)

    def text(self) -> str:
        return self.body.get("1.0", "end")

    def font_size(self, tag: str) -> int:
        import tkinter.font as tkfont

        return int(tkfont.Font(font=self.body.tag_cget(tag, "font")).cget("size"))

    def test_table_becomes_tabbed_rows(self):
        from attention_please import mdview

        md = ("| 时段 | 有效专注 |\n|---|---|\n"
              "| 08:30–10:30 高数 | **1 小时** |\n"
              "| 10:40–11:30 数学 | 0 分钟 |\n")
        mdview.render(self.body, md, avail_px=900)
        out = self.text()
        self.assertNotIn("|", out, "表格竖线还在 —— 等于没渲染")
        self.assertIn("\t", out, "表格没有用 tab 停靠位对齐")
        self.assertIn("08:30–10:30 高数", out)
        # 表头与单元格里的 **1 小时** 都要有粗体标签
        self.assertTrue(self.body.tag_ranges("tbl1b"), "表头没有加粗")
        self.assertGreaterEqual(len(self.body.tag_ranges("tbl1b")), 2)
        # 表格标签带着 tab 停靠位
        self.assertTrue(self.body.tag_cget("tbl1", "tabs"))

    def test_prose_still_wraps_and_has_no_markers(self):
        from attention_please import mdview

        mdview.render(self.body, "# 标题\n\n> ⚠️ **重点** 和 `代码`\n\n- 一条\n  - 缩进一条\n",
                      avail_px=900)
        out = self.text()
        for marker in ("**", "`", "#", "> ", "- "):
            self.assertNotIn(marker, out, f"markdown 记号 {marker!r} 漏到了画面上")
        self.assertIn("标题", out)
        self.assertIn("重点", out)
        self.assertIn("缩进一条", out)
        self.assertTrue(self.body.tag_ranges("bold"), "行内粗体没生效")
        self.assertTrue(self.body.tag_ranges("b1"), "缩进列表没生效")

    def test_wide_table_shrinks_until_it_actually_fits(self):
        """表太宽时必须缩到**真的塞进可用宽度** —— 缩一半等于没缩, 照样折行。

        断言的是"缩完之后的列宽之和 <= 可用宽度", 而不是"看起来没折行": 后者取决于
        Text 控件有多宽, 在没上屏的测试里量不准。
        """
        import tkinter.font as tkfont

        from attention_please import mdview

        md = ("| 很长的表头一 | 很长的表头二 | 很长的表头三 | 很长的表头四 | 很长的表头五 |\n"
              "|---|---|---|---|---|\n"
              "| 内容甲甲甲 | 内容乙乙乙 | 内容丙丙丙 | 内容丁丁丁 | 内容戊戊戊 |\n")
        avail = 420
        mdview.render(self.body, md, avail_px=avail)
        self.assertLess(self.font_size("tbl1"), 10, "太宽的表没有缩字号")
        tbl = mdview.parse(md)[0]
        font = tkfont.Font(font=self.body.tag_cget("tbl1", "font"))
        # pad=8 是最后一级的列间距, 拿它算出来的是"最宽松的解释", 任何一级都成立
        self.assertLessEqual(sum(mdview.column_widths(tbl, font.measure, pad=8)), avail,
                             "缩完还是塞不下 —— 这张表注定会折行")

    def test_a_comfortable_width_keeps_the_normal_font(self):
        from attention_please import mdview

        md = "| 时段 | 有效专注 |\n|---|---|\n| 09:00–10:00 | 1 小时 |\n"
        mdview.render(self.body, md, avail_px=900)
        self.assertEqual(self.font_size("tbl1"), 10)

    def test_real_report_renders_without_error(self):
        from attention_please import mdview
        from attention_please.config import Config
        from attention_please.report import build_report
        from attention_please.store import Store

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        cfg = Config.from_dict({
            "schedule": {"block": [
                {"name": "高数强化听课", "start": "08:30", "end": "10:30"},
                {"name": "英语单词", "start": "15:10", "end": "15:40",
                 "allow_phone": True}]},
        }, path=root / "config.toml")
        store = Store(root / "data.sqlite3")
        self.addCleanup(store.close)
        store.event("episode_end", at=datetime(2026, 9, 18, 9, 5), signal="screen",
                    duration=120)
        store.event("episode_start", at=datetime(2026, 9, 18, 9, 30), signal="screen")
        store.event("nudge", at=datetime(2026, 9, 18, 9, 30), signal="screen", level=1,
                    detail="微信")
        md = build_report(cfg, store, "2026-09-18",
                          now=datetime(2026, 9, 18, 23, 0))

        mdview.render(self.body, md, avail_px=930)
        out = self.text()
        self.assertIn("各时段专注情况", out)
        self.assertIn("高数强化听课", out)
        self.assertIn("合计", out)
        self.assertNotIn("|", out)
        self.assertNotIn("**", out)
        self.assertNotIn("没有收尾", "".join(self.body.tag_names()))


if __name__ == "__main__":
    unittest.main()
