"""日报生成与截图的纯逻辑单测(不需要摄像头)。"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import unittest
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from attention_please import capture  # noqa: E402
from attention_please.config import Config  # noqa: E402
from attention_please.report import build_report, write_report  # noqa: E402
from attention_please.store import Store  # noqa: E402

DAY = "2026-05-01"

RAW = {
    "general": {"tick_hz": 5, "camera_width": 1280, "camera_height": 720},
    "schedule": {
        "block": [
            {"name": "上午", "start": "08:30", "end": "10:30", "kind": "focus"},
            {"name": "下午", "start": "13:00", "end": "15:00", "kind": "focus"},
            {"name": "休息", "start": "10:30", "end": "10:40", "kind": "rest"},
        ]
    },
    "report": {"report_dir": "data/reports"},
}


def at(hh, mm, ss=0) -> datetime:
    return datetime(2026, 5, 1, hh, mm, ss)


class TestPlannedSeconds(unittest.TestCase):
    def test_sums_only_focus_blocks(self):
        cfg = Config.from_dict(RAW)
        self.assertAlmostEqual(cfg.schedule.planned_seconds(), 4 * 3600)

    def test_cross_midnight_block(self):
        raw = {"schedule": {"block": [
            {"name": "深夜", "start": "23:30", "end": "01:00"}]}}
        cfg = Config.from_dict(raw)
        self.assertAlmostEqual(cfg.schedule.planned_seconds(), 5400)


class TestReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.cfg = Config.from_dict(RAW, path=root / "config.toml")
        self.store = Store(root / "data" / "events.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_empty_day_report_has_four_lines(self):
        text = build_report(self.cfg, self.store, DAY)
        for key in ("计划学习时长", "有效专注", "分心", "暂停", "离开座位"):
            self.assertIn(key, text)
        self.assertIn("(今天没有被判定的分心)", text)
        self.assertIn("(今天没有暂停)", text)

    def test_counts_and_durations(self):
        self.store.event("episode_end", at=at(9, 0), signal="screen", duration=120)
        self.store.event("episode_end", at=at(9, 30), signal="screen", duration=60)
        self.store.event("nudge", at=at(9, 0), signal="screen", level=1,
                         detail="哔哩哔哩 - Chrome")
        self.store.event("pause_end", at=at(14, 0), duration=600, detail="上厕所")
        for i in range(2):
            self.store.coverage_tick(at(9, 0, i), pose_hit=True, face_hit=True,
                                     judging=True)
        text = build_report(self.cfg, self.store, DAY)
        self.assertIn("3 分钟(2 次)", text)      # 分心 120+60 秒
        self.assertIn("10 分钟", text)           # 暂停
        self.assertIn("哔哩哔哩 - Chrome", text)
        self.assertIn("上厕所", text)

    def test_coverage_warning_when_low(self):
        for i in range(10):
            self.store.coverage_tick(at(9, 0, i), pose_hit=True, face_hit=(i < 5),
                                     judging=True)
        text = build_report(self.cfg, self.store, DAY)
        self.assertIn("人脸覆盖率 **50%**", text)
        self.assertIn("不可靠", text)
        self.assertIn("脸不在画面", text)

    def test_busiest_hours_listed(self):
        self.store.event("episode_end", at=at(9, 10), duration=300)
        self.store.event("episode_end", at=at(15, 10), duration=60)
        text = build_report(self.cfg, self.store, DAY)
        self.assertIn("09:00–10:00", text)
        self.assertIn("15:00–16:00", text)

    def test_write_report_creates_file(self):
        path = write_report(self.cfg, self.store, DAY)
        self.assertTrue(path.exists())
        self.assertIn("专注日报", path.read_text(encoding="utf-8"))
        self.assertEqual(path.name, f"{DAY}.md")

    def test_camera_busy_flagged(self):
        self.store.event("camera_busy_end", at=at(9, 0), duration=420)
        text = build_report(self.cfg, self.store, DAY)
        self.assertIn("摄像头被占用漏检 7 分钟", text)


class TestCapture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name) / "captures"

    def tearDown(self):
        self.tmp.cleanup()

    def test_compose_stacks_screen_over_camera(self):
        screen = Image.new("RGB", (1920, 1080), (10, 20, 30))
        cam = Image.new("RGB", (1280, 720), (200, 100, 50))
        out = capture.compose(screen, cam, width=400)
        self.assertEqual(out.width, 400)
        expected = (round(1080 * 400 / 1920) + capture.LABEL_BAR_PX
                    + round(720 * 400 / 1280) + capture.LABEL_BAR_PX
                    + capture.SEPARATOR_PX)
        self.assertEqual(out.height, expected)

    def test_compose_without_screen(self):
        cam = Image.new("RGB", (640, 480), (0, 0, 0))
        out = capture.compose(None, cam, width=320)
        self.assertEqual(out.width, 320)

    def test_compose_nothing_returns_none(self):
        self.assertIsNone(capture.compose(None, None))

    def test_save_camera_only_writes_jpeg(self):
        frame = np.zeros((720, 1280, 3), dtype="uint8")
        frame[:, :, 1] = 120
        path = capture.save_composite(self.dir, at=at(9, 0), frame_bgr=frame,
                                      width=480, screen=False)
        self.assertIsNotNone(path)
        assert path is not None
        self.assertTrue(path.exists())
        self.assertLess(path.stat().st_size, 200_000)

    def test_save_returns_none_when_nothing(self):
        self.assertIsNone(capture.save_composite(self.dir, at=at(9, 0), frame_bgr=None,
                                                 screen=False))

    def test_cleanup_removes_only_old(self):
        self.dir.mkdir(parents=True)
        old = self.dir / "old.jpg"
        new = self.dir / "new.jpg"
        Image.new("RGB", (10, 10)).save(old, "JPEG")
        Image.new("RGB", (10, 10)).save(new, "JPEG")
        past = time.time() - 8 * 86400
        import os
        os.utime(old, (past, past))
        self.assertEqual(capture.cleanup(self.dir, retention_days=7), 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_cleanup_disabled_when_zero_days(self):
        self.dir.mkdir(parents=True)
        f = self.dir / "a.jpg"
        Image.new("RGB", (10, 10)).save(f, "JPEG")
        self.assertEqual(capture.cleanup(self.dir, retention_days=0), 0)
        self.assertTrue(f.exists())

    def test_clear_all(self):
        self.dir.mkdir(parents=True)
        for name in ("a.jpg", "b.jpg"):
            Image.new("RGB", (10, 10)).save(self.dir / name, "JPEG")
        self.assertEqual(capture.clear_all(self.dir), 2)
        self.assertEqual(capture.clear_all(self.dir), 0)

    def test_newest(self):
        self.dir.mkdir(parents=True)
        self.assertIsNone(capture.newest(self.dir))
        Image.new("RGB", (10, 10)).save(self.dir / "a.jpg", "JPEG")
        self.assertEqual(capture.newest(self.dir).name, "a.jpg")


if __name__ == "__main__":
    unittest.main()
