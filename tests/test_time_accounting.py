"""守"时间口径"这一簇 —— 三个都会让日报的头号数字算错。

H2: 「离开」的时间被从有效专注里**扣了两次**(away ⊆ blind)。
    人不在座位上时人脸当然是 0%, 那些分钟整段落进 blind, 而 away 又扣一次。
    实测 2026-09-18: 离开窗口 15:09:59-15:20:49 全落在 blind 分钟里,
    日报的「有效专注 5 小时 22 分」因此少算 10.8 分钟(应为 5 小时 32 分)。

M2: monitored_seconds 用**配置里的标称 tick_hz** 折算, 于是
    (a) 改一次 tick_hz 会回溯性改写全天统计(5 -> 10 让 98 分钟变成 49 分钟);
    (b) 循环掉速时 monitored 缩水, 而 distract/away 是真实墙钟秒 -> 多扣有效专注。

M7: 跨午夜的时间段整段落进第二天, 起始日还留下一个**假的**"没等到收尾"
    (日报会谎报"程序在分心时被关掉/崩溃"), 次日则被夹成 0 专注。
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
from attention_please.signals import (  # noqa: E402
    AwayChange,
    EpisodeEnd,
    SignalKind,
)
from attention_please.store import Store  # noqa: E402

DAY = "2026-09-20"
RAW = {
    "schedule": {"block": [{"name": "测试", "start": "00:00", "end": "24:00"}]},
    "privacy": {"save_captures": False},
    "report": {"daily_report": False},
}


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 9, 20, hh, mm, ss)


class TestAwayIsNotBlindTwice(unittest.TestCase):
    """H2 —— 离开的时间只能被扣一次。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _blind_minutes(self, minutes: int, start_min: int = 10) -> None:
        """插 minutes 个"人脸 0%"的分钟(每分 60 秒)。"""
        for i in range(minutes):
            for s in range(0, 60, 12):
                self.store.coverage_tick(at(15, start_min + i, s), pose_hit=False,
                                         face_hit=False, judging=True, dt=12.0)

    def test_away_is_subtracted_from_blind(self):
        self._blind_minutes(10)                       # 15:10–15:19 全看不清
        self.store.event("away_start", at=at(15, 10))
        self.store.event("away_end", at=at(15, 19, 30), duration=570)   # 9.5 分钟

        st = self.store.day_stats(DAY)
        self.assertAlmostEqual(st.away_seconds, 570, delta=1)
        # 10 分钟里 9.5 分钟是"离开", 所以只有 30 秒算真正的"看不清"
        self.assertAlmostEqual(st.blind_seconds, 30, delta=2,
                               msg="离开的时间又被算进看不清了 —— 有效专注被扣两遍")

    def test_focus_is_not_double_subtracted(self):
        self._blind_minutes(10)
        self.store.event("away_start", at=at(15, 10))
        self.store.event("away_end", at=at(15, 20), duration=600)

        st = self.store.day_stats(DAY)
        # monitored=600, distract=0, blind≈0, away=600 -> focus≈0(而不是 -600 再夹成 0)
        self.assertAlmostEqual(st.blind_seconds, 0.0, delta=2)
        self.assertAlmostEqual(st.focus_seconds, 0.0, delta=2)

    def test_unmatched_away_start_does_not_reduce_blind(self):
        """没结尾的 away 本来也没进 away_seconds, 所以不许拿它去减 blind。"""
        self._blind_minutes(3)
        self.store.event("away_start", at=at(15, 10))
        st = self.store.day_stats(DAY)
        self.assertAlmostEqual(st.blind_seconds, 180, delta=2)
        self.assertEqual(st.unfinished_away, 1)


