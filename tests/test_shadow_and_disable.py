"""守两个"看得见的盲区":

M5 白名单里的**通用名词**会让黑名单站点"隐身": 判定顺序是白名单优先(刻意的,
   为了让「Bilibili 课堂」不被 bilibili 误杀), 但「数学」「英语」「考研」这类词
   会把「知乎」「小红书」「微博」「哔哩哔哩」整个盖住, 而且**一条日志都不留**。
   修法**不改判定**(改优先级是产品决策, 会直接影响误报率), 只让它可见:
   标题变化时记一条 title_shadowed, 日报的可信度段里报次数。

M6 热重载把某个信号关掉时, 它正在开着的分集不会被收口 —— 时长被拉到时间表结束。
   实测: 真实只观测到 ~12 秒的分心, 被记成 700 秒。
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
from attention_please.report import build_report  # noqa: E402
from attention_please.runtime import Runtime  # noqa: E402
from attention_please.signals import (  # noqa: E402
    EpisodeEnd,
    FocusStateMachine,
    Observation,
    Policy,
    SignalKind,
)
from attention_please.wordlist import TitleVerdict, classify  # noqa: E402

DAY = "2026-09-20"
# 和 config.toml 同款: 白名单里有通用名词, 黑名单里是站点名
WHITE = ["数学", "英语", "电路", "政治", "考研", "真题", "单词", "笔记", "网课",
         "Visual Studio Code", "Bilibili 课堂"]
BLACK = ["哔哩哔哩", "bilibili", "知乎", "微博", "小红书", "微信", "QQ"]

RAW = {
    "schedule": {"block": [{"name": "测试", "start": "00:00", "end": "24:00"}]},
    "privacy": {"save_captures": False},
    "report": {"daily_report": False},
    "whitelist": {"words": WHITE},
    "blacklist": {"words": BLACK},
}


def make_runtime(root: pathlib.Path) -> Runtime:
    root.mkdir(parents=True, exist_ok=True)
    cfg = Config.from_dict(RAW, path=root / "config.toml")
    rt = Runtime(cfg, Calibration())
    rt.notifier = types.SimpleNamespace(buzz=lambda *a, **k: None)
    rt.ui = types.SimpleNamespace(show=lambda r: None, ask_reason=lambda *a: None,
                                  drain=lambda: [])
    return rt


class TestWhitelistShadowingIsVisible(unittest.TestCase):
    def test_classify_reports_the_shadowed_blacklist_word(self):
        m = classify("考研数学基础班 - 知乎", WHITE, BLACK)
        self.assertIs(m.verdict, TitleVerdict.FOCUS)      # 判定本身没变
        self.assertEqual(m.matched, "数学")
        self.assertEqual(m.shadowed, "知乎", "被盖住的黑名单词必须报出来")

    def test_clean_focus_title_has_no_shadow(self):
        self.assertIsNone(classify("Typora - 笔记.md", WHITE, BLACK).shadowed)

    def test_pure_distraction_has_no_shadow(self):
        m = classify("哔哩哔哩 (゜-゜)つロ 干杯~-bilibili", WHITE, BLACK)
        self.assertIs(m.verdict, TitleVerdict.DISTRACT)
        self.assertIsNone(m.shadowed)

    def test_runtime_records_it_once_per_title(self):
        tmp = tempfile.TemporaryDirectory()
        rt = make_runtime(pathlib.Path(tmp.name) / "rt")
        rt._note_shadowed_title("考研数学基础班 - 知乎")
        rt._note_shadowed_title("考研数学基础班 - 知乎")     # 同一个标题不该重复记
        rt._note_shadowed_title("英语单词记忆法 - 小红书")
        st = rt.store.day_stats(DAY)
        self.assertEqual(st.shadowed_titles, 2, "白名单盖住黑名单这件事没有被记下来")
        rt.close()
        tmp.cleanup()

    def test_report_discloses_it_neutrally(self):
        """**用户 2026-09-20 已确认"白名单优先"是设计如此**, 所以日报里这句必须是
        中性披露, 不能写成"警告/可能漏判/建议删词"。"""
        tmp = tempfile.TemporaryDirectory()
        rt = make_runtime(pathlib.Path(tmp.name) / "rt")
        rt._note_shadowed_title("考研数学真题解析 - 微博")
        text = build_report(rt.cfg, rt.store, DAY)
        self.assertIn("同时命中白/黑名单", text)
        self.assertIn("设计如此", text)
        self.assertNotIn("可能漏判", text)
        self.assertNotIn("⚠️ 有 1 次窗口标题", text)
        rt.close()
        tmp.cleanup()

    def test_whitelist_beats_blacklist_is_locked_in(self):
        """把"白名单优先"这条已确认的决定钉死 —— 别哪天被"优化"成站点优先。"""
        for title in ("考研数学基础班 - 知乎", "【考研政治】徐涛强化班 - 哔哩哔哩",
                      "电路真题讲解 - 微博"):
            m = classify(title, WHITE, BLACK)
            self.assertIs(m.verdict, TitleVerdict.FOCUS,
                          f"{title} 被判成 {m.verdict} —— 用户明确要求白名单优先")


class TestDisablingASignalClosesItsEpisode(unittest.TestCase):
    """M6 —— 关掉信号不能把开着的分集拖到时间表结束。"""

    def _sm(self) -> FocusStateMachine:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = Config.from_dict(RAW, path=pathlib.Path(tmp.name) / "c.toml")
        return FocusStateMachine(cfg, Calibration())

    def _frame(self, ts: float) -> Observation:
        return Observation(ts=ts, at=datetime(2026, 5, 1, 10, 0, 0), pose_present=True,
                           yaw=0.0, pitch=0.0, title="哔哩哔哩 - 视频")

    def test_disabling_closes_the_episode_at_that_moment(self):
        sm = self._sm()
        on = Policy(judging=True, enabled=frozenset({"screen"}))
        off = Policy(judging=True, enabled=frozenset({"phone"}))   # 热重载后 screen 被关掉

        for ts in (0.0, 6.0, 12.0):
            sm.update(self._frame(ts), on)
        self.assertIsNotNone(sm.tracks[SignalKind.SCREEN].episode_start)

        # 热重载生效后的第一帧就该收口
        acts = sm.update(self._frame(14.0), off)
        ends = [a for a in acts if isinstance(a, EpisodeEnd)]
        self.assertEqual(len(ends), 1, "关掉信号时没有收口 —— 时长会被拉到块结束")
        self.assertAlmostEqual(ends[0].duration, 14.0, delta=0.1)
        self.assertIsNone(sm.tracks[SignalKind.SCREEN].episode_start)

    def test_no_further_duration_accumulates(self):
        sm = self._sm()
        on = Policy(judging=True, enabled=frozenset({"screen"}))
        off = Policy(judging=True, enabled=frozenset({"phone"}))
        for ts in (0.0, 6.0, 12.0):
            sm.update(self._frame(ts), on)
        sm.update(self._frame(14.0), off)
        later = []
        for ts in range(16, 700, 2):
            later += sm.update(self._frame(float(ts)), off)
        self.assertEqual([a for a in later if isinstance(a, EpisodeEnd)], [],
                         "关掉之后不该再冒出第二个 episode_end")
        self.assertIsNone(sm.tracks[SignalKind.SCREEN].episode_start)

    def test_disabled_signal_without_episode_emits_nothing(self):
        sm = self._sm()
        off = Policy(judging=True, enabled=frozenset({"phone"}))
        self.assertEqual(sm.update(self._frame(0.0), off), [])


if __name__ == "__main__":
    unittest.main()
