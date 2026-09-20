"""守 LOW 那批"看起来小、但会误导人"的问题。

L6 `start == end` 的块被算成 **24 小时计划**, 却 `contains()` 永不命中 ——
   日报的"计划学习时长"直接变成一天, 还会冒出一条"约 24 小时没有监控数据"的假警告。
L7 `idle_seconds()` 用**有符号** GetTickCount: 开机满 2^31 毫秒(约 24.85 天)后
   恒返回 0, 「已 N 秒没有键鼠操作」这句证据文案再也不会出现。
L8 同一 tick 里两条提醒时第二声被 min_gap 压掉, 而且**不留任何痕迹**。
"""
from __future__ import annotations

import ctypes
import pathlib
import sys
import time
import unittest
from ctypes import wintypes
from datetime import datetime, time as dtime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.config import Schedule, ScheduleBlock  # noqa: E402
from attention_please.notifier import Notifier  # noqa: E402


class TestZeroLengthBlock(unittest.TestCase):
    """L6 —— start == end 是空块, 不是 24 小时。"""

    def test_zero_length_block_plans_nothing(self):
        z = ScheduleBlock(name="手滑", start=dtime(20, 0), end=dtime(20, 0), kind="focus")
        self.assertEqual(Schedule(blocks=[z]).planned_seconds(), 0.0)
        self.assertFalse(z.contains(dtime(20, 0)), "空块不该命中任何时刻")

    def test_cross_midnight_still_works(self):
        b = ScheduleBlock(name="深夜", start=dtime(23, 30), end=dtime(1, 0), kind="focus")
        self.assertAlmostEqual(Schedule(blocks=[b]).planned_seconds(), 5400.0)
        self.assertTrue(b.contains(dtime(23, 45)))
        self.assertTrue(b.contains(dtime(0, 30)))
        self.assertFalse(b.contains(dtime(2, 0)))

    def test_normal_block_unaffected(self):
        b = ScheduleBlock(name="上午", start=dtime(8, 30), end=dtime(10, 30), kind="focus")
        self.assertAlmostEqual(Schedule(blocks=[b]).planned_seconds(), 7200.0)


class TestTickCountIsUnsigned(unittest.TestCase):
    """L7 —— GetTickCount 必须是 DWORD, 否则开机 24.85 天后恒为 0。"""

    def test_restype_is_declared_unsigned(self):
        from attention_please import input_activity
        k32 = getattr(input_activity, "_kernel32", None)
        if k32 is None:                       # 非 Windows
            self.skipTest("只在 Windows 上有效")
        self.assertIs(k32.GetTickCount.restype, wintypes.DWORD,
                      "restype 没声明 -> ctypes 默认 c_int, 开机 24.85 天后会读成负数")

    def test_arithmetic_survives_a_25_day_uptime(self):
        """把"开机 25 天"代进去算一遍: 声明成 DWORD 之后结果必须还是 60 秒。"""
        raw = 25 * 86400 * 1000                      # > 2**31
        as_dword = ctypes.c_ulong(raw & 0xFFFFFFFF).value
        dw_time = wintypes.DWORD(raw - 60_000).value  # 最后一次输入在 60 秒前
        self.assertAlmostEqual(max(0.0, (as_dword - dw_time) / 1000.0), 60.0, delta=1)
        # 对照: 有符号读取会算成 0(修复前的行为)
        as_signed = ctypes.c_int(raw & 0xFFFFFFFF).value
        self.assertEqual(max(0.0, (as_signed - dw_time) / 1000.0), 0.0)


class TestDroppedBeepIsVisible(unittest.TestCase):
    """L8 —— 第二声被压掉可以, 但不能一声不响。"""

    def test_second_buzz_within_the_gap_says_so(self):
        n = Notifier()
        played: list = []

        class FakeBeep:
            def Beep(self, freq, dur):    # noqa: N802 - 模拟 winsound.Beep
                played.append((freq, dur))
                time.sleep(dur / 1000.0)

        n._winsound = FakeBeep()
        n.buzz(1, sound=True, label="A")
        n.buzz(1, sound=True, label="B")     # 同一个 tick 的第二条提醒
        time.sleep(1.2)
        self.assertEqual(len(played), 1, "第二声本来就该被 min_gap 压掉")
        self.assertGreater(len(played), 0)
        # 压掉这件事本身必须留下痕迹(走 safe_print -> 文件日志)
        import io
        import contextlib
        n2 = Notifier()
        n2._winsound = FakeBeep()
        n2.buzz(1, sound=True, label="A")
        time.sleep(0.05)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            n2._play([(880, 10)])
        self.assertIn("最小间隔", buf.getvalue(),
                      "被压掉的提醒声没有任何日志 —— 事后对不上账")


if __name__ == "__main__":
    unittest.main()
