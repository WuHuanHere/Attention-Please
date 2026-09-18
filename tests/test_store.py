"""store.py 的纯逻辑单测(临时数据库, 不需要摄像头)。"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.store import Store  # noqa: E402

DAY = "2026-05-01"


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 5, 1, hh, mm, ss)


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_empty_day(self):
        st = self.store.day_stats(DAY)
        self.assertEqual(st.episodes, 0)
        self.assertEqual(st.focus_seconds, 0.0)
        self.assertEqual(st.coverage, 0.0)

    def test_distraction_and_nudges_counted(self):
        self.store.event("episode_end", at=at(9, 1), signal="screen", duration=30)
        self.store.event("episode_end", at=at(9, 20), signal="screen", duration=90)
        self.store.event("nudge", at=at(9, 1), signal="screen", level=1)
        self.store.event("nudge", at=at(9, 20), signal="screen", level=2)
        self.store.event("wrong", at=at(9, 2), signal="screen")
        st = self.store.day_stats(DAY)
        self.assertEqual(st.episodes, 2)
        self.assertEqual(st.nudges, 2)
        self.assertEqual(st.wrong_feedback, 1)
        self.assertAlmostEqual(st.distract_seconds, 120.0)

    def test_coverage_accumulates_by_minute(self):
        for i in range(4):
            self.store.coverage_tick(at(9, 0, i * 10), pose_hit=True,
                                     face_hit=(i < 3), judging=True)
        st = self.store.day_stats(DAY)
        self.assertAlmostEqual(st.coverage, 0.75, places=3)

    def test_blind_minutes_counted_separately(self):
        """人脸覆盖不足的分钟要记成'看不清', 既不算专注也不算分心。

        换算: 判定时长 = 判定 tick 数 / tick_hz。这里每分钟只插了 4 个 tick,
        在 4 Hz 下折合 1 秒/分钟, 所以 2 个看不清的分钟 = 2 秒。
        """
        for (mm, face) in ((0, True), (1, False), (2, False)):
            for i in range(4):
                self.store.coverage_tick(at(9, mm, i * 10), pose_hit=True,
                                         face_hit=face, judging=True)
        st = self.store.day_stats(DAY, tick_hz=4.0)
        self.assertAlmostEqual(st.blind_seconds, 2.0, places=1)
        self.assertGreater(st.focus_seconds, 0)

    def test_focus_excludes_distraction_and_away(self):
        for i in range(20):
            self.store.coverage_tick(at(9, 0, i), pose_hit=True, face_hit=True, judging=True)
        self.store.event("episode_end", at=at(9, 0, 3), duration=4)
        self.store.event("away_end", at=at(9, 0, 10), duration=5)
        st = self.store.day_stats(DAY, tick_hz=1.0)
        self.assertAlmostEqual(st.focus_seconds, 20 - 4 - 5, places=1)

    def test_camera_busy_recorded(self):
        self.store.event("camera_busy_end", at=at(9, 0), duration=300)
        st = self.store.day_stats(DAY)
        self.assertAlmostEqual(st.camera_busy_seconds, 300.0)

    def test_busiest_hours(self):
        self.store.event("episode_end", at=at(9, 5), duration=60)
        self.store.event("episode_end", at=at(9, 40), duration=120)
        self.store.event("episode_end", at=at(15, 0), duration=30)
        top = self.store.busiest_distraction_hours(DAY)
        self.assertEqual(top[0][0], 9)
        self.assertAlmostEqual(top[0][1], 180.0)

    def test_top_titles_before(self):
        for _ in range(3):
            self.store.event("nudge", at=at(9, 0), detail="哔哩哔哩 - Chrome")
        self.store.event("nudge", at=at(9, 5), detail="Steam")
        top = self.store.top_titles_before(DAY)
        self.assertEqual(top[0], ("哔哩哔哩 - Chrome", 3))

    def test_reopen_persists(self):
        self.store.event("nudge", at=at(9, 0), level=1)
        self.store.close()
        again = Store(pathlib.Path(self.tmp.name) / "t.sqlite3")
        self.assertEqual(again.day_stats(DAY).nudges, 1)
        again.close()


if __name__ == "__main__":
    unittest.main()
