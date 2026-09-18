"""运行时"暂停 / 手动学习 / 每日任务"的纯逻辑单测(不需要摄像头)。

守的几条已确认的口径:
  - 暂停**必须**填理由(这是"有代价的暂停"的代价);
  - 暂停时长单列(pause_end), 不混进有效专注;
  - 时间表外可以手动开始学习;
  - 日报要在 report_time 之后生成 —— 而那一刻通常已经不在学习时段,
    所以它绝不能挂在"判定分支"里(这个坑已经踩过一次)。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.config import Calibration, Config  # noqa: E402
from attention_please.runtime import Runtime  # noqa: E402

RAW = {
    "detection": {"enabled_signals": ["screen"]},
    "reminder": {"max_level_high": 3, "cooldown_seconds": 180,
                 "wrong_feedback_mute_seconds": 900},
    "schedule": {"block": [{"name": "上午", "start": "08:30", "end": "10:30"}]},
    "report": {"daily_report": True, "report_time": "21:30",
               "report_dir": "data/reports"},
    "privacy": {"save_captures": False, "capture_retention_days": 7},
}


class RuntimeHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        cfg = Config.from_dict(RAW, path=self.root / "config.toml")
        self.rt = Runtime(cfg, Calibration())
        self.rt.notifier = types.SimpleNamespace(buzz=lambda *a, **k: None)
        self.rt.ui = types.SimpleNamespace(show=lambda req: None,
                                           ask_reason=lambda *a: None,
                                           drain=lambda: [])

    def tearDown(self):
        self.rt.close()
        self.tmp.cleanup()

    def events(self, kind: str) -> list[tuple]:
        cur = self.rt.store.conn.execute(
            "SELECT duration, detail FROM event WHERE kind = ? ORDER BY id", (kind,))
        return cur.fetchall()


class TestPause(RuntimeHarness):
    def test_pause_requires_reason(self):
        self.rt.pause("", datetime.now())
        self.assertFalse(self.rt.paused)
        self.rt.pause("   ", datetime.now())
        self.assertFalse(self.rt.paused)
        self.assertEqual(self.events("pause_start"), [])

    def test_pause_blocks_judging_and_sets_state(self):
        now = datetime(2026, 5, 1, 9, 0)
        self.rt.pause("上厕所", now)
        self.assertTrue(self.rt.paused)
        self.assertEqual(self.rt.state_key(), "paused")
        policy = self.rt._policy(now)
        self.assertFalse(policy.judging)
        self.assertEqual(policy.block_name, "已暂停")
        self.assertEqual(self.events("pause_start"), [(None, "上厕所")])

    def test_resume_records_duration_but_not_focus(self):
        start = datetime(2026, 5, 1, 9, 0)
        self.rt.pause("接电话", start)
        self.rt.resume(start + timedelta(minutes=7))
        self.assertFalse(self.rt.paused)
        self.assertEqual(self.events("pause_end"), [(420.0, "接电话")])
        st = self.rt.store.day_stats("2026-05-01")
        self.assertAlmostEqual(st.pause_seconds, 420.0)

    def test_resume_without_pause_is_noop(self):
        self.rt.resume(datetime.now())
        self.assertEqual(self.events("pause_end"), [])

    def test_request_pause_goes_through_ui(self):
        calls = []
        self.rt.ui = types.SimpleNamespace(ask_reason=lambda *a: calls.append(a),
                                           show=lambda req: None, drain=lambda: [])
        self.rt.request_pause()
        self.assertEqual(len(calls), 1)


class TestManualSession(RuntimeHarness):
    def test_manual_session_judges_outside_schedule(self):
        outside = datetime(2026, 5, 1, 20, 0)
        self.assertFalse(self.rt._policy(outside).judging)
        self.rt.start_manual_session(60)
        policy = self.rt._policy(datetime.now())
        self.assertTrue(policy.judging)
        self.assertEqual(policy.block_name, "手动学习")

    def test_manual_session_does_not_affect_inside_schedule(self):
        self.rt.start_manual_session(60)
        inside = datetime(2026, 5, 1, 9, 0)
        self.assertEqual(self.rt._policy(inside).block_name, "上午")


class TestDailyJobs(RuntimeHarness):
    def test_report_written_after_report_time(self):
        late = datetime.now().replace(hour=23, minute=59, second=0, microsecond=0)
        self.rt._daily_jobs(late)
        report = self.root / "data" / "reports" / f"{late.date().isoformat()}.md"
        self.assertTrue(report.exists())
        self.assertIn("专注日报", report.read_text(encoding="utf-8"))

    def test_report_not_written_before_report_time(self):
        early = datetime.now().replace(hour=0, minute=1, second=0, microsecond=0)
        self.rt._daily_jobs(early)
        report = self.root / "data" / "reports" / f"{early.date().isoformat()}.md"
        self.assertFalse(report.exists())

    def test_report_written_only_once_per_day(self):
        late = datetime.now().replace(hour=23, minute=59, second=0, microsecond=0)
        self.rt._daily_jobs(late)
        report = self.root / "data" / "reports" / f"{late.date().isoformat()}.md"
        first = report.stat().st_mtime_ns
        self.rt._daily_jobs(late)
        self.assertEqual(report.stat().st_mtime_ns, first)

    def test_report_skipped_when_disabled(self):
        raw = dict(RAW)
        raw["report"] = {"daily_report": False, "report_time": "00:01"}
        cfg = Config.from_dict(raw, path=self.root / "config2.toml")
        rt = Runtime(cfg, Calibration())
        rt._daily_jobs(datetime.now().replace(hour=23, minute=59))
        self.assertFalse((self.root / "data" / "reports").exists())
        rt.close()


class TestStatusText(RuntimeHarness):
    def test_status_text_mentions_state_and_metrics(self):
        text = self.rt.status_text()
        self.assertIn("attention_please", text)
        self.assertIn("人脸覆盖率", text)

    def test_state_idle_outside_schedule(self):
        # 用一个必然不在时间表内的时间点判断策略
        self.assertFalse(self.rt.cfg.schedule.in_focus_window(
            datetime(2026, 5, 1, 20, 0)))


if __name__ == "__main__":
    unittest.main()
