"""回归测试: 把摄像头**让给别的程序**(会议/通话/直播软件)。

## 为什么需要这个功能(2026-09-18 用户报的 bug)

现象: 打开别的需要摄像头的软件时, 本程序不让出摄像头。

实测根因(见 `probe_out/` 里那次共享实验): 我们用 DirectShow 占着摄像头时,
- 另一个 **DSHOW** 句柄能开也能读 -> 设备被共享;
- 另一个 **MSMF** 句柄能开但读失败, 并且会把我们的流踢掉;
- 但**当别人只是来抢一下又失败时, 我们的 `cap.read()` 照样成功** -> 我们毫无察觉。

所以代码里唯一的让出路径(`cap.read()` 失败)根本不会触发 —— 当天日志里
`camera_read_fail`/`camera_busy` **一条都没有**。Windows 又不提供"别人想要摄像头"的
通知, 于是只能主动判断: 前台窗口命中关键词就让。

## 这个文件守的三件事

1. **命中就让**: 前台是会议/通话/直播软件时, 策略立刻变成"不判定 + 让出摄像头",
   而且摄像头是**立刻**释放(不是等 120 秒的常规释放)。
2. **不命中不让**: 普通学习窗口绝不能让出 —— 否则等于给自己开免监控后门。
3. **关键词表里不许出现"微信"/"QQ"**: 它们在黑名单里, 是分心信号要抓的目标;
   前台一挂微信就让出摄像头 = 正好在它该抓你的时候失效。这条单独测。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.config import Calibration, Config, DEFAULT_YIELD_APPS  # noqa: E402
from attention_please.runtime import Runtime  # noqa: E402

# 时间表: 全天 focus -> 让出的效果不会被"时间表外"掩盖
RAW = {
    "schedule": {"block": [{"name": "测试", "start": "00:00", "end": "24:00"}]},
    "camera_yield": {"enabled": True, "apps": ["腾讯会议", "Zoom", "视频通话"],
                     "manual_minutes": 30},
}


def make_rt(tmp: str, **yield_overrides) -> Runtime:
    raw = {k: (dict(v) if isinstance(v, dict) else v) for k, v in RAW.items()}
    if yield_overrides:
        raw["camera_yield"] = {**raw["camera_yield"], **yield_overrides}
    cfg = Config.from_dict(raw, path=pathlib.Path(tmp) / "config.toml")
    rt = Runtime(cfg, Calibration())
    rt.notifier = types.SimpleNamespace(buzz=lambda *a, **k: None)
    rt.ui = types.SimpleNamespace(show=lambda req: None)
    return rt


class TestYieldPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = make_rt(self.tmp.name)
        self.now = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)

    def tearDown(self):
        self.rt.close()
        self.tmp.cleanup()

    def test_matching_foreground_app_yields(self):
        p = self.rt._policy(self.now, title="腾讯会议 - 待入会")
        self.assertFalse(p.judging)
        self.assertTrue(p.camera_yield, "命中让出关键词时必须标记 camera_yield")
        self.assertIn("腾讯会议", p.block_name)

    def test_zoom_matches_case_insensitively(self):
        self.assertTrue(self.rt._policy(self.now, title="zoom meeting").camera_yield)

    def test_normal_study_window_does_not_yield(self):
        """最重要的一条: 正常学习窗口绝不能让出(否则就是免监控后门)。"""
        for title in ("Visual Studio Code", "哔哩哔哩 - Chrome", "新标签页", ""):
            p = self.rt._policy(self.now, title=title)
            self.assertFalse(p.camera_yield, f"「{title}」不该让出摄像头")
            self.assertTrue(p.judging, f"「{title}」应该照常判定")

    def test_pause_takes_precedence_over_yield(self):
        self.rt.pause("测试暂停", self.now)
        p = self.rt._policy(self.now, title="腾讯会议")
        self.assertFalse(p.judging)
        self.assertFalse(p.camera_yield, "暂停是用户明确动作, 不该被记成'让出'")

    def test_manual_yield_overrides_everything(self):
        self.rt.yield_camera(30)
        p = self.rt._policy(self.now, title="Visual Studio Code")
        self.assertFalse(p.judging)
        self.assertTrue(p.camera_yield)
        self.assertIn("手动", p.block_name)

    def test_manual_yield_expires(self):
        self.rt.yield_camera(30)
        later = datetime.now() + timedelta(minutes=31)
        p = self.rt._policy(later, title="Visual Studio Code")
        self.assertTrue(p.judging, "手动让出到期后必须自己恢复判定")

    def test_reclaim_cancels_manual_yield(self):
        self.rt.yield_camera(30)
        self.rt.reclaim_camera()
        self.assertTrue(self.rt._policy(self.now, title="Visual Studio Code").judging)

    def test_disabled_yield_never_triggers(self):
        rt = make_rt(self.tmp.name, enabled=False)
        try:
            p = rt._policy(self.now, title="腾讯会议")
            self.assertFalse(p.camera_yield)
            self.assertTrue(p.judging)
        finally:
            rt.close()

    def test_empty_app_list_never_triggers(self):
        rt = make_rt(self.tmp.name, apps=[])
        try:
            self.assertTrue(rt._policy(self.now, title="腾讯会议").judging)
        finally:
            rt.close()

    def test_outside_schedule_is_idle_not_yield(self):
        """时间表外本来就待机 —— 不许记成"让出摄像头"。

        否则晚上挂着会议室, 日报会把整个晚上算成"主动把摄像头让给别的程序",
        那是骗人的(而且那段时间本来也不监控)。
        """
        rt = make_rt(self.tmp.name)
        try:
            outside = datetime.now().replace(hour=3, minute=0, second=0, microsecond=0)
            # RAW 的时间表是全天 focus, 这里临时换一张"只有上午"的表
            cfg = Config.from_dict(
                {"schedule": {"block": [{"name": "上午", "start": "08:00", "end": "11:00"}]},
                 "camera_yield": {"enabled": True, "apps": ["腾讯会议"]}},
                path=pathlib.Path(self.tmp.name) / "config2.toml")
            rt2 = Runtime(cfg, Calibration())
            try:
                p = rt2._policy(outside, title="腾讯会议")
                self.assertFalse(p.judging)
                self.assertFalse(p.camera_yield, "时间表外不该记成让出")
                self.assertFalse(p.block_name)
            finally:
                rt2.close()
        finally:
            rt.close()


class TestYieldCausesImmediateRelease(unittest.TestCase):
    """让出必须**立刻**释放摄像头 —— 人家正等着用, 不能等 120 秒(§0 的常规释放)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = make_rt(self.tmp.name)

    def tearDown(self):
        self.rt.close()
        self.tmp.cleanup()

    def test_yield_releases_even_when_just_judged(self):
        from attention_please.signals import Policy
        yield_pol = Policy(judging=False, camera_yield=True)
        self.rt.idle_since = time.monotonic()      # 上一帧还在判定
        self.assertTrue(self.rt._should_release_now(yield_pol, time.monotonic()))

    def test_regular_idle_still_waits_120s(self):
        """对照: 普通的"时间表外待机"仍然要等 120 秒(免得反复开开关关摄像头)。"""
        from attention_please.signals import Policy
        idle_pol = Policy(judging=False, camera_yield=False)
        now = time.monotonic()
        self.rt.idle_since = now
        self.assertFalse(self.rt._should_release_now(idle_pol, now))
        self.rt.idle_since = now - 121.0
        self.assertTrue(self.rt._should_release_now(idle_pol, now))


