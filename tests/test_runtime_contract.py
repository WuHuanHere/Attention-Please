"""运行时"能消费状态机所有动作"的契约测试。

这条守的是一个真实踩到的 bug: dispatch() 里读了 `EpisodeEnd.evidence`,
而动作对象上根本没有这个字段 —— 分心结束的那一刻会抛 AttributeError。
这类"两个模块对同一个数据类的假设不一致"的错, 单测状态机是发现不了的,
所以这里直接拿真实 Runtime 的 dispatch 跑一遍所有动作类型。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import types
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.config import Calibration, Config  # noqa: E402
from attention_please.runtime import Runtime  # noqa: E402
from attention_please.signals import (  # noqa: E402
    AwayChange,
    EpisodeEnd,
    EpisodeStart,
    Nudge,
    SignalKind,
)

RAW = {
    "reminder": {"max_level_high": 3, "cooldown_seconds": 180,
                 "wrong_feedback_mute_seconds": 900},
    "schedule": {"block": [{"name": "测试", "start": "00:00", "end": "24:00"}]},
}


class TestDispatchContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config.from_dict(RAW, path=pathlib.Path(self.tmp.name) / "config.toml")
        self.rt = Runtime(cfg, Calibration())
        # 测试里不发声、不弹窗(否则会真响、真开窗)
        self.rt.notifier = types.SimpleNamespace(buzz=lambda *a, **k: None)
        self.rt.ui = types.SimpleNamespace(show=lambda req: None)

    def tearDown(self):
        self.rt.close()
        self.tmp.cleanup()

    def test_every_action_type_is_dispatchable(self):
        now = datetime.now()
        self.rt.dispatch([
            EpisodeStart(SignalKind.SCREEN, "窗口标题命中黑名单「哔哩哔哩」",
                         now, 0.0, False),
            Nudge(SignalKind.SCREEN, 1, "窗口标题命中黑名单「哔哩哔哩」",
                  False, now, 0.0, "测试"),
            Nudge(SignalKind.SCREEN, 2, "窗口标题命中黑名单「哔哩哔哩」",
                  False, now, 10.0, "测试"),
            EpisodeEnd(SignalKind.SCREEN, now, 20.0, 30.0, 2, False,
                       "窗口标题命中黑名单「哔哩哔哩」"),
            AwayChange(True, now, 30.0, 0.0),
            AwayChange(False, now, 130.0, 100.0),
        ])
        st = self.rt.store.day_stats(now.date().isoformat())
        self.assertEqual(st.nudges, 2)
        self.assertEqual(st.episodes, 1)
        self.assertAlmostEqual(st.distract_seconds, 30.0)
        self.assertAlmostEqual(st.away_seconds, 100.0)

    def test_nudge_with_evidence_popup_path(self):
        """level 2 会走弹窗分支(show_evidence=True), 也要能吃下 AlertRequest。"""
        now = datetime.now()
        reqs = []
        self.rt.ui = types.SimpleNamespace(show=lambda req: reqs.append(req))
        self.rt.dispatch([Nudge(SignalKind.SCREEN, 2, "证据文本", False, now, 12.0, "块")])
        self.assertEqual(len(reqs), 1)
        self.assertIn("切到了娱乐窗口", reqs[0].title)
        self.assertIn("证据文本", reqs[0].body)

    def test_level1_with_sound_does_not_popup(self):
        now = datetime.now()
        reqs = []
        self.rt.ui = types.SimpleNamespace(show=lambda req: reqs.append(req))
        self.rt.dispatch([Nudge(SignalKind.SCREEN, 1, "证据", True, now, 12.0, "块")])
        self.assertEqual(reqs, [], "L1 有声时只轻响一声, 不该弹窗打断")

    def test_notifier_failure_does_not_lose_the_record(self):
        """**这条守的是 2026-09-17 的事故**: 提醒通道一抛异常, 记录就没了。

        现在的顺序是"先记账、再提醒", 且提醒整段包在 try 里 ——
        哪怕声卡/Tk 全炸, 也必须留下 nudge 事件和文件日志。
        """
        now = datetime.now()

        def boom(*a, **k):
            raise RuntimeError("声卡炸了")

        self.rt.notifier = types.SimpleNamespace(buzz=boom)
        self.rt.ui = types.SimpleNamespace(show=boom)
        self.rt.dispatch([Nudge(SignalKind.SCREEN, 2, "证据", True, now, 12.0, "块")])
        st = self.rt.store.day_stats(now.date().isoformat())
        self.assertEqual(st.nudges, 1, "提醒失败绝不能把这条记录吞掉")
        log_text = self.rt.log.path.read_text(encoding="utf-8")
        self.assertIn("notifier.buzz 出错", log_text)
        self.assertIn("ui.show 出错", log_text)

    def test_dispatch_logs_actions_to_file(self):
        now = datetime.now()
        self.rt.dispatch([EpisodeEnd(SignalKind.SCREEN, now, 20.0, 30.0, 2, False, "证据")])
        self.assertIn("EpisodeEnd", self.rt.log.path.read_text(encoding="utf-8"))

    def test_record_only_episode_saves_no_capture(self):
        """**只记录的分集不许截图**(2026-09-18 真机验证时抓到)。

        POSE 分集只是"看不到脸时 Pose 觉得你在低头", 不是分心; 背单词时段的 phone
        也走只记录。给它们存"屏幕+摄像头"拼接图既是隐私问题(低头写题会被拍一堆),
        也会把 captures/ 塞满, 还会让人误以为"它又报警了"。
        实测: 一段 33 秒的 pose 分集真的存了一张图。
        """
        now = datetime.now()
        with mock.patch("attention_please.capture.save_composite") as save:
            self.rt.dispatch([EpisodeStart(SignalKind.POSE, "Pose 弱证据(低头/没动)",
                                           now, 0.0, True)])
            save.assert_not_called()
        st = self.rt.store.day_stats(now.date().isoformat())
        self.assertEqual(st.episodes, 0, "只记录的分集不算分心")
        self.assertEqual(st.pose_weak_seconds, 0.0)   # 只有 start, 没有 end

    def test_normal_episode_still_saves_a_capture(self):
        """对照组: 真正的分心分集必须照旧存证据图。"""
        now = datetime.now()
        with mock.patch("attention_please.capture.save_composite",
                        return_value=pathlib.Path("x.jpg")) as save:
            self.rt.dispatch([EpisodeStart(SignalKind.SCREEN, "证据", now, 0.0, False)])
            save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
