"""回归测试: 控制台编码(GBK)不能让"记账或提醒"被吞掉。

## 守的是 2026-09-18 抓到的真 bug

中文 Windows 控制台默认 **GBK**, 而提醒文案里有 emoji(🔔 ⏹ 💤 👋)。
`print` 会抛 `UnicodeEncodeError`, 而 `Runtime.dispatch` 的顺序是:

    记账(_log) → print(...) → 响铃(buzz) → 弹窗(ui.show)

print 一抛, **响铃和弹窗被整段跳过**: 库里记了一笔"提醒", 但你既没听见也没看见。
(同一原因还让单测在没设 PYTHONIOENCODING=utf-8 时红 12 个, 看起来像"偶发"。)

修法:`logbook.safe_print()` —— 编不出来的字符降级成 `?`, 绝不抛异常;
runtime 的所有输出走 `self._say()`。
"""
from __future__ import annotations

import io
import pathlib
import sys
import tempfile
import types
import unittest
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.config import Calibration, Config  # noqa: E402
from attention_please.logbook import safe_print  # noqa: E402
from attention_please.runtime import Runtime  # noqa: E402
from attention_please.signals import Nudge, SignalKind  # noqa: E402

RAW = {
    "schedule": {"block": [{"name": "上午", "start": "08:30", "end": "10:30"}]},
    "report": {"daily_report": False},
    "privacy": {"save_captures": False},
}

EMOJI_LINE = "🔔 提醒 L1(一声) 切到了娱乐窗口: 证据"


class TestSafePrint(unittest.TestCase):
    def setUp(self):
        self._real = sys.stdout
        self.addCleanup(self._restore)

    def _restore(self):
        sys.stdout = self._real

    def test_emoji_does_not_raise_on_gbk_console(self):
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
        safe_print(EMOJI_LINE)          # 不抛即通过
        self.assertTrue(True)

    def test_emoji_still_prints_something(self):
        buf = io.BytesIO()
        sys.stdout = io.TextIOWrapper(buf, encoding="gbk", errors="strict")
        safe_print(EMOJI_LINE)
        sys.stdout.flush()
        text = buf.getvalue().decode("gbk", "replace")
        self.assertIn("提醒 L1", text, "降级后连中文都没了")
        self.assertIn("?", text, "emoji 应该被降级成 ?")

    def test_normal_ascii_path_untouched(self):
        buf = io.BytesIO()
        sys.stdout = io.TextIOWrapper(buf, encoding="gbk", errors="strict")
        safe_print("plain ascii")
        sys.stdout.flush()
        # TextIOWrapper 会把 \n 翻成 \r\n, 所以只比内容不比行尾
        self.assertEqual(buf.getvalue().decode("gbk").strip(), "plain ascii")

    def test_closed_stdout_does_not_raise(self):
        class Boom:
            encoding = "gbk"

            def write(self, *_a, **_k):
                raise ValueError("stdout 被关了")

            def flush(self):
                raise ValueError("stdout 被关了")

        sys.stdout = Boom()             # type: ignore[assignment]
        safe_print(EMOJI_LINE)          # 不抛即通过
        self.assertTrue(True)


class TestDispatchSurvivesGbkConsole(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config.from_dict(RAW, path=pathlib.Path(self.tmp.name) / "config.toml")
        self.rt = Runtime(cfg, Calibration())
        self._real = sys.stdout
        self.addCleanup(self._restore)
        # 模拟中文 Windows 控制台: 只能编 GBK, 且严格模式
        self.gbk = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
        sys.stdout = self.gbk

    def _restore(self):
        sys.stdout = self._real
        self.rt.close()
        self.tmp.cleanup()

    def test_nudge_is_recorded_and_delivered(self):
        calls: list[tuple] = []
        self.rt.notifier = types.SimpleNamespace(
            buzz=lambda *a, **k: calls.append(("buzz", a, k)))
        self.rt.ui = types.SimpleNamespace(show=lambda req: calls.append(("show", req)))
        now = datetime.now()

        self.rt.dispatch([Nudge(SignalKind.SCREEN, 1, "证据", True, now, 0.0, "上午")])

        st = self.rt.store.day_stats(now.date().isoformat())
        self.assertEqual(st.nudges, 1, "GBK 控制台下记账丢了")
        self.assertTrue(any(c[0] == "buzz" for c in calls),
                        "GBK 控制台下响铃被 print 的异常吞掉了(这就是当年的 bug)")

    def test_episode_end_does_not_raise(self):
        from attention_please.signals import EpisodeEnd

        now = datetime.now()
        self.rt.dispatch([EpisodeEnd(SignalKind.SCREEN, now, 20.0, 30.0, 2, False, "证据")])
        st = self.rt.store.day_stats(now.date().isoformat())
        self.assertEqual(st.episodes, 1)

    def test_away_change_does_not_raise(self):
        from attention_please.signals import AwayChange

        now = datetime.now()
        self.rt.dispatch([AwayChange(True, now, 0.0, 0.0),
                          AwayChange(False, now, 100.0, 100.0)])
        st = self.rt.store.day_stats(now.date().isoformat())
        self.assertAlmostEqual(st.away_seconds, 100.0)


if __name__ == "__main__":
    unittest.main()
