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
import time
import unittest

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


if __name__ == "__main__":
    unittest.main()