class TestMonitoredUsesRealSeconds(unittest.TestCase):
    """M2 —— 实际监控时长必须是实测秒数, 不能靠配置里的 tick_hz 折算。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_changing_tick_hz_does_not_rescale_history(self):
        for i in range(10):
            self.store.coverage_tick(at(9, 0, i), pose_hit=True, face_hit=True,
                                     judging=True, dt=1.0)
        a = self.store.day_stats(DAY, tick_hz=5.0).monitored_seconds
        b = self.store.day_stats(DAY, tick_hz=10.0).monitored_seconds
        self.assertAlmostEqual(a, 10.0, delta=0.01)
        self.assertAlmostEqual(b, 10.0, delta=0.01,
                               msg="改 tick_hz 把历史统计改写了")

    def test_slow_loop_does_not_over_subtract(self):
        """循环掉到 1 Hz: 真实经过 60 秒, 但只有 6 个 tick。

        旧口径会算成 6/5=1.2 秒监控, 却扣掉 60 秒分心 -> 有效专注被夹成 0。
        """
        for i in range(6):
            self.store.coverage_tick(at(9, 0, i * 10), pose_hit=True, face_hit=True,
                                     judging=True, dt=10.0)
        self.store.event("episode_end", at=at(9, 1), duration=60)
        st = self.store.day_stats(DAY, tick_hz=5.0)
        self.assertAlmostEqual(st.monitored_seconds, 60.0, delta=0.5)
        self.assertAlmostEqual(st.focus_seconds, 0.0, delta=0.5)

    def test_old_rows_without_seconds_still_work(self):
        """老库没有 seconds 列(或那列是 0)时必须退回旧口径, 不能算成 0 小时。"""
        for i in range(20):
            self.store.coverage_tick(at(9, 0, i), pose_hit=True, face_hit=True,
                                     judging=True)          # dt 默认 0
        st = self.store.day_stats(DAY, tick_hz=5.0)
        self.assertAlmostEqual(st.monitored_seconds, 4.0, delta=0.01)


class TestCrossMidnightSpans(unittest.TestCase):
    """M7 —— 跨午夜要按天拆开, 否则一边谎报"崩溃"、一边把专注夹成 0。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        cfg = Config.from_dict(RAW, path=self.root / "config.toml")
        self.rt = Runtime(cfg, Calibration())
        self.rt.notifier = types.SimpleNamespace(buzz=lambda *a, **k: None)
        self.rt.ui = types.SimpleNamespace(show=lambda r: None, ask_reason=lambda *a: None,
                                          drain=lambda: [])

    def tearDown(self):
        self.rt.close()
        self.tmp.cleanup()

    def test_episode_spanning_midnight_is_split(self):
        self.rt.dispatch([EpisodeEnd(SignalKind.SCREEN,
                                     datetime(2026, 5, 2, 0, 5), 0.0, 600.0, 1, False,
                                     "证据")])
        # 起始日不该有**假的**孤儿(以前会谎报"程序崩溃")
        a = self.rt.store.day_stats("2026-05-01")
        self.assertEqual(a.unfinished_episodes, 0, "起始日凭空多了个假的'没等到收尾'")
        b = self.rt.store.day_stats("2026-05-02")
        # 时长按天拆开: 23:55–00:00 归 5/1, 00:00–00:05 归 5/2, 加起来还是 600 秒
        self.assertAlmostEqual(a.distract_seconds, 300.0, delta=2)
        self.assertAlmostEqual(b.distract_seconds, 300.0, delta=2)
        self.assertAlmostEqual(a.distract_seconds + b.distract_seconds, 600.0, delta=1)

    def test_same_day_span_is_untouched(self):
        self.rt.dispatch([EpisodeEnd(SignalKind.SCREEN, at(10, 0, 30), 0.0, 30.0, 1,
                                     False, "证据")])
        st = self.rt.store.day_stats(DAY)
        self.assertEqual(st.episodes, 1)
        self.assertAlmostEqual(st.distract_seconds, 30.0, delta=0.5)

    def test_away_spanning_midnight_is_split(self):
        self.rt.dispatch([AwayChange(False, datetime(2026, 5, 2, 0, 10), 0.0, 1200.0)])
        a = self.rt.store.day_stats("2026-05-01")
        b = self.rt.store.day_stats("2026-05-02")
        self.assertAlmostEqual(a.away_seconds + b.away_seconds, 1200.0, delta=2)
        self.assertEqual(a.unfinished_away, 0)
        self.assertEqual(b.unfinished_away, 0)

    def test_pause_spanning_midnight_is_split(self):
        self.rt.paused = True
        self.rt.pause_started = datetime(2026, 5, 1, 23, 50)
        self.rt.pause_reason = "跨夜"
        self.rt._close_pause(datetime(2026, 5, 2, 0, 10), reopen=False)
        a = self.rt.store.day_stats("2026-05-01")
        b = self.rt.store.day_stats("2026-05-02")
        self.assertAlmostEqual(a.pause_seconds + b.pause_seconds, 1200.0, delta=2)
        self.assertEqual(len(self.rt.store.pause_reasons("2026-05-01")), 1)
        self.assertEqual(len(self.rt.store.pause_reasons("2026-05-02")), 1)


if __name__ == "__main__":
    unittest.main()
