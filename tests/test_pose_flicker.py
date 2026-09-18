"""回归测试: Pose 抖动不能"饿死"信号判定。

## 这条守的是 2026-09-17 那次真实故障("该提醒却不提醒")

当时的代码里有一句:

    self._update_away(obs, actions)
    if obs.ts < self.resume_until:      # 坐下宽限期
        return actions                  # ← 直接跳过整个信号判定

而 `_update_away()` 会在**每一个"检测到人"的 tick** 把 `resume_until` 推成 `now + 20s`。
于是只要 Pose 在通/断之间抖动(低头、转头、光线差时极常见), 信号判定几乎永远被跳过:

  - 分心**不升级**(只响一次 L1, 后面 209 秒一声不吭);
  - 分集**超长**(断的间隙没超过 5 秒迟滞, 分集一直挂着);
  - 离开后**不判"回到座位"**。

修法: 记账永远照跑, 宽限期只用于"别刚坐下就念你"(抑制提醒, 不抑制记账)。
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
)

BASE = datetime(2026, 5, 1, 9, 0, 0)

RAW = {
    "detection": {
        "screen_continuous_seconds": 10,
        "phone_continuous_seconds": 20,
        "away_seconds": 90,
        "resume_grace_seconds": 20,
    },
    "reminder": {"max_level_high": 3, "max_level_mid": 2, "max_level_low": 1,
                 "escalate_after": [0, 10, 30], "cooldown_seconds": 180,
                 "wrong_feedback_mute_seconds": 900},
    "whitelist": {"words": ["Visual Studio Code"]},
    "blacklist": {"words": ["哔哩哔哩"]},
}

CAL = Calibration(phone_yaw_deg=30.0, book_pitch_deg=20.0, blink_rate_per_min=15.0,
                  is_calibrated=True)
TITLE_DISTRACT = "哔哩哔哩 (゜-゜)つロ 干杯~-bilibili"
TITLE_NEUTRAL = "新标签页 - Chrome"
TICK = 0.2


def obs(ts: float, *, present: bool = True, title: str = TITLE_NEUTRAL) -> Observation:
    return Observation(ts=ts, at=BASE + timedelta(seconds=ts), pose_present=present,
                       yaw=0.0, pitch=0.0, yaw_std=10.0, pitch_std=10.0,
                       blink_rate=15.0, eye_closed_ratio=0.05, idle_seconds=0.0,
                       title=title)


def policy() -> Policy:
    return Policy(judging=True, allow_phone=False, quiet=True,
                  enabled=frozenset({"screen"}))


def drive(sm: FocusStateMachine, seconds: float, *, present_pattern=None,
          title: str = TITLE_DISTRACT, start: float = 0.0):
    """按 5Hz 喂 seconds 秒。present_pattern: 可调用(第几个 tick) -> 是否检测到人。"""
    actions = []
    n = int(seconds / TICK)
    for i in range(n):
        ts = start + i * TICK
        present = True if present_pattern is None else present_pattern(i)
        actions.extend(sm.update(obs(ts, present=present, title=title), policy()))
    return actions


def nudges(actions):
    return [(a.signal, a.level) for a in actions if isinstance(a, Nudge)]


class TestPoseFlicker(unittest.TestCase):
    def test_flicker_still_escalates(self):
        """每两个 tick 抖一次 —— 修好之后必须照样升级到 L2/L3。"""
        sm = FocusStateMachine(Config.from_dict(RAW), CAL)
        actions = drive(sm, 45, present_pattern=lambda i: i % 2 == 0)
        levels = [lvl for _sig, lvl in nudges(actions)]
        self.assertIn(1, levels, "L1 都没发")
        self.assertIn(2, levels, "抖动时没升级到 L2(这正是当年的 bug)")
        self.assertIn(3, levels, "抖动时没升级到 L3")

    def test_flicker_does_not_end_episode(self):
        """间隙都小于迟滞(5s), 分集不该被切断。"""
        sm = FocusStateMachine(Config.from_dict(RAW), CAL)
        actions = drive(sm, 40, present_pattern=lambda i: i % 4 != 0)
        self.assertEqual([a for a in actions if isinstance(a, EpisodeEnd)], [])

    def test_flicker_does_not_report_away(self):
        """短暂丢失 Pose 不算"离开座位"(否则你会被莫名其妙记一笔)。"""
        sm = FocusStateMachine(Config.from_dict(RAW), CAL)
        actions = drive(sm, 60, present_pattern=lambda i: i % 3 != 0)
        self.assertEqual([a for a in actions if isinstance(a, AwayChange)], [])

    def test_single_tick_blip_does_not_delay_escalation(self):
        """中间丢一帧不能让升级时间往后拖。"""
        sm = FocusStateMachine(Config.from_dict(RAW), CAL)
        actions = drive(sm, 25, present_pattern=lambda i: i != 30)
        levels = [lvl for _sig, lvl in nudges(actions)]
        self.assertEqual(levels[:2], [1, 2])
        # L2 必须出现在"首次提醒后 10 秒"附近, 不能被宽限期推后
        second = [a for a in actions if isinstance(a, Nudge) and a.level == 2][0]
        first = [a for a in actions if isinstance(a, Nudge) and a.level == 1][0]
        self.assertLess(second.ts - first.ts, 11.5)

    def test_intermittent_signal_with_stable_pose(self):
        """标题抖动(通断交替, 间隙 < 迟滞)也一样要升级。"""
        sm = FocusStateMachine(Config.from_dict(RAW), CAL)
        actions = []
        for i in range(int(60 / TICK)):
            ts = i * TICK
            title = TITLE_DISTRACT if (i // 10) % 2 == 0 else TITLE_NEUTRAL
            actions.extend(sm.update(obs(ts, title=title), policy()))
        self.assertIn(3, [lvl for _sig, lvl in nudges(actions)])


class TestGenuineAwayReturn(unittest.TestCase):
    def test_away_then_return_is_reported(self):
        """真的离开 90 秒以上、再回来: 必须报告"回到座位"。"""
        sm = FocusStateMachine(Config.from_dict(RAW), CAL)
        actions = drive(sm, 120, present_pattern=lambda i: False)
        self.assertEqual([a for a in actions if isinstance(a, AwayChange) and a.away],
                         [a for a in actions if isinstance(a, AwayChange) and a.away])
        back = drive(sm, 5, start=120.0)
        returns = [a for a in back if isinstance(a, AwayChange) and not a.away]
        self.assertEqual(len(returns), 1, "离开后回来没报告(当年就是这个现象)")
        self.assertAlmostEqual(returns[0].duration, 120.0, places=1)

    def test_grace_suppresses_nudge_but_still_records(self):
        """刚坐下 20 秒内: 该记的记, 但别念你。"""
        sm = FocusStateMachine(Config.from_dict(RAW), CAL)
        drive(sm, 120, present_pattern=lambda i: False)          # 离开
        actions = drive(sm, 30, start=120.0)                     # 回来后一直在 B 站
        starts = [a for a in actions if isinstance(a, EpisodeStart)]
        self.assertEqual(len(starts), 1, "宽限期内也要记账(否则这段时间凭空消失)")
        early = [a for a in actions if isinstance(a, Nudge) and a.ts < 140.0]
        self.assertEqual(early, [], "刚坐下 20 秒内不该提醒")
        self.assertTrue(any(isinstance(a, Nudge) for a in actions),
                        "宽限期过了应该开始提醒")

    def test_grace_does_not_starve_bookkeeping(self):
        """宽限期内分集该结束就要结束, 不能挂着(当年那些 209 秒的超长分集就是这样来的)。"""
        sm = FocusStateMachine(Config.from_dict(RAW), CAL)
        drive(sm, 120, present_pattern=lambda i: False)
        actions = drive(sm, 30, start=120.0, title=TITLE_DISTRACT)   # 回来后坐在 B 站
        actions += drive(sm, 10, start=150.0, title=TITLE_NEUTRAL)   # 切回正常窗口
        ends = [a for a in actions if isinstance(a, EpisodeEnd)]
        self.assertEqual(len(ends), 1, f"期望 1 次分集结束, 实际 {len(ends)}")
        self.assertLess(ends[0].duration, 40, "分集时长被拉长了")


if __name__ == "__main__":
    unittest.main()
