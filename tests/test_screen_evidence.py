"""「切到娱乐窗口」的判定证据测试。

## 守的是 2026-09-17 那次**误报**

你说: "我刚刚应该一直低头在写题目" —— 而系统却在提醒"切到了娱乐窗口"。

根因: `evaluate()` 里把**拿不到头姿**当成了"你在看屏幕":

    looking_at_screen = obs.pitch is None or obs.pitch < cal.book_pitch_deg
    #                  ^^^^^^^^^^^^^^^^^^ 低头写题时脸出画面 -> pitch 是 None -> 误判为在看屏幕

于是"把 QQ / B 站晾在前台、自己低头写题"一路被当成分心。

现在的口径(回到你最初的设计: 低头 = 纸笔模式, 不判窗口):

    face_visible = obs.pitch is not None            # 看不到脸 = 不知道你在看什么, 不猜
    head_up      = face_visible and obs.pitch < 阈值 # 头没低下 = 确实在看屏幕
    screen_active = pose_present and head_up and 标题命中黑名单
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
    Observation,
    Policy,
    SignalKind,
    evaluate,
)

BASE = datetime(2026, 9, 17, 21, 49, 0)
DISTRACT = "哔哩哔哩 (゜-゜)つロ 干杯~-bilibili"
FOCUS_TITLE = "考研数学 - Bilibili 课堂"

RAW = {
    "detection": {"screen_continuous_seconds": 10, "away_seconds": 90,
                  "resume_grace_seconds": 20},
    "reminder": {"max_level_high": 3, "escalate_after": [0, 10, 30],
                 "cooldown_seconds": 180, "max_silent_episode_seconds": 90},
    "whitelist": {"words": ["考研", "Bilibili 课堂"]},
    "blacklist": {"words": ["哔哩哔哩", "QQ"]},
}

# 你的校准结果: 低头阈值 17.4°, 看屏幕时 pitch ≈ 6.5°
CAL = Calibration(phone_yaw_deg=19.0, book_pitch_deg=17.4, blink_rate_per_min=15.0,
                  is_calibrated=True)


def obs(*, pitch: float | None, title: str, idle: float = 0.0,
        present: bool = True) -> Observation:
    """pitch=None 表示拿不到头姿(看不到脸)。"""
    return Observation(ts=0.0, at=BASE, pose_present=present, yaw=0.0, pitch=pitch,
                       yaw_std=10.0, pitch_std=10.0, blink_rate=15.0,
                       eye_closed_ratio=0.05, idle_seconds=idle, title=title)


class TestScreenEvidence(unittest.TestCase):
    def setUp(self):
        self.cfg = Config.from_dict(RAW)

    def screen(self, o: Observation) -> bool:
        return evaluate(o, self.cfg, CAL)[SignalKind.SCREEN].active

    # ---- 今晚的误报场景: 必须不判 ----
    def test_head_down_writing_with_entertainment_in_foreground(self):
        """低头写题 + 前台是 B 站 -> 不判(这就是今晚的误报)。"""
        self.assertFalse(self.screen(obs(pitch=30.0, title=DISTRACT, idle=300)))

    def test_face_not_visible_never_judges_window(self):
        """看不到脸 -> 不判。旧逻辑把 pitch=None 当"在看屏幕", 正是误报源头。"""
        self.assertFalse(self.screen(obs(pitch=None, title=DISTRACT, idle=300)))

    def test_face_not_visible_even_with_recent_input(self):
        """刚碰过键鼠也不能替我下结论: 低头写题时也可能碰一下鼠标。"""
        self.assertFalse(self.screen(obs(pitch=None, title=DISTRACT, idle=1)))

    def test_no_pose_present(self):
        self.assertFalse(self.screen(obs(pitch=0.0, title=DISTRACT, present=False)))

    # ---- 真正的分心必须抓到 ----
    def test_head_up_watching_entertainment(self):
        """头没低、看着屏幕、标题是 B 站 -> 判(包括看视频几分钟没碰键鼠的情况)。"""
        self.assertTrue(self.screen(obs(pitch=6.0, title=DISTRACT, idle=240)))

    def test_whitelist_still_wins(self):
        """看着屏幕但在看网课 -> 不判(白名单优先)。"""
        self.assertFalse(self.screen(obs(pitch=6.0, title=FOCUS_TITLE)))

    def test_evidence_mentions_looking_and_idle(self):
        ev = evaluate(obs(pitch=6.0, title=DISTRACT, idle=240),
                      self.cfg, CAL)[SignalKind.SCREEN]
        self.assertIn("你在看屏幕", ev.evidence)
        self.assertIn("没有键鼠操作", ev.evidence)

    def test_evidence_omits_idle_when_recently_active(self):
        ev = evaluate(obs(pitch=6.0, title=DISTRACT, idle=3),
                      self.cfg, CAL)[SignalKind.SCREEN]
        self.assertNotIn("没有键鼠操作", ev.evidence)


class TestNoFalseEpisodeFromWriting(unittest.TestCase):
    """端到端: 低头写题 8 分钟(脸常常看不到)不该产生任何分集/提醒。"""

    def test_long_writing_session_is_silent(self):
        cfg = Config.from_dict(RAW)
        sm = FocusStateMachine(cfg, CAL)
        policy = Policy(judging=True, allow_phone=False, quiet=True,
                        enabled=frozenset({"screen"}))
        actions = []
        for i in range(int(480 / 0.25)):
            ts = i * 0.25
            # 低头写题: 大部分时间脸看不到, 偶尔能瞄到但也是低头的
            pitch = None if (i % 5) else 28.0
            actions.extend(sm.update(Observation(
                ts=ts, at=BASE + timedelta(seconds=ts), pose_present=True,
                yaw=0.0, pitch=pitch, yaw_std=10.0, pitch_std=10.0, blink_rate=15.0,
                eye_closed_ratio=0.05, idle_seconds=300.0, title=DISTRACT),
                policy))
        self.assertEqual([a for a in actions if type(a).__name__ in
                          ("Nudge", "EpisodeStart", "EpisodeEnd")], [],
                         "低头写题被误判成了分心")


if __name__ == "__main__":
    unittest.main()
