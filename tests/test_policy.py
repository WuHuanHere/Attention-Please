"""Policy 信号开关的单测。

M3 试跑只开 screen, 所以"未启用的信号必须连记录都不做"这条得钉死 ——
否则试跑数据里会混进还没验证过的推断信号, 让你误判是不是误报。
"""
from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.config import Calibration, Config  # noqa: E402
from attention_please.signals import (  # noqa: E402
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
        "daze_continuous_seconds": 60,
        "away_seconds": 90,
        "resume_grace_seconds": 20,
    },
    "reminder": {"max_level_high": 3, "max_level_mid": 2, "max_level_low": 1,
                 "escalate_after": [0, 10, 30], "cooldown_seconds": 180},
    "whitelist": {"words": ["Visual Studio Code"]},
    "blacklist": {"words": ["哔哩哔哩"]},
}

CAL = Calibration(phone_yaw_deg=30.0, book_pitch_deg=20.0, blink_rate_per_min=15.0,
                  is_calibrated=True)


def obs(ts: float, **kw) -> Observation:
    base = dict(pose_present=True, yaw=0.0, pitch=0.0, yaw_std=10.0, pitch_std=10.0,
                blink_rate=15.0, title="新标签页")
    base.update(kw)
    return Observation(ts=ts, at=BASE + timedelta(seconds=ts), **base)


class TestEnabledSignals(unittest.TestCase):
    def test_screen_only(self):
        sm = FocusStateMachine(Config.from_dict(RAW), CAL)
        pol = Policy(enabled=frozenset({"screen"}))
        acts = [a for ts in range(0, 60, 2)
                for a in sm.update(obs(ts, yaw=50.0), pol)]        # 看手机姿势
        self.assertEqual([a for a in acts if isinstance(a, (Nudge, EpisodeStart))], [])

    def test_screen_still_fires_when_only_screen_enabled(self):
        sm = FocusStateMachine(Config.from_dict(RAW), CAL)
        pol = Policy(enabled=frozenset({"screen"}))
        acts = [a for ts in range(0, 14)
                for a in sm.update(obs(ts, title="哔哩哔哩"), pol)]
        self.assertEqual([(a.signal, a.level) for a in acts if isinstance(a, Nudge)],
                         [(SignalKind.SCREEN, 1)])

    def test_all_enabled_by_default(self):
        sm = FocusStateMachine(Config.from_dict(RAW), CAL)
        acts = [a for ts in range(0, 60, 2)
                for a in sm.update(obs(ts, yaw=50.0), Policy())]
        self.assertTrue(any(isinstance(a, Nudge) and a.signal is SignalKind.PHONE
                            for a in acts))


if __name__ == "__main__":
    unittest.main()
