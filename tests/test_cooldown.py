"""冷却与静默上限的回归测试。

## 两个真实事故的教训

1. **冷却锚点错了**: 原来锚在"上一次分集结束", 于是
   `切窗口 → 分集结束 → 马上又切过去 → 分集结束 → …`
   会把冷却无限顺延 —— 实测出现过**连续 18 分钟一声不吭**。
   现在锚点是"上一次真的响过的时刻", 而且冷却只约束**这一集的第一声**
   (否则已经开始提醒的这一集, L2/L3 永远发不出来)。

2. **静默可能吃掉整段分心**: 实测有一段 **109 秒**的分心完全落在冷却窗口里,
   从头到尾没响。现在有一条硬底线: 一次分心连续超过
   `max_silent_episode_seconds`(默认 90)还没出过声, 就强行提醒一次。
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.config import Calibration, Config  # noqa: E402
from attention_please.signals import (  # noqa: E402
    FocusStateMachine,
    Nudge,
    Observation,
    Policy,
    SignalKind,
)

BASE = datetime(2026, 5, 1, 9, 0, 0)
TICK = 0.25
DISTRACT = "哔哩哔哩 (゜-゜)つロ 干杯~-bilibili"
NEUTRAL = "新标签页 - Chrome"

RAW = {
    "detection": {"screen_continuous_seconds": 10, "away_seconds": 90,
                  "resume_grace_seconds": 20},
    "reminder": {"max_level_high": 3, "max_level_mid": 2, "max_level_low": 1,
                 "escalate_after": [0, 10, 30],
                 "cooldown_seconds": 180,
                 "max_silent_episode_seconds": 90,
                 "wrong_feedback_mute_seconds": 900},
    "whitelist": {"words": ["Visual Studio Code"]},
    "blacklist": {"words": ["哔哩哔哩"]},
}

CAL = Calibration(phone_yaw_deg=30.0, book_pitch_deg=20.0, blink_rate_per_min=15.0,
                  is_calibrated=True)


def policy() -> Policy:
    return Policy(judging=True, allow_phone=False, quiet=True,
                  enabled=frozenset({"screen"}))


def feed(sm: FocusStateMachine, start: float, seconds: float, title: str):
    actions = []
    n = int(seconds / TICK)
    for i in range(n):
        ts = start + i * TICK
        actions.extend(sm.update(
            Observation(ts=ts, at=BASE + timedelta(seconds=ts), pose_present=True,
                        yaw=0.0, pitch=0.0, yaw_std=10.0, pitch_std=10.0,
                        blink_rate=15.0, eye_closed_ratio=0.05, idle_seconds=0.0,
                        title=title), policy()))
    return actions


def nudges(actions):
    return [(round(a.ts, 1), a.signal, a.level) for a in actions if isinstance(a, Nudge)]


class TestCooldown(unittest.TestCase):
    def setUp(self):
        self.sm = FocusStateMachine(Config.from_dict(RAW), CAL)

    def test_short_chained_episode_stays_silent(self):
        """刚提醒过, 又切过去只待了 25 秒 —— 不该再叫(这就是冷却存在的意义)。"""
        acts = feed(self.sm, 0, 25, DISTRACT)          # 分集@10 -> L1@10, L2@20
        acts += feed(self.sm, 25, 6, NEUTRAL)          # 回来, 分集结束(~30)
        acts += feed(self.sm, 31, 25, DISTRACT)        # 马上又切过去, 只待 25 秒
        acts += feed(self.sm, 56, 8, NEUTRAL)
        self.assertEqual([n[2] for n in nudges(acts)], [1, 2])

    def test_silence_cap_breaks_through_cooldown(self):
        """连续分心超过静默上限, 必须出声(109 秒一声不吭就是当年的故障)。

        注意计时起点是**分集开始**(信号出现 + 10 秒门槛), 不是信号出现。
        """
        acts = feed(self.sm, 0, 25, DISTRACT)          # L1@10, L2@20
        acts += feed(self.sm, 25, 6, NEUTRAL)
        acts += feed(self.sm, 31, 400, DISTRACT)       # 分集@41, 长时间分心
        late = [n for n in nudges(acts) if n[0] > 60]
        self.assertTrue(late, "静默上限没生效: 冷却把整段分心吃掉了")
        self.assertLessEqual(late[0][0], 41 + 90 + 2,
                             f"第一次强制提醒来得太晚: {late[0][0]:.0f}s")

    def test_escalation_not_blocked_by_cooldown(self):
        """**关键**: 已经开始提醒的这一集, 升级不能被冷却挡住(否则永远不会 L2/L3)。"""
        acts = feed(self.sm, 0, 45, DISTRACT)
        self.assertEqual([n[2] for n in nudges(acts)], [1, 2, 3])

    def test_forced_only_once_per_episode(self):
        """强行打破静默只做一次, 不能变成每 90 秒叫一次。"""
        acts = feed(self.sm, 0, 25, DISTRACT)          # L1@10, L2@20
        acts += feed(self.sm, 25, 6, NEUTRAL)
        acts += feed(self.sm, 31, 400, DISTRACT)       # 分集@41
        late = [n for n in nudges(acts) if n[0] > 60]
        self.assertEqual(len(late), 1, f"强行提醒重复了: {late}")

    def test_wrong_feedback_overrides_silence_cap(self):
        """你明确说了"判定错了", 那静默上限也不能反过来吵你。"""
        feed(self.sm, 0, 12, DISTRACT)
        self.sm.report_wrong(SignalKind.SCREEN, 12.0)
        acts = feed(self.sm, 12, 300, DISTRACT)
        self.assertEqual(nudges(acts), [])


if __name__ == "__main__":
    unittest.main()
