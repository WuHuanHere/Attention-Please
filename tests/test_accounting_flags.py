"""守"该记的没记 / 记成别的"这一簇 —— 都是日报会误导人的口径 bug。

M3: `allow_phone` 时段(背单词)的"看手机"只记录不报警, 但它照样被算成分心、
    从有效专注里扣掉。根因: `episode_end` 不落 `record_only`, store 分不出来。
M4: 用户点了「判定错了」, 那段误报照样计进分心时长 —— 而 docstring 写着
    "立刻结束分集", 日报还并列印着"你标记了 1 次误判"。
M8: 弹窗通道自己报错时, 事件落库成了 `alert_dismissed`(等同于"用户点了我回来了"),
    事件流里完全看不出 UI 已经坏了。
M10: 清空截图时删不掉的文件被静默漏掉, 而托盘提示说"已清空" —— 这是隐私功能。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please import capture  # noqa: E402
from attention_please.config import Calibration, Config  # noqa: E402
from attention_please.report import build_report  # noqa: E402
from attention_please.runtime import Runtime  # noqa: E402
from attention_please.signals import Observation, Policy, SignalKind  # noqa: E402
from attention_please.tray import Tray  # noqa: E402

DAY = datetime.now().date().isoformat()
# ⚠️ **日期必须跟着"今天"走**, 不能写死。runtime 给 `wrong` / `ui_error` 打的时间戳是
# 真实的 `datetime.now()`, 而分集的时间戳来自测试伪造的 `Observation.at` —— 两边一旦
# 跨日(写死的 2026-09-20 撞上真实日期), `day_stats(DAY)` 就只看得见一半事件,
# 测试会莫名其妙地红(实测 2026-09-25: 5 个测试同时失败, 与代码改动无关)。
TODAY = datetime.now()

BASE = {
    "schedule": {"block": [{"name": "测试", "start": "00:00", "end": "24:00"}]},
    "privacy": {"save_captures": False},
    "report": {"daily_report": False},
    "blacklist": {"words": ["哔哩哔哩"]},
}


def make_runtime(root: pathlib.Path, **extra) -> Runtime:
    root.mkdir(parents=True, exist_ok=True)
    raw = dict(BASE)
    raw.update(extra)
    cfg = Config.from_dict(raw, path=root / "config.toml")
    rt = Runtime(cfg, Calibration())
    rt.notifier = types.SimpleNamespace(buzz=lambda *a, **k: None)
    rt.ui = types.SimpleNamespace(show=lambda r: None, ask_reason=lambda *a: None,
                                  drain=lambda: [])
    return rt


class TestAllowedPhoneIsNotDistraction(unittest.TestCase):
    """M3 —— 背单词时段用手机 App 是策略允许的, 不许当成摸鱼扣专注。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # 这个块的 allow_phone = true(和 config.toml 里的「英语单词」一样)
        self.rt = make_runtime(pathlib.Path(self.tmp.name) / "rt", **{
            "schedule": {"block": [{"name": "英语单词", "start": "00:00", "end": "24:00",
                                    "allow_phone": True}]}})

    def tearDown(self):
        self.rt.close()
        self.tmp.cleanup()

    def _run_phone_episode(self) -> None:
        pol = Policy(judging=True, allow_phone=True, block_name="英语单词",
                     enabled=frozenset({"phone"}))
        t0 = TODAY.replace(hour=15, minute=10, second=0, microsecond=0)


        def frame(ts: float, yaw: float) -> Observation:
            return Observation(ts=ts, at=t0 + timedelta(seconds=ts), pose_present=True,
                               yaw=yaw, pitch=0.0, title="Anki")

        for ts in range(0, 40, 2):
            self.rt.dispatch(self.rt.sm.update(frame(float(ts), 40.0), pol))
        for ts in range(42, 60, 2):
            self.rt.dispatch(self.rt.sm.update(frame(float(ts), 0.0), pol))

    def test_allowed_phone_is_not_counted_as_distraction(self):
        self._run_phone_episode()
        st = self.rt.store.day_stats(DAY, tick_hz=5.0)
        self.assertEqual(st.nudges, 0, "allow_phone 时段本来就不该报警")
        self.assertEqual(st.episodes, 0, "被允许的手机使用被当成了分心次数")
        self.assertEqual(st.distract_seconds, 0.0, "被允许的手机使用被扣进了分心时长")
        self.assertGreater(st.allowed_phone_seconds, 30,
                           "但也不能假装没发生 —— 要单列出来")

    def test_normal_phone_episode_is_still_distraction(self):
        """对照组: 普通时段(allow_phone=false)的看手机照旧算分心。"""
        rt = make_runtime(pathlib.Path(self.tmp.name) / "rt2")
        pol = Policy(judging=True, allow_phone=False, block_name="数学刷题",
                     enabled=frozenset({"phone"}))
        t0 = TODAY.replace(hour=10, minute=0, second=0, microsecond=0)

        def frame(ts: float, yaw: float) -> Observation:
            return Observation(ts=ts, at=t0 + timedelta(seconds=ts), pose_present=True,
                               yaw=yaw, pitch=0.0, title="Visual Studio Code")

        for ts in range(0, 40, 2):
            rt.dispatch(rt.sm.update(frame(float(ts), 40.0), pol))
        for ts in range(42, 60, 2):
            rt.dispatch(rt.sm.update(frame(float(ts), 0.0), pol))
        st = rt.store.day_stats(DAY, tick_hz=5.0)
        self.assertEqual(st.episodes, 1)
        self.assertGreater(st.distract_seconds, 30)
        self.assertEqual(st.allowed_phone_seconds, 0.0)
        rt.close()


