"""离开座位(人不在检测框里)的行为与记账测试。

守两件事:

1. **记账不能跨"不判定"的时段**。真实风险时间线:
     11:20 离开(已报告) -> 11:30 时间表结束(不判定) -> 午休 -> 13:00 回来
   旧代码里离开状态会一直挂着, 回来时算成"离开 1 小时 40 分", 其中 1.5 小时是午休 ——
   而午休本来就不在"监控时间"里, 日报却会把这段从未计入的时间又从"有效专注"里扣一次。
   现在: 判定一停就把离开结掉, 恢复后若人还不在, 90 秒后重新开一段。

2. **托盘必须看得出来**。人不在画面里时图标不能还是绿的 —— 否则你会以为它在管你,
   其实它谁也看不见。
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
    FocusStateMachine,
    Nudge,
    Observation,
    Policy,
    SignalKind,
)
from attention_please.tray import STATE_COLORS  # noqa: E402

BASE = datetime(2026, 5, 1, 11, 20, 0)
TICK = 0.2

RAW = {
    "detection": {"screen_continuous_seconds": 10, "away_seconds": 90,
                  "resume_grace_seconds": 20},
    "reminder": {"max_level_high": 3, "escalate_after": [0, 10, 30],
                 "cooldown_seconds": 180, "max_silent_episode_seconds": 90},
    "whitelist": {"words": ["考研"]},
    "blacklist": {"words": ["哔哩哔哩"]},
    "schedule": {"block": [{"name": "上午", "start": "08:30", "end": "11:30"}]},
    "report": {"daily_report": False},
}

CAL = Calibration(phone_yaw_deg=19.0, book_pitch_deg=17.4, blink_rate_per_min=15.0,
                  is_calibrated=True)


def obs(ts: float, *, present: bool, title: str = "新标签页") -> Observation:
    return Observation(ts=ts, at=BASE + timedelta(seconds=ts), pose_present=present,
                       yaw=0.0, pitch=0.0, yaw_std=10.0, pitch_std=10.0,
                       blink_rate=15.0, eye_closed_ratio=0.05, idle_seconds=0.0,
                       title=title)


def policy(judging: bool = True, quiet: bool = True) -> Policy:
    return Policy(judging=judging, allow_phone=False, quiet=quiet,
                  enabled=frozenset({"screen"}))


def drive(sm, start: float, seconds: float, *, present: bool, judging: bool = True,
          title: str = "新标签页"):
    actions = []
    for i in range(int(seconds / TICK)):
        ts = start + i * TICK
        actions.extend(sm.update(obs(ts, present=present, title=title),
                                 policy(judging)))
    return actions


class TestAwayBasics(unittest.TestCase):
    def setUp(self):
        self.sm = FocusStateMachine(Config.from_dict(RAW), CAL)

    def test_no_away_before_threshold(self):
        actions = drive(self.sm, 0, 89, present=False)
        self.assertEqual([a for a in actions if isinstance(a, AwayChange)], [])

    def test_away_reported_at_threshold(self):
        actions = drive(self.sm, 0, 95, present=False)
        away = [a for a in actions if isinstance(a, AwayChange) and a.away]
        self.assertEqual(len(away), 1)
        self.assertGreaterEqual(away[0].ts, 90)

    def test_short_gap_is_invisible(self):
        """离开 60 秒再回来: 不该产生任何离开事件。"""
        actions = drive(self.sm, 0, 60, present=False)
        actions += drive(self.sm, 60, 5, present=True)
        self.assertEqual([a for a in actions if isinstance(a, AwayChange)], [])

    def test_return_reports_duration(self):
        drive(self.sm, 0, 120, present=False)
        actions = drive(self.sm, 120, 5, present=True)
        back = [a for a in actions if isinstance(a, AwayChange) and not a.away]
        self.assertEqual(len(back), 1)
        self.assertAlmostEqual(back[0].duration, 120.0, places=1)

    def test_ongoing_episode_ends_when_leaving(self):
        """分心到一半人走了: 分集要在 5 秒迟滞后结束, 而不是挂在那里。"""
        actions = drive(self.sm, 0, 30, present=True, title="哔哩哔哩")
        actions += drive(self.sm, 30, 20, present=False)
        ends = [a for a in actions if isinstance(a, EpisodeEnd)]
        self.assertEqual(len(ends), 1)
        self.assertLess(ends[0].ts, 40)


class TestAwayDoesNotSpanNonJudgingGaps(unittest.TestCase):
    def test_away_closed_when_judging_stops(self):
        sm = FocusStateMachine(Config.from_dict(RAW), CAL)
        drive(sm, 0, 120, present=False)                 # 离开并被报告
        actions = sm.update(obs(120, present=False), policy(judging=False))
        backs = [a for a in actions if isinstance(a, AwayChange) and not a.away]
        self.assertEqual(len(backs), 1, "判定停止时没有把离开状态收口")
        self.assertAlmostEqual(backs[0].duration, 120.0, places=1)

    def test_return_after_lunch_does_not_count_lunch(self):
        """11:20 离开 -> 11:22 时间表结束(判定停) -> 午休 -> 13:00 回来: 不能算成 1 小时 40 分。"""
        sm = FocusStateMachine(Config.from_dict(RAW), CAL)
        drive(sm, 0, 120, present=False)                       # 离开并被报告
        # 判定在这一刻停止(时间表结束) —— 离开必须在这里收口
        closed = sm.update(obs(120, present=False), policy(judging=False))
        backs = [a for a in closed if isinstance(a, AwayChange) and not a.away]
        self.assertEqual(len(backs), 1, "判定停止时没有把离开收口")
        self.assertAlmostEqual(backs[0].duration, 120.0, places=1)
        # 午休: 时间跨越 1.5 小时, 期间只有几次"不判定"的 tick
        for t in (600.0, 3000.0, 6000.0):
            sm.update(obs(t, present=False), policy(judging=False))
        # 13:00 恢复判定, 人还不在 -> 重新开一段(90 秒门槛); 100 秒后回来
        drive(sm, 6000.0, 100, present=False)
        actions = drive(sm, 6100.0, 5, present=True)
        backs = [a for a in actions if isinstance(a, AwayChange) and not a.away]
        self.assertEqual(len(backs), 1)
        self.assertLess(backs[0].duration, 130.0,
                        f"离开时长把午休算进去了: {backs[0].duration:.0f}s")

    def test_still_away_after_resume_opens_new_away(self):
        """午休时人一直不在: 恢复判定后应重新开一段离开(90 秒门槛)。"""
        sm = FocusStateMachine(Config.from_dict(RAW), CAL)
        drive(sm, 0, 120, present=False)
        sm.update(obs(600, present=False), policy(judging=False))
        actions = drive(sm, 600, 120, present=False)
        away = [a for a in actions if isinstance(a, AwayChange) and a.away]
        self.assertEqual(len(away), 1)
        self.assertAlmostEqual(away[0].ts, 600 + 90, delta=1.0)


class TestTrayShowsAway(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        cfg = Config.from_dict(RAW, path=self.root / "config.toml")
        self.rt = Runtime(cfg, Calibration())
        self.rt.notifier = types.SimpleNamespace(buzz=lambda *a, **k: None)
        self.rt.ui = types.SimpleNamespace(show=lambda req: None,
                                           ask_reason=lambda *a: None, drain=lambda: [])

    def tearDown(self):
        self.rt.close()
        self.tmp.cleanup()

    def test_away_state_has_its_own_colour(self):
        self.assertIn("away", STATE_COLORS)
        self.assertNotEqual(STATE_COLORS["away"], STATE_COLORS["judging"])

    def test_state_key_reports_away(self):
        # 时间表在 11:30 结束; 用 11:00 构造一个"判定中且已报告离开"的状态
        self.rt.sm.away_since = 0.0
        self.rt.sm.away_reported = True
        self.rt._policy = lambda at: types.SimpleNamespace(judging=True, block_name="上午")
        self.assertEqual(self.rt.state_key(), "away")
        self.assertIn("离开座位", self.rt.status_text())

    def test_state_key_judging_when_present(self):
        self.rt._policy = lambda at: types.SimpleNamespace(judging=True, block_name="上午")
        self.assertEqual(self.rt.state_key(), "judging")


class TestAwayReminder(unittest.TestCase):
    """离开太久要提醒一次(默认 15 分钟门槛), 而且**一次离开只提醒一次**。"""

    def setUp(self):
        self.sm = FocusStateMachine(Config.from_dict(RAW), CAL)

    def _nudges(self, actions):
        return [a for a in actions if isinstance(a, Nudge)]

    def test_no_reminder_before_threshold(self):
        actions = drive(self.sm, 0, 600, present=False)          # 10 分钟
        self.assertEqual(self._nudges(actions), [])

    def test_one_reminder_after_threshold(self):
        actions = drive(self.sm, 0, 960, present=False)          # 16 分钟
        ns = self._nudges(actions)
        self.assertEqual(len(ns), 1)
        self.assertIs(ns[0].signal, SignalKind.AWAY)
        self.assertEqual(ns[0].level, 1, "离开提醒的级别上限应该是 1 声")
        self.assertIn("已经离开座位", ns[0].evidence)

    def test_long_absence_still_only_one_reminder(self):
        actions = drive(self.sm, 0, 3600, present=False)         # 整整一小时
        self.assertEqual(len(self._nudges(actions)), 1, "一次离开被重复提醒了")

    def test_disabled_by_zero(self):
        raw = {**RAW, "detection": {**RAW["detection"], "away_reminder_seconds": 0}}
        sm = FocusStateMachine(Config.from_dict(raw), CAL)
        actions = drive(sm, 0, 1800, present=False)
        self.assertEqual(self._nudges(actions), [])

    def test_channel_follows_quiet_hours(self):
        """白天出声、安静时段只弹窗 —— 声音/弹窗由 quiet 决定。"""
        day = FocusStateMachine(Config.from_dict(RAW), CAL)
        night = FocusStateMachine(Config.from_dict(RAW), CAL)
        day_nudges = [a for a in drive(day, 0, 960, present=False) if isinstance(a, Nudge)]
        # 用一个"不安静"的 policy 重放
        sm = FocusStateMachine(Config.from_dict(RAW), CAL)
        loud = []
        for i in range(int(960 / TICK)):
            ts = i * TICK
            loud.extend(sm.update(obs(ts, present=False), policy(quiet=False)))
        self.assertEqual(len(day_nudges), 1)
        self.assertFalse(day_nudges[0].sound, "安静时段应该不发声")
        self.assertTrue([a for a in loud if isinstance(a, Nudge)][0].sound,
                        "非安静时段应该发声")

    def test_returning_resets_and_can_remind_again(self):
        actions = drive(self.sm, 0, 960, present=False)          # 提醒一次
        self.assertEqual(len(self._nudges(actions)), 1)
        drive(self.sm, 960, 10, present=True)                    # 回来了
        again = drive(self.sm, 970, 960, present=False)          # 又离开 16 分钟
        self.assertEqual(len(self._nudges(again)), 1, "再次离开应该重新提醒")

    def test_away_episode_is_not_counted_as_distraction(self):
        """离开的分集不能算进"分心", 否则有效专注被扣两次。"""
        store = None
        import tempfile
        from attention_please.store import Store
        tmp = tempfile.TemporaryDirectory()
        try:
            store = Store(pathlib.Path(tmp.name) / "t.sqlite3")
            now = datetime(2026, 9, 18, 10, 0)
            for i in range(300):
                store.coverage_tick(now, pose_hit=False, face_hit=False, judging=True)
            store.event("episode_end", at=now, signal="away", duration=900)
            store.event("nudge", at=now, signal="away", level=1)
            st = store.day_stats("2026-09-18", tick_hz=5)
            self.assertEqual(st.episodes, 0, "离开被算成了分心次数")
            self.assertAlmostEqual(st.distract_seconds, 0.0)
            self.assertEqual(st.away_nudges, 1)
            self.assertEqual(st.nudges, 0)
        finally:
            if store is not None:
                store.close()
            tmp.cleanup()


class TestAwayPopupAlwaysShown(unittest.TestCase):
    """离开的提醒必须弹窗(你不在座位上, 回来得看见它凭什么响过), 而且不自动关闭。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        cfg = Config.from_dict(RAW, path=self.root / "config.toml")
        self.rt = Runtime(cfg, Calibration())
        self.rt.notifier = types.SimpleNamespace(buzz=lambda *a, **k: None)

    def tearDown(self):
        self.rt.close()
        self.tmp.cleanup()

    def test_away_l1_with_sound_still_pops_up(self):
        reqs = []
        self.rt.ui = types.SimpleNamespace(show=lambda req: reqs.append(req))
        now = datetime.now()
        self.rt.dispatch([Nudge(SignalKind.AWAY, 1, "已经离开座位 16 分钟", True,
                                now, 0.0, "上午")])
        self.assertEqual(len(reqs), 1, "离开提醒没有弹窗")
        self.assertEqual(reqs[0].auto_close_seconds, 0.0, "离开提醒不该自动关闭")
        self.assertIn("离开座位", reqs[0].title)

    def test_other_signals_l1_with_sound_still_quiet(self):
        reqs = []
        self.rt.ui = types.SimpleNamespace(show=lambda req: reqs.append(req))
        now = datetime.now()
        self.rt.dispatch([Nudge(SignalKind.SCREEN, 1, "证据", True, now, 0.0, "上午")])
        self.assertEqual(reqs, [], "普通 L1 有声时不该弹窗")


if __name__ == "__main__":
    unittest.main()
