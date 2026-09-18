"""纯逻辑单测: 信号判定 + 分集状态机 + 升级/冷却/离开/暂停(M2)。

这是最该被测透的一层: 它决定"什么时候吵你", 而你的底线是误报最烦。
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.config import Calibration, Config  # noqa: E402
from attention_please.signals import (  # noqa: E402
    AwayChange,
    EpisodeEnd,
    EpisodeStart,
    FocusStateMachine,
    Nudge,
    Observation,
    Policy,
    SignalKind,
    evaluate,
)

BASE = datetime(2026, 5, 1, 9, 0, 0)

RAW = {
    "detection": {
        "screen_continuous_seconds": 10,
        "phone_continuous_seconds": 20,
        "away_seconds": 90,
        "resume_grace_seconds": 20,
    },
    "reminder": {
        "max_level_high": 3, "max_level_mid": 2, "max_level_low": 1,
        "escalate_after": [0, 10, 30],
        "cooldown_seconds": 180,
        "wrong_feedback_mute_seconds": 900,
    },
    "whitelist": {"words": ["Visual Studio Code", "考研"]},
    "blacklist": {"words": ["哔哩哔哩", "Steam"]},
}


def make_cfg(**det_overrides) -> Config:
    raw = {k: (dict(v) if isinstance(v, dict) else v) for k, v in RAW.items()}
    raw["detection"].update(det_overrides)
    return Config.from_dict(raw)


CAL = Calibration(phone_yaw_deg=30.0, book_pitch_deg=20.0, screen_pitch_deg=10.0,
                  blink_rate_per_min=15.0, is_calibrated=True)


def obs(ts: float, **kw) -> Observation:
    base = dict(pose_present=True, yaw=0.0, pitch=0.0, yaw_std=10.0, pitch_std=10.0,
                blink_rate=15.0, eye_closed_ratio=0.1, idle_seconds=0.0,
                title="新标签页 - Google Chrome")
    base.update(kw)
    return Observation(ts=ts, at=BASE + timedelta(seconds=ts), **base)


def run(sm: FocusStateMachine, ticks, policy: Policy | None = None):
    """把一串 (ts, obs_override) 喂进去, 收集所有动作。"""
    actions = []
    for ts, kw in ticks:
        actions.extend(sm.update(obs(ts, **kw), policy or Policy()))
    return actions


def nudges(actions):
    return [(a.signal, a.level) for a in actions if isinstance(a, Nudge)]


class TestEvaluate(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()

    def test_screen_distraction_requires_title_hit(self):
        ev = evaluate(obs(0, title="哔哩哔哩 - Chrome"), self.cfg, CAL)
        self.assertTrue(ev[SignalKind.SCREEN].active)
        self.assertIn("哔哩哔哩", ev[SignalKind.SCREEN].evidence)

    def test_whitelist_wins(self):
        ev = evaluate(obs(0, title="考研数学 - Bilibili 课堂"), self.cfg, CAL)
        self.assertFalse(ev[SignalKind.SCREEN].active)

    def test_looking_down_at_book_ignores_window(self):
        """低头写题时不判窗口 —— 否则你挂着网课页面就是误报。"""
        ev = evaluate(obs(0, title="哔哩哔哩 - Chrome", pitch=35.0), self.cfg, CAL)
        self.assertFalse(ev[SignalKind.SCREEN].active)

    def test_phone_requires_right_yaw(self):
        self.assertTrue(evaluate(obs(0, yaw=45.0), self.cfg, CAL)[SignalKind.PHONE].active)
        self.assertFalse(evaluate(obs(0, yaw=20.0), self.cfg, CAL)[SignalKind.PHONE].active)

    def test_phone_needs_pose(self):
        self.assertFalse(
            evaluate(obs(0, pose_present=False, yaw=45.0), self.cfg, CAL)[SignalKind.PHONE].active)

    def test_daze_is_gone(self):
        """发呆已于 2026-09-18 砍掉 —— 别让它悄悄回来。

        它依赖人脸关键点, 而低头写题时人脸覆盖率只有 12-15%; 眨眼闸门又几乎永不成立。
        真要做回来, 得先推翻 signals.py 模块 docstring 里那三条证据。
        """
        self.assertNotIn("daze", {k.value for k in SignalKind})
        self.assertNotIn("daze", Policy().enabled)
        self.assertFalse(hasattr(self.cfg.detection, "daze_continuous_seconds"))
        # 低头姿态下不该凭空冒出任何"发呆类"信号
        ev = evaluate(obs(0, pitch=35.0, yaw=0.0, yaw_std=1.0, pitch_std=1.0,
                          blink_rate=5.0), self.cfg, CAL)
        self.assertEqual({k.value for k, v in ev.items() if v.active}, set())


class TestScreenEpisode(unittest.TestCase):
    def setUp(self):
        self.cfg = make_cfg()
        self.sm = FocusStateMachine(self.cfg, CAL)

    def test_no_nudge_before_threshold(self):
        acts = run(self.sm, [(0, {"title": "Steam"}), (6, {"title": "Steam"})])
        self.assertEqual(nudges(acts), [])
        self.assertEqual([a for a in acts if isinstance(a, EpisodeStart)], [])

    def test_nudge_after_continuous_threshold(self):
        acts = run(self.sm, [(t, {"title": "Steam"}) for t in range(0, 13, 1)])
        self.assertEqual(nudges(acts), [(SignalKind.SCREEN, 1)])
        self.assertEqual(len([a for a in acts if isinstance(a, EpisodeStart)]), 1)

    def test_single_tick_blip_never_fires(self):
        """查资料时切一下窗口(2 秒)不该被念 —— 这是最典型的误报。"""
        acts = run(self.sm, [(0, {"title": "Steam"}), (2, {}), (4, {}), (6, {})])
        self.assertEqual(nudges(acts), [])

    def test_escalation_and_cap_high(self):
        acts = run(self.sm, [(t, {"title": "Steam"}) for t in range(0, 45, 2)])
        self.assertEqual(nudges(acts), [(SignalKind.SCREEN, 1), (SignalKind.SCREEN, 2),
                                        (SignalKind.SCREEN, 3)])

    def test_episode_end_reports_duration(self):
        ticks = [(t, {"title": "Steam"}) for t in range(0, 15)] + \
                [(t, {}) for t in range(15, 25)]
        acts = run(self.sm, ticks)
        ends = [a for a in acts if isinstance(a, EpisodeEnd)]
        self.assertEqual(len(ends), 1)
        self.assertGreater(ends[0].duration, 10)
        self.assertEqual(ends[0].max_level, 1)


class TestCaps(unittest.TestCase):
    def test_phone_capped_at_two(self):
        sm = FocusStateMachine(make_cfg(), CAL)
        acts = run(sm, [(t, {"yaw": 50.0}) for t in range(0, 70, 2)])
        self.assertEqual(nudges(acts), [(SignalKind.PHONE, 1), (SignalKind.PHONE, 2)])

    def test_confidence_tiers_cap_the_level(self):
        """可信度分级表本身要守住 —— 推断出来的信号不许升到最吵的那一档。

        (原来这条测的是"发呆只响一声"; 发呆砍掉后, 低可信档只剩 away,
        所以直接钉住整张表, 顺带守住 POSE 的硬性 0。)
        """
        sm = FocusStateMachine(make_cfg(), CAL)
        self.assertEqual(sm.max_level_for(SignalKind.SCREEN), 3)   # high
        self.assertEqual(sm.max_level_for(SignalKind.PHONE), 2)    # mid
        self.assertEqual(sm.max_level_for(SignalKind.AWAY), 1)     # low
        self.assertEqual(sm.max_level_for(SignalKind.POSE), 0)     # 只记录


class TestRecordOnly(unittest.TestCase):
    def test_allow_phone_records_without_nudging(self):
        """背单词时段: 右偏看手机只入库, 不报警。"""
        sm = FocusStateMachine(make_cfg(), CAL)
        pol = Policy(judging=True, allow_phone=True, block_name="英语单词")
        acts = run(sm, [(t, {"yaw": 50.0}) for t in range(0, 60, 2)], pol)
        self.assertEqual(nudges(acts), [])
        starts = [a for a in acts if isinstance(a, EpisodeStart)]
        self.assertEqual(len(starts), 1)
        self.assertTrue(starts[0].record_only)

    def test_allow_phone_does_not_suppress_screen(self):
        sm = FocusStateMachine(make_cfg(), CAL)
        pol = Policy(allow_phone=True, block_name="英语单词")
        acts = run(sm, [(t, {"title": "Steam"}) for t in range(0, 14)], pol)
        self.assertEqual(nudges(acts), [(SignalKind.SCREEN, 1)])

    def test_quiet_suppresses_sound_but_not_popup(self):
        sm = FocusStateMachine(make_cfg(), CAL)
        acts = run(sm, [(t, {"title": "Steam"}) for t in range(0, 13)], Policy(quiet=True))
        ns = [a for a in acts if isinstance(a, Nudge)]
        self.assertEqual(len(ns), 1)
        self.assertFalse(ns[0].sound)


class TestCooldownAndFeedback(unittest.TestCase):
    def test_cooldown_suppresses_repeat(self):
        sm = FocusStateMachine(make_cfg(), CAL)
        ticks = [(t, {"title": "Steam"}) for t in range(0, 12)]      # 第一次分心
        ticks += [(t, {}) for t in range(12, 20)]                    # 回来
        ticks += [(t, {"title": "Steam"}) for t in range(20, 40)]    # 马上又切过去
        acts = run(sm, ticks)
        self.assertEqual(nudges(acts), [(SignalKind.SCREEN, 1)])     # 冷却期内只记录不吵

    def test_repeat_nudges_after_cooldown_expires(self):
        sm = FocusStateMachine(make_cfg(), CAL)
        ticks = [(t, {"title": "Steam"}) for t in range(0, 12)]
        ticks += [(t, {}) for t in range(12, 20)]
        ticks += [(t, {"title": "Steam"}) for t in range(20, 230, 5)]
        acts = run(sm, ticks)
        self.assertGreaterEqual(len(nudges(acts)), 2)

    def test_wrong_feedback_mutes_signal(self):
        sm = FocusStateMachine(make_cfg(), CAL)
        acts = run(sm, [(t, {"title": "Steam"}) for t in range(0, 12)])
        self.assertEqual(len(nudges(acts)), 1)
        sm.report_wrong(SignalKind.SCREEN, 12)
        acts2 = run(sm, [(t, {"title": "Steam"}) for t in range(13, 300, 5)])
        self.assertEqual(nudges(acts2), [])

    def test_wrong_feedback_only_mutes_that_signal(self):
        sm = FocusStateMachine(make_cfg(), CAL)
        run(sm, [(t, {"title": "Steam"}) for t in range(0, 12)])
        sm.report_wrong(SignalKind.SCREEN, 12)
        acts = run(sm, [(t, {"yaw": 50.0}) for t in range(13, 40)])
        self.assertEqual(nudges(acts), [(SignalKind.PHONE, 1)])


class TestAwayAndSuspend(unittest.TestCase):
    def test_away_reported_after_threshold(self):
        sm = FocusStateMachine(make_cfg(), CAL)
        acts = run(sm, [(t, {"pose_present": False}) for t in range(0, 100, 10)])
        away = [a for a in acts if isinstance(a, AwayChange) and a.away]
        self.assertEqual(len(away), 1)
        self.assertGreaterEqual(away[0].ts, 90)

    def test_return_reports_duration_and_grace(self):
        sm = FocusStateMachine(make_cfg(), CAL)
        run(sm, [(t, {"pose_present": False}) for t in range(0, 100, 10)])
        acts = run(sm, [(100, {}), (110, {"title": "Steam"}), (118, {"title": "Steam"})])
        back = [a for a in acts if isinstance(a, AwayChange) and not a.away]
        self.assertEqual(len(back), 1)
        self.assertAlmostEqual(back[0].duration, 100.0, places=1)
        self.assertEqual(nudges(acts), [])          # 刚坐下的宽限期内不吵

    def test_away_not_reported_for_short_gap(self):
        sm = FocusStateMachine(make_cfg(), CAL)
        acts = run(sm, [(t, {"pose_present": False}) for t in range(0, 60, 10)])
        self.assertEqual([a for a in acts if isinstance(a, AwayChange) and a.away], [])

    def test_outside_schedule_suspends_and_closes_episode(self):
        sm = FocusStateMachine(make_cfg(), CAL)
        run(sm, [(t, {"title": "Steam"}) for t in range(0, 14)])
        acts = sm.update(obs(14, title="Steam"), Policy(judging=False))
        self.assertEqual(len([a for a in acts if isinstance(a, EpisodeEnd)]), 1)

    def test_snapshot_shape(self):
        sm = FocusStateMachine(make_cfg(), CAL)
        run(sm, [(t, {"title": "Steam"}) for t in range(0, 14)])
        snap = sm.snapshot(14.0)
        self.assertTrue(snap["screen"]["in_episode"])
        self.assertEqual(snap["screen"]["level"], 1)
        self.assertFalse(snap["phone"]["in_episode"])


if __name__ == "__main__":
    unittest.main()