class TestWrongFeedbackRemovesTheTime(unittest.TestCase):
    """M4 —— 点了「判定错了」的那一段不许再扣专注。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = make_runtime(pathlib.Path(self.tmp.name) / "rt")

    def tearDown(self):
        self.rt.close()
        self.tmp.cleanup()

    def _open_screen_episode(self) -> None:
        pol = Policy(judging=True, enabled=frozenset({"screen"}))
        t0 = TODAY.replace(hour=10, minute=0, second=0, microsecond=0)

        def frame(ts: float) -> Observation:
            return Observation(ts=ts, at=t0 + timedelta(seconds=ts), pose_present=True,
                               yaw=0.0, pitch=0.0, title="哔哩哔哩 - 视频")

        for ts in (0.0, 6.0, 12.0):
            self.rt.dispatch(self.rt.sm.update(frame(ts), pol))

    def test_wrong_feedback_excludes_the_episode(self):
        self._open_screen_episode()
        self.rt.ui = types.SimpleNamespace(drain=lambda: [("wrong", "screen")])
        self.rt._drain_ui(14.0)

        st = self.rt.store.day_stats(DAY)
        self.assertEqual(st.wrong_feedback, 1)
        self.assertEqual(st.episodes, 0, "被判定为误报的分集还算进了分心次数")
        self.assertEqual(st.distract_seconds, 0.0, "被判定为误报的分集还在扣专注")
        self.assertGreater(st.wrong_seconds, 0, "但那段时长要单列出来, 不能凭空消失")
        self.assertIsNone(self.rt.sm.tracks[SignalKind.SCREEN].episode_start,
                          "report_wrong 没有真的结束分集")

    def test_episode_after_the_flag_still_counts(self):
        """剔除只针对被标记的那一段, 之后的真分心照常计。"""
        self._open_screen_episode()
        self.rt.ui = types.SimpleNamespace(drain=lambda: [("wrong", "screen")])
        self.rt._drain_ui(14.0)

        pol = Policy(judging=True, enabled=frozenset({"screen"}))
        t0 = TODAY.replace(hour=11, minute=0, second=0, microsecond=0)   # 换一小时, 避开静默窗

        for ts in (0.0, 6.0, 12.0):
            self.rt.dispatch(self.rt.sm.update(
                Observation(ts=ts, at=t0 + timedelta(seconds=ts), pose_present=True,
                            yaw=0.0, pitch=0.0, title="哔哩哔哩 - 视频"), pol))
        st = self.rt.store.day_stats(DAY)
        self.assertEqual(st.wrong_seconds > 0, True)
        self.assertEqual(st.wrong_feedback, 1)


class TestUiErrorIsNotADismissal(unittest.TestCase):
    """M8 —— 弹窗通道坏了必须看得出来, 不能被记成"用户点了我回来了"。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = make_runtime(pathlib.Path(self.tmp.name) / "rt")

    def tearDown(self):
        self.rt.close()
        self.tmp.cleanup()

    def test_ui_error_gets_its_own_kind(self):
        self.rt.ui = types.SimpleNamespace(
            drain=lambda: [("ui_error", "tk-callback: TclError: bad screen distance")])
        self.rt._drain_ui(0.0)
        rows = list(self.rt.store.conn.execute(
            "SELECT kind, COALESCE(detail,'') FROM event ORDER BY id"))
        self.assertEqual([r[0] for r in rows], ["ui_error"],
                         "UI 报错被记成了别的 kind(以前是 alert_dismissed)")
        self.assertIn("TclError", rows[0][1])
        self.assertEqual(self.rt.store.day_stats(DAY).ui_errors, 1)

    def test_real_dismissal_still_logs_as_dismissed(self):
        self.rt.ui = types.SimpleNamespace(drain=lambda: [("dismissed", "screen")])
        self.rt._drain_ui(0.0)
        rows = list(self.rt.store.conn.execute("SELECT kind FROM event"))
        self.assertEqual([r[0] for r in rows], ["alert_dismissed"])

    def test_unknown_event_does_not_masquerade_as_dismissal(self):
        self.rt.ui = types.SimpleNamespace(drain=lambda: [("some_new_thing", "x")])
        self.rt._drain_ui(0.0)
        rows = list(self.rt.store.conn.execute("SELECT kind FROM event"))
        self.assertEqual([r[0] for r in rows], ["some_new_thing"])

    def test_report_surfaces_ui_errors(self):
        self.rt.store.event("ui_error", at=datetime.now(), detail="TclError")
        text = build_report(self.rt.cfg, self.rt.store, datetime.now().date().isoformat())
        self.assertIn("提醒窗口报错", text)


