"""守「判定停止 / 退出」这条路径上的收口 —— 三个真实的静默丢数据 bug。

现场(2026-09-17):
  - 20:21:53 开了一段 screen 分心, 一直没有 episode_end -> 库里留下孤儿;
  - 21:29:11 离开座位(away_start), 最后一个时间表块 21:30:00 结束 -> 没有 away_end;
  - 那天日报写着「离开座位 **0 分钟**」, 而人明明走了。

根因: `_tick` 的非判定分支写的是 `self.sm.update(...)` —— **返回值被丢掉了**,
而 `suspend()` 正是靠返回值把进行中的分集和离开收口的。判定分支写的是
`self.dispatch(self.sm.update(...), frame)`, 少了一个 dispatch。

另外两个同源问题:
  - `pause_start` 没有对应的 `pause_end`(退出时、或生成日报时人还暂停着);
  - `tray._resume` 没有像 `_yield`/`_reclaim` 那样护栏, 记账失败时一声不响。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.config import Calibration, Config  # noqa: E402
from attention_please.runtime import Runtime  # noqa: E402
from attention_please.signals import Observation, Policy, SignalKind  # noqa: E402
from attention_please.tray import Tray  # noqa: E402

RAW = {
    "blacklist": {"words": ["哔哩哔哩"]},
    "schedule": {"block": [{"name": "测试", "start": "00:00", "end": "24:00"}]},
    "privacy": {"save_captures": False},
    # report_time 设成 00:00, 这样 _daily_jobs 在任何时刻都会真的走到生成日报那一步
    "report": {"daily_report": True, "report_time": "00:00"},
}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        cfg = Config.from_dict(RAW, path=self.root / "config.toml")
        self.rt = Runtime(cfg, Calibration())
        self.rt.notifier = types.SimpleNamespace(buzz=lambda *a, **k: None)
        self.rt.ui = types.SimpleNamespace(show=lambda r: None, ask_reason=lambda *a: None,
                                          drain=lambda: [])
        self.day = datetime.now().date().isoformat()

    def tearDown(self):
        self.rt.close()
        self.tmp.cleanup()

    def stop_judging(self) -> None:
        """让 _tick 认为"现在不判定" —— 等价于走出时间表 / 暂停 / 让出摄像头。"""
        self.rt._policy = lambda at, title=None: Policy(judging=False,
                                                       enabled=frozenset({"screen"}))

    def open_screen_episode(self, seconds: float = 12.0) -> None:
        pol = Policy(judging=True, enabled=frozenset({"screen"}))
        t0 = datetime(2026, 9, 20, 10, 0, 0)

        def frame(ts: float) -> Observation:
            return Observation(ts=ts, at=t0 + timedelta(seconds=ts), pose_present=True,
                               yaw=0.0, pitch=0.0, title="哔哩哔哩 - 视频")

        with mock.patch("attention_please.capture.save_composite",
                        return_value=pathlib.Path("x.jpg")):
            for ts in (0.0, seconds / 2, seconds):
                self.rt.dispatch(self.rt.sm.update(frame(ts), pol))


class TestSuspendActionsAreDispatched(Fixture):
    def test_open_episode_is_recorded_when_judging_stops(self):
        """分心途中走出时间表 -> episode_end 必须落库。"""
        self.open_screen_episode()
        self.assertEqual(self.rt.store.day_stats(self.day).episodes, 0)
        self.assertIsNotNone(self.rt.sm.tracks[SignalKind.SCREEN].episode_start)

        self.stop_judging()
        self.rt._tick()

        st = self.rt.store.day_stats(self.day)
        self.assertEqual(st.episodes, 1,
                         "状态机收口了但 episode_end 没落库 —— 分心时长凭空消失")
        self.assertGreater(st.distract_seconds, 0)
        self.assertEqual(st.unfinished_episodes, 0, "不该留下孤儿")

    def test_open_away_is_recorded_when_judging_stops(self):
        """离开座位途中走出时间表 -> away_end 必须落库(2026-09-17 那次)。"""
        pol = Policy(judging=True, enabled=frozenset({"screen"}))
        t0 = datetime(2026, 9, 20, 15, 0, 0)
        for ts in range(0, 200, 5):          # Pose 连续丢失 -> 90 秒后 away_start
            self.rt.dispatch(self.rt.sm.update(
                Observation(ts=float(ts), at=t0 + timedelta(seconds=ts),
                            pose_present=False), pol))
        n = list(self.rt.store.conn.execute(
            "SELECT COUNT(*) FROM event WHERE kind='away_start'"))[0][0]
        self.assertEqual(n, 1, "先要有一段离开")

        self.stop_judging()
        self.rt._tick()

        st = self.rt.store.day_stats(self.day)
        self.assertGreater(st.away_seconds, 0,
                           "away_end 被丢掉了 —— 日报会写「离开座位 0 分钟」")
        self.assertEqual(st.unfinished_away, 0)

    def test_unfinished_away_is_visible_when_it_really_is_lost(self):
        """对照组: 真的丢了(没有 away_end)时, 必须报出来而不是写 0。"""
        self.rt.store.event("away_start", at=datetime.now())
        st = self.rt.store.day_stats(self.day)
        self.assertEqual(st.unfinished_away, 1)
        self.assertEqual(st.away_seconds, 0.0)


class TestPauseIsClosed(Fixture):
    def test_shutdown_closes_an_open_pause(self):
        """暂停中直接关掉托盘 -> 时长和理由都必须留下(2026-09-18 21:00 那次)。"""
        self.rt.pause("今天学太累了", datetime.now() - timedelta(minutes=7))
        self.rt.shutdown()
        rows = list(self.rt.store.conn.execute(
            "SELECT duration, detail FROM event WHERE kind='pause_end'"))
        self.assertEqual(len(rows), 1, "退出时没有把进行中的暂停结账")
        self.assertGreaterEqual(rows[0][0], 7 * 60 - 5)
        self.assertEqual(rows[0][1], "今天学太累了")

    def test_report_generation_closes_and_reopens_the_pause(self):
        """生成日报时人还暂停着 -> 先结一段, 暂停继续计时。"""
        self.rt.pause("休息一下", datetime.now() - timedelta(minutes=3))
        self.rt._daily_jobs(datetime.now())
        rows = list(self.rt.store.conn.execute(
            "SELECT ROUND(duration), detail FROM event WHERE kind='pause_end'"))
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(rows[0][0], 170)
        self.assertEqual(rows[0][1], "休息一下")
        # 暂停还没结束: 起点被推到现在, 后面真的恢复时会结第二段
        self.assertTrue(self.rt.paused)
        self.assertIsNotNone(self.rt.pause_started)

    def test_resume_still_works(self):
        self.rt.pause("接电话", datetime.now() - timedelta(minutes=5))
        self.rt.resume(datetime.now())
        self.assertFalse(self.rt.paused)
        st = self.rt.store.day_stats(self.day)
        self.assertAlmostEqual(st.pause_seconds, 300, delta=5)
        self.assertEqual(len(self.rt.store.pause_reasons(self.day)), 1)


class TestTrayResumeDoesNotLie(Fixture):
    def test_resume_bookkeeping_failure_is_visible(self):
        tray = Tray(self.rt, self.rt.cfg.report_dir)
        notes: list[str] = []
        tray._notify = lambda m, title="attention_please": notes.append(m)

        def boom(*_a, **_k):
            raise OSError("database is locked")

        self.rt.resume = boom
        tray._resume()          # 不许把异常甩给 pystray
        self.assertEqual(len(notes), 1)
        self.assertIn("没记上账", notes[0])


if __name__ == "__main__":
    unittest.main()
