"""日报的"实际监控时长"口径测试。

## 守的是 2026-09-18 上午发现的报表误导

程序 09:09 才启动, 报表却写 "有效专注 1 小时 48 分 / 计划 9 小时 30 分 = 19%",
看起来像摸了一天鱼。根因是**比值分母用了"计划时长"** ——
"机器没开机 / 程序没跑 / 暂停"被算成了"你没专注"。

现在的口径:
  - 比值分母 = **实际监控时长**(真正在判定的时间);
  - 计划时长只作为上下文;
  - 并且必须写一行"计划里这段没有监控数据"的警告 —— 漏报必须可见。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.config import Config  # noqa: E402
from attention_please.report import build_report  # noqa: E402
from attention_please.store import Store  # noqa: E402

DAY = "2026-09-18"

RAW = {
    "general": {"tick_hz": 5},
    "schedule": {"block": [
        {"name": "高数强化听课", "start": "08:30", "end": "10:30", "kind": "focus"},
        {"name": "数学刷题", "start": "10:40", "end": "11:30", "kind": "focus"},
    ]},
    "report": {"report_dir": "data/reports"},
}


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 9, 18, hh, mm, ss)


class TestMonitoredWindow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.cfg = Config.from_dict(RAW, path=root / "config.toml")
        self.store = Store(root / "data" / "events.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _fill_minute(self, hh: int, mm: int, ticks: int = 300, face: int | None = None):
        for i in range(ticks):
            self.store.coverage_tick(at(hh, mm, 0), pose_hit=True,
                                     face_hit=(face is None or i < face), judging=True)

    def test_monitored_seconds_from_ticks(self):
        self._fill_minute(9, 9)
        self._fill_minute(9, 10)
        st = self.store.day_stats(DAY, tick_hz=5)
        self.assertAlmostEqual(st.monitored_seconds, 600 / 5, places=1)
        self.assertEqual(st.first_monitored, "09:09")
        self.assertEqual(st.last_monitored, "09:10")

    def test_ratio_uses_monitored_not_plan(self):
        """09:09 才开机: 比值必须按监控时间算, 不能被 9.5 小时计划拉低。"""
        for minute in range(9 * 60 + 9, 11 * 60 + 30):      # 09:09 - 11:29
            self._fill_minute(minute // 60, minute % 60)
        text = build_report(self.cfg, self.store, DAY)
        self.assertIn("实际监控", text)
        self.assertIn("/ 实际监控", text)
        self.assertIn("占监控时间", text)
        # 计划 3 小时 30 分, 监控 2 小时 21 分: 占比必须接近 100%, 而不是 67%
        self.assertIn("100%", text)
        self.assertNotIn("(占监控时间 67%)", text)

    def test_late_start_warning(self):
        self._fill_minute(9, 9)
        text = build_report(self.cfg, self.store, DAY)
        self.assertIn("08:30", text)
        self.assertIn("那段时间没有数据", text)
        self.assertIn("不要当成没专注", text)

    def test_no_warning_when_started_on_time(self):
        self._fill_minute(8, 30)
        self._fill_minute(8, 31)
        text = build_report(self.cfg, self.store, DAY)
        self.assertNotIn("那段时间没有数据", text)

    def test_empty_day_is_explicit(self):
        text = build_report(self.cfg, self.store, DAY)
        self.assertIn("今天没有任何监控数据", text)

    def test_focus_equals_monitored_minus_losses(self):
        self._fill_minute(9, 0, ticks=300, face=0)          # 整分钟看不清
        self.store.event("episode_end", at=at(9, 0, 5), signal="screen", duration=60)
        st = self.store.day_stats(DAY, tick_hz=5)
        self.assertAlmostEqual(st.monitored_seconds, 60.0, places=1)
        self.assertAlmostEqual(st.blind_seconds, 60.0, places=1)
        self.assertAlmostEqual(st.focus_seconds, 0.0, places=1)


if __name__ == "__main__":
    unittest.main()