class TestClearCapturesDoesNotLie(unittest.TestCase):
    """M10 —— 隐私功能不许报假成功。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_locked_file_is_reported_as_failed(self):
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            (self.dir / name).write_bytes(b"x")
        held = open(self.dir / "c.jpg", "rb")       # 模拟被看图软件占着
        try:
            removed, failed = capture.clear_all(self.dir)
            self.assertEqual(removed, 2)
            self.assertEqual(failed, 1, "删不掉的文件被静默漏掉了")
            self.assertEqual(len(list(self.dir.glob("*.jpg"))), 1)
        finally:
            held.close()

    def test_tray_says_so_when_something_survived(self):
        rt = make_runtime(pathlib.Path(self.tmp.name) / "rt")
        tray = Tray(rt, rt.cfg.report_dir)
        notes: list[str] = []
        tray._notify = lambda m, title="attention_please": notes.append(m)
        rt.cfg.capture_dir.mkdir(parents=True, exist_ok=True)
        (rt.cfg.capture_dir / "a.jpg").write_bytes(b"x")
        held = open(rt.cfg.capture_dir / "b.jpg", "wb")
        try:
            tray._clear_captures()
            self.assertIn("删不掉", notes[0])
            self.assertNotIn("已清空", notes[0])
        finally:
            held.close()
        rt.close()

    def test_all_clear_still_says_cleared(self):
        rt = make_runtime(pathlib.Path(self.tmp.name) / "rt3")
        tray = Tray(rt, rt.cfg.report_dir)
        notes: list[str] = []
        tray._notify = lambda m, title="attention_please": notes.append(m)
        rt.cfg.capture_dir.mkdir(parents=True, exist_ok=True)
        (rt.cfg.capture_dir / "a.jpg").write_bytes(b"x")
        tray._clear_captures()
        self.assertIn("已清空 1 张", notes[0])
        rt.close()


if __name__ == "__main__":
    unittest.main()
