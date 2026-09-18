"""M4 回归测试: Pose 弱证据通道(**只记录, 永远不许报警**)。

为什么这一层必须单独守: 它是唯一一个"输入本身就不可靠"的信号 —— Pose 的头部关键点是
模型从身体外推出来的, 精度低到按"误报最烦"的铁律连一声都不该响。它一旦漏出声音,
就是同时踩中两条铁律: **看不到脸还替你下结论 + 吵你**。

同时守住"记账正确": pose 分集进了 event 表, 但它不是分心 ——
算进分心会让"低头写题"直接变成"分心", 正好是最烦的那种误报。
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.config import Calibration, Config  # noqa: E402
from attention_please.pose_head import PoseHead  # noqa: E402
from attention_please.report import build_report  # noqa: E402
from attention_please.signals import (  # noqa: E402
    EpisodeEnd,
    EpisodeStart,
    FocusStateMachine,
    Nudge,
    Observation,
    Policy,
    SignalKind,
    evaluate,
)
from attention_please.store import Store  # noqa: E402

BASE = datetime(2026, 5, 1, 9, 0, 0)

RAW = {
    "detection": {
        "screen_continuous_seconds": 10,
        "phone_continuous_seconds": 20,
        "daze_continuous_seconds": 60,
        "pose_continuous_seconds": 10,
        "away_seconds": 90,
        "resume_grace_seconds": 20,
    },
    "reminder": {
        "max_level_high": 3, "max_level_mid": 2, "max_level_low": 1,
        "escalate_after": [0, 10, 30],
        "cooldown_seconds": 180,
        "max_silent_episode_seconds": 90,
        "wrong_feedback_mute_seconds": 900,
    },
    "whitelist": {"words": ["Visual Studio Code"]},
    "blacklist": {"words": ["哔哩哔哩"]},
}

CAL = Calibration(phone_yaw_deg=30.0, book_pitch_deg=20.0, screen_pitch_deg=10.0,
                  blink_rate_per_min=15.0, is_calibrated=True)


def make_cfg(**det_overrides) -> Config:
    raw = {k: (dict(v) if isinstance(v, dict) else v) for k, v in RAW.items()}
    raw["detection"].update(det_overrides)
    return Config.from_dict(raw)


def ph(*, head_down: bool = True, head_turned: bool = False, still: bool = True) -> PoseHead:
    return PoseHead(head_down=head_down, head_turned=head_turned, still=still,
                    bow=0.10, side=0.0, bow_delta=-0.35, side_delta=0.0,
                    ear_ratio=0.0, quality=0.9, samples=15)


def obs(ts: float, **kw) -> Observation:
    base = dict(pose_present=True, yaw=0.0, pitch=0.0, yaw_std=10.0, pitch_std=10.0,
                blink_rate=15.0, eye_closed_ratio=0.1, idle_seconds=0.0,
                title="新标签页 - Google Chrome")
    base.update(kw)
    return Observation(ts=ts, at=BASE + timedelta(seconds=ts), **base)


def feed(sm: FocusStateMachine, cfg: Config, start: float, seconds: float, **kw) -> list:
    """按 1 Hz 喂一段。**策略从 cfg 来**, 和 runtime._policy() 一致。

    不能图省事用 `Policy()`: 那会绕开 enabled_signals, 于是"关掉某个信号"的测试
    根本没测到东西(第一次写这几个测试时就踩了)。
    """
    policy = Policy(enabled=frozenset(cfg.detection.enabled_signals))
    actions = []
    ts = start
    while ts < start + seconds:
        ts += 1.0
        actions.extend(sm.update(obs(ts, **kw), policy))
    return actions


class TestPoseNeverNudges(unittest.TestCase):
    def test_evaluate_marks_pose_active_only_when_face_is_lost(self):
        cfg, cal = make_cfg(), CAL
        # 看不到脸(pitch=None) + Pose 说低头 -> 弱证据成立
        ev = evaluate(obs(0.0, pitch=None, pose_head=ph()), cfg, cal)[SignalKind.POSE]
        self.assertTrue(ev.active)
        self.assertIn("低头", ev.evidence)

    def test_face_visible_suppresses_pose_evidence(self):
        """看得到脸就有更好的证据, 不该再用 Pose 兜底(否则等于重复计数)。"""
        cfg, cal = make_cfg(), CAL
        ev = evaluate(obs(0.0, pitch=5.0, pose_head=ph()), cfg, cal)[SignalKind.POSE]
        self.assertFalse(ev.active)

    def test_pose_abstention_is_not_active(self):
        cfg, cal = make_cfg(), CAL
        ev = evaluate(obs(0.0, pitch=None, pose_head=None), cfg, cal)[SignalKind.POSE]
        self.assertFalse(ev.active)

    def test_upright_pose_is_not_active(self):
        cfg, cal = make_cfg(), CAL
        ev = evaluate(obs(0.0, pitch=None, pose_head=ph(head_down=False)),
                      cfg, cal)[SignalKind.POSE]
        self.assertFalse(ev.active)

    def test_max_level_is_hard_zero(self):
        sm = FocusStateMachine(make_cfg(), CAL)
        self.assertEqual(sm.max_level_for(SignalKind.POSE), 0)

    def test_long_pose_episode_emits_no_nudge(self):
        """喂到远远超过升级阶梯与静默上限, 也必须一声不响。"""
        cfg = make_cfg()
        sm = FocusStateMachine(cfg, CAL)
        actions = feed(sm, cfg, 0.0, 600.0, pitch=None, pose_head=ph())
        self.assertEqual([a for a in actions if isinstance(a, Nudge)], [])
        episodes = [a for a in actions if isinstance(a, EpisodeStart)]
        self.assertEqual([e.signal for e in episodes], [SignalKind.POSE])
        self.assertTrue(episodes[0].record_only)

    def test_pose_episode_is_flagged_record_only_and_level_zero(self):
        cfg = make_cfg()
        sm = FocusStateMachine(cfg, CAL)
        actions = feed(sm, cfg, 0.0, 30.0, pitch=None, pose_head=ph())
        # 信号消失后要过 5 秒迟滞才收口(和别的信号一个规矩)
        actions += feed(sm, cfg, 30.0, 8.0, pitch=None, pose_head=None)
        ends = [a for a in actions if isinstance(a, EpisodeEnd)]
        self.assertEqual(len(ends), 1)
        self.assertTrue(ends[0].record_only)
        self.assertEqual(ends[0].max_level, 0)
        self.assertGreater(ends[0].duration, 20.0)

    def test_pose_respects_enabled_signals_switch(self):
        """关掉就什么都不记 —— 和别的信号一个规矩。"""
        cfg = make_cfg(enabled_signals=["screen"])
        sm = FocusStateMachine(cfg, CAL)
        actions = feed(sm, cfg, 0.0, 60.0, pitch=None, pose_head=ph())
        self.assertEqual([a for a in actions if isinstance(a, EpisodeStart)], [])

    def test_pose_is_recorded_when_enabled(self):
        cfg = make_cfg(enabled_signals=["screen", "phone", "pose"])
        sm = FocusStateMachine(cfg, CAL)
        actions = feed(sm, cfg, 0.0, 30.0, pitch=None, pose_head=ph())
        self.assertEqual(len([a for a in actions if isinstance(a, EpisodeStart)]), 1)

    def test_policy_default_enables_every_switchable_signal(self):
        """默认值必须是"全开"(away 例外, 它不走 enabled_signals)。

        踩过的坑: Policy.enabled 曾经硬写 ["screen","phone","daze"], 于是新增 pose 之后
        单测里它永不触发、生产里却会触发 —— 两边行为不一致, 而单测正是用来兜底的。
        """
        switchable = {k.value for k in SignalKind} - {SignalKind.AWAY.value}
        self.assertEqual(Policy().enabled, frozenset(switchable))


class TestStoreAccounting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite3")
        self.day = BASE.date().isoformat()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_pose_episode_is_not_a_distraction(self):
        """最重要的一条: 低头写题不能被算成"分心"。"""
        at = BASE + timedelta(minutes=5)
        self.store.event("episode_start", at=at, signal="pose", duration=None)
        self.store.event("episode_end", at=at, signal="pose", duration=120.0)
        st = self.store.day_stats(self.day, tick_hz=5.0)
        self.assertEqual(st.episodes, 0)
        self.assertEqual(st.distract_seconds, 0.0)
        self.assertEqual(st.pose_weak_seconds, 120.0)

    def test_real_distraction_still_counts(self):
        at = BASE + timedelta(minutes=5)
        self.store.event("episode_end", at=at, signal="screen", duration=60.0)
        st = self.store.day_stats(self.day, tick_hz=5.0)
        self.assertEqual(st.episodes, 1)
        self.assertEqual(st.distract_seconds, 60.0)
        self.assertEqual(st.pose_weak_seconds, 0.0)

    def test_away_episode_is_not_a_distraction(self):
        at = BASE + timedelta(minutes=5)
        self.store.event("episode_end", at=at, signal="away", duration=900.0)
        st = self.store.day_stats(self.day, tick_hz=5.0)
        self.assertEqual(st.episodes, 0)
        self.assertEqual(st.distract_seconds, 0.0)

    def test_busiest_hours_excludes_away_and_pose(self):
        at = BASE + timedelta(minutes=5)
        self.store.event("episode_end", at=at, signal="pose", duration=600.0)
        self.store.event("episode_end", at=at, signal="away", duration=600.0)
        self.store.event("episode_end", at=at, signal="screen", duration=30.0)
        hours = self.store.busiest_distraction_hours(self.day)
        self.assertEqual(hours, [(9, 30.0)])


class TestReportBreakdown(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.sqlite3")
        self.day = BASE.date().isoformat()
        self.cfg = make_cfg()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _blind_minute(self, minute: str, ticks: int = 300, pose_hits: int = 300,
                      face_hits: int = 30):
        self.store.conn.execute(
            "INSERT INTO coverage_minute (day, minute, ticks, pose_hits, face_hits,"
            " judging) VALUES (?,?,?,?,?,?)",
            (self.day, f"{self.day}T{minute}", ticks, pose_hits, face_hits, ticks))
        self.store.conn.commit()

    def test_blind_time_is_split_into_paper_mode_and_truly_unseen(self):
        self._blind_minute("09:00")
        self._blind_minute("09:01")
        at = BASE + timedelta(minutes=1)
        self.store.event("episode_end", at=at, signal="pose", duration=60.0)
        text = build_report(self.cfg, self.store, self.day, planned_seconds=3600.0)
        self.assertIn("看不清", text)
        self.assertIn("疑似纸笔模式", text)
        self.assertIn("什么都没看到", text)

    def test_no_breakdown_line_when_there_is_no_pose_evidence(self):
        self._blind_minute("09:00")
        text = build_report(self.cfg, self.store, self.day, planned_seconds=3600.0)
        self.assertNotIn("疑似纸笔模式", text)

    def test_no_remainder_line_when_pose_covers_the_blind_time(self):
        """Pose 覆盖了全部看不清时间时, 不许印出"剩下约 0 分钟"这种噪声。"""
        self._blind_minute("09:00")
        at = BASE + timedelta(minutes=1)
        self.store.event("episode_end", at=at, signal="pose", duration=60.0)
        text = build_report(self.cfg, self.store, self.day, planned_seconds=3600.0)
        self.assertIn("疑似纸笔模式", text)
        self.assertNotIn("什么都没看到", text)


class TestConfigKeys(unittest.TestCase):
    def test_pose_keys_are_parsed(self):
        cfg = make_cfg(pose_continuous_seconds=7, pose_head_down_k=2.0,
                       pose_head_turn_k=3.0, pose_head_min_margin=0.11)
        d = cfg.detection
        self.assertEqual(d.pose_continuous_seconds, 7)
        self.assertEqual(d.pose_head_down_k, 2.0)
        self.assertEqual(d.pose_head_turn_k, 3.0)
        self.assertEqual(d.pose_head_min_margin, 0.11)

    def test_shipped_config_enables_pose_and_parses(self):
        """真正的 config.toml 必须能被读出来 —— 别让手写注释把键写错。"""
        cfg = Config.load()
        self.assertIn("pose", cfg.detection.enabled_signals)
        self.assertGreater(cfg.detection.pose_head_min_margin, 0)
        self.assertGreater(cfg.detection.pose_continuous_seconds, 0)


if __name__ == "__main__":
    unittest.main()