class TestYieldKeywordSafety(unittest.TestCase):
    def test_shipped_keywords_do_not_contain_bare_wechat_or_qq(self):
        """**别给自己开免监控后门。**

        微信/QQ 在 blacklist 里, 是 screen 信号要抓的目标。把它们放进让出关键词表,
        就等于"前台挂着微信 -> 摄像头让出 -> 判定停止 -> 再也不会提醒你在微信上"
        —— 而且正好在它该抓你的时候失效。要匹配视频通话请用「视频通话」这类词。
        """
        for word in DEFAULT_YIELD_APPS:
            self.assertNotEqual(word, "微信")
            self.assertNotEqual(word, "QQ")
            self.assertNotIn("微信", word)
        # 也不许只写 "qq"(大小写无关地会命中 QQ 的窗口)
        self.assertFalse(any(w.strip().lower() == "qq" for w in DEFAULT_YIELD_APPS))

    def test_shipped_config_keywords_are_safe(self):
        cfg = Config.load()
        for word in cfg.camera_yield.apps:
            self.assertNotIn("微信", word, f"config.toml 的让出关键词「{word}」会开免监控后门")
            self.assertNotEqual(word.strip().lower(), "qq")
        self.assertIn("腾讯会议", cfg.camera_yield.apps)
        self.assertIn("视频通话", cfg.camera_yield.apps)


class TestYieldAccounting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = make_rt(self.tmp.name)

    def tearDown(self):
        self.rt.close()
        self.tmp.cleanup()

    def test_yield_events_are_logged_once_per_episode(self):
        from attention_please.signals import Policy
        yield_pol = Policy(judging=False, camera_yield=True, block_name="已让出摄像头")
        ok_pol = Policy(judging=True)

        self.rt._track_yield(yield_pol, datetime.now())      # 开始
        self.rt._track_yield(yield_pol, datetime.now())      # 仍在让出, 不该重复记
        end = datetime.now() + timedelta(minutes=5)
        self.rt._track_yield(ok_pol, end)                    # 结束

        st = self.rt.store.day_stats(datetime.now().date().isoformat())
        self.assertAlmostEqual(st.yield_seconds, 300.0, delta=2.0)

    def test_report_mentions_yield(self):
        from attention_please.report import build_report
        day = datetime.now().date().isoformat()
        self.rt.store.event("camera_yield_end", at=datetime.now(), duration=600.0)
        text = build_report(self.rt.cfg, self.rt.store, day, planned_seconds=3600.0)
        self.assertIn("让给别的程序", text)
        self.assertIn("不是你没专注", text)


if __name__ == "__main__":
    unittest.main()
