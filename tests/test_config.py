"""纯逻辑单测: 配置 / 时间表 / 安静时段 / 校准数据(M2)。"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, time as dtime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.config import (  # noqa: E402
    Calibration,
    Config,
    PoseStat,
    TimeRange,
    parse_hhmm,
)

RAW = {
    "general": {"camera_index": 2, "tick_hz": 3, "auto_monitor_in_schedule": True},
    "schedule": {
        "block": [
            {"name": "上午", "start": "08:30", "end": "10:30", "kind": "focus"},
            {"name": "单词", "start": "15:10", "end": "15:40", "kind": "focus",
             "allow_phone": True},
            {"name": "深夜", "start": "23:30", "end": "01:00", "kind": "focus"},
            {"name": "休息", "start": "10:30", "end": "10:40", "kind": "rest"},
        ]
    },
    "quiet_hours": {"enabled": True, "ranges": ["18:00-24:00", "23:00-02:00"]},
    "report": {"report_time": "21:30", "report_dir": "data/reports"},
}


def dt(hh: int, mm: int) -> datetime:
    return datetime(2026, 5, 1, hh, mm, 0)


class TestTimeRange(unittest.TestCase):
    def test_plain(self):
        r = TimeRange.parse("08:30-10:30")
        self.assertTrue(r.contains(dtime(8, 30)))
        self.assertTrue(r.contains(dtime(10, 29)))
        self.assertFalse(r.contains(dtime(10, 30)))

    def test_end_of_day(self):
        r = TimeRange.parse("18:00-24:00")
        self.assertFalse(r.contains(dtime(17, 59)))
        self.assertTrue(r.contains(dtime(18, 0)))
        self.assertTrue(r.contains(dtime(23, 59)))

    def test_cross_midnight(self):
        r = TimeRange.parse("23:00-02:00")
        self.assertTrue(r.contains(dtime(23, 30)))
        self.assertTrue(r.contains(dtime(1, 0)))
        self.assertFalse(r.contains(dtime(2, 0)))
        self.assertFalse(r.contains(dtime(12, 0)))

    def test_parse_hhmm_rejects_junk(self):
        with self.assertRaises(ValueError):
            parse_hhmm("八点半")


class TestSchedule(unittest.TestCase):
    def setUp(self):
        self.cfg = Config.from_dict(RAW)

    def test_block_at_focus(self):
        b = self.cfg.schedule.block_at(dt(9, 0))
        self.assertIsNotNone(b)
        self.assertEqual(b.name, "上午")
        self.assertFalse(b.allow_phone)

    def test_block_at_rest_and_gap(self):
        self.assertIsNone(self.cfg.schedule.block_at(dt(12, 0)))   # 午休
        self.assertIsNone(self.cfg.schedule.block_at(dt(10, 35)))  # rest 块不算 focus

    def test_allow_phone_flag(self):
        self.assertTrue(self.cfg.schedule.block_at(dt(15, 20)).allow_phone)
        self.assertFalse(self.cfg.schedule.block_at(dt(15, 20)).allow_phone is None)

    def test_cross_midnight_block(self):
        self.assertIsNotNone(self.cfg.schedule.block_at(dt(23, 45)))
        self.assertIsNotNone(self.cfg.schedule.block_at(dt(0, 30)))
        self.assertIsNone(self.cfg.schedule.block_at(dt(1, 30)))

    def test_in_focus_window(self):
        self.assertTrue(self.cfg.schedule.in_focus_window(dt(9, 0)))
        self.assertFalse(self.cfg.schedule.in_focus_window(dt(19, 0)))


class TestQuietHours(unittest.TestCase):
    def setUp(self):
        self.cfg = Config.from_dict(RAW)

    def test_evening_is_quiet(self):
        self.assertTrue(self.cfg.quiet_hours.is_quiet(dt(19, 0)))
        self.assertTrue(self.cfg.quiet_hours.is_quiet(dt(23, 30)))

    def test_morning_is_loud(self):
        self.assertFalse(self.cfg.quiet_hours.is_quiet(dt(9, 0)))
        self.assertFalse(self.cfg.quiet_hours.is_quiet(dt(17, 0)))

    def test_disabled(self):
        raw = {"quiet_hours": {"enabled": False, "ranges": ["00:00-24:00"]}}
        self.assertFalse(Config.from_dict(raw).quiet_hours.is_quiet(dt(12, 0)))


class TestCalibration(unittest.TestCase):
    def test_defaults_are_conservative_when_uncalibrated(self):
        cal = Calibration()
        self.assertFalse(cal.is_calibrated)
        self.assertGreaterEqual(cal.phone_yaw_deg, 25.0)
        self.assertGreaterEqual(cal.book_pitch_deg, 15.0)

    def test_derive_thresholds_are_midpoints(self):
        cal = Calibration.derive({
            "screen": [(0.0, 5.0)] * 20,
            "book": [(2.0, 35.0)] * 20,
            "phone": [(40.0, 20.0)] * 20,
        })
        self.assertTrue(cal.is_calibrated)
        self.assertAlmostEqual(cal.phone_yaw_deg, 20.0, places=1)
        self.assertAlmostEqual(cal.book_pitch_deg, 20.0, places=1)
        self.assertGreater(cal.screen_pitch_deg, 5.0)

    def test_derive_without_phone_samples_falls_back(self):
        cal = Calibration.derive({"screen": [(0.0, 5.0)] * 20})
        self.assertGreaterEqual(cal.phone_yaw_deg, 20.0)

    def test_save_load_round_trip(self):
        cal = Calibration.derive({
            "screen": [(0.0, 5.0)], "book": [(1.0, 30.0)], "phone": [(38.0, 18.0)],
        }, camera_index=1)
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "calibration.json"
            cal.save(path)
            back = Calibration.load(path)
        self.assertTrue(back.is_calibrated)
        self.assertEqual(back.camera_index, 1)
        self.assertAlmostEqual(back.phone_yaw_deg, cal.phone_yaw_deg, places=1)

    def test_load_missing_file_is_uncalibrated(self):
        cal = Calibration.load(pathlib.Path("does-not-exist.json"))
        self.assertFalse(cal.is_calibrated)

    def test_pose_stat_from_dict(self):
        s = PoseStat.from_dict({"mean": 12.5, "std": 1.5})
        self.assertEqual((s.mean, s.std), (12.5, 1.5))


class TestRealConfig(unittest.TestCase):
    """真实 config.toml 必须能被解析(结构检查, 不对具体时间做断言, 免得你改了作息就红)。"""

    def test_loads(self):
        cfg = Config.load()
        self.assertGreaterEqual(len(cfg.schedule.blocks), 1)
        self.assertTrue(any(b.is_focus for b in cfg.schedule.blocks))
        self.assertGreater(len(cfg.wordlists.whitelist), 5)
        self.assertGreater(len(cfg.wordlists.blacklist), 5)
        self.assertIsInstance(cfg.general.camera_index, int)
        self.assertGreater(cfg.general.tick_hz, 0)
        self.assertTrue(cfg.report_dir.name)

    def test_paths_under_project_root(self):
        cfg = Config.load()
        self.assertEqual(cfg.data_dir.name, "data")
        self.assertEqual(cfg.capture_dir.name, "captures")
        self.assertTrue(str(cfg.db_path).endswith("events.sqlite3"))


if __name__ == "__main__":
    unittest.main()
