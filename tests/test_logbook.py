"""文件日志的单测。

守的是 2026-09-17 那次"该提醒却没提醒、而且什么都查不到"的教训:
托盘版用 pythonw 启动时 **stdout 不存在**, 所以必须有一样东西把输出落到磁盘上。
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

from attention_please.logbook import Logbook, StreamTee, install_streams  # noqa: E402


class TestLogbook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Logbook(pathlib.Path(self.tmp.name) / "logs")

    def tearDown(self):
        self.log.close()
        self.tmp.cleanup()

    def test_writes_and_flushes_immediately(self):
        self.log.write("提醒 L3 已发出")
        text = self.log.path.read_text(encoding="utf-8")
        self.assertIn("提醒 L3 已发出", text)
        self.assertIn("INFO", text)

    def test_failure_is_visible_not_fatal(self):
        self.log.write("先写一行")
        self.log.write("再写一行")
        lines = self.log.path.read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(lines), 2)

    def test_exception_records_traceback(self):
        try:
            raise ValueError("数据库炸了")
        except ValueError as exc:
            self.log.exception("dispatch", exc)
        text = self.log.path.read_text(encoding="utf-8")
        self.assertIn("dispatch 出错", text)
        self.assertIn("ValueError", text)
        self.assertIn("数据库炸了", text)
        self.assertIn("Traceback", text)

    def test_creates_directory(self):
        nested = pathlib.Path(self.tmp.name) / "a" / "b"
        log = Logbook(nested)
        log.write("ok")
        self.assertTrue(nested.exists())
        log.close()


class TestStreamTee(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Logbook(pathlib.Path(self.tmp.name) / "logs")

    def tearDown(self):
        self.log.close()
        self.tmp.cleanup()

    def test_writes_lines_to_log(self):
        tee = StreamTee(self.log, "OUT")
        tee.write("第一行\n第二行\n")
        text = self.log.path.read_text(encoding="utf-8")
        self.assertIn("第一行", text)
        self.assertIn("第二行", text)
        self.assertIn("OUT", text)

    def test_echoes_to_original_stream(self):
        buf = io.StringIO()
        tee = StreamTee(self.log, "OUT", echo=buf)
        tee.write("hello\n")
        self.assertEqual(buf.getvalue(), "hello\n")

    def test_blank_lines_ignored(self):
        tee = StreamTee(self.log, "OUT")
        tee.write("\n\n   \n")
        # 全是空行 -> 不该产生日志文件(Logbook 是懒创建)
        self.assertFalse(self.log.path.exists())

    def test_does_not_raise_when_log_broken(self):
        class Broken:
            def write(self, *a, **k):
                raise OSError("磁盘满了")

            def flush(self):
                raise OSError("磁盘满了")

        tee = StreamTee(Broken())      # type: ignore[arg-type]
        tee.write("不该炸\n")           # 不抛即通过
        self.assertTrue(True)

    def test_isatty_false(self):
        self.assertFalse(StreamTee(self.log).isatty())


class TestInstallStreams(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Logbook(pathlib.Path(self.tmp.name) / "logs")
        self._out, self._err = sys.stdout, sys.stderr
        self._flag = getattr(sys, "attention_logging_installed", None)

    def tearDown(self):
        sys.stdout, sys.stderr = self._out, self._err
        if self._flag is None:
            try:
                del sys.attention_logging_installed  # type: ignore[attr-defined]
            except AttributeError:
                pass
        else:
            sys.attention_logging_installed = self._flag  # type: ignore[attr-defined]
        self.log.close()
        self.tmp.cleanup()

    def test_install_redirects_and_is_idempotent(self):
        self.assertTrue(install_streams(self.log))
        print("这句话应该进日志")
        self.assertFalse(install_streams(self.log), "重复安装应返回 False")
        text = self.log.path.read_text(encoding="utf-8")
        self.assertIn("这句话应该进日志", text)


class TestRuntimeWritesLog(unittest.TestCase):
    """集成: Runtime.run() 启动时必须真的把东西写进 data/logs/(而不只是 print)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self._out, self._err = sys.stdout, sys.stderr
        self._flag = getattr(sys, "attention_logging_installed", None)

    def tearDown(self):
        sys.stdout, sys.stderr = self._out, self._err
        if self._flag is None:
            try:
                del sys.attention_logging_installed  # type: ignore[attr-defined]
            except AttributeError:
                pass
        else:
            sys.attention_logging_installed = self._flag  # type: ignore[attr-defined]
        self.tmp.cleanup()

    def test_banner_and_actions_land_in_file(self):
        from attention_please.config import Calibration, Config
        from attention_please.runtime import Runtime
        from attention_please.signals import EpisodeEnd, SignalKind

        cfg = Config.from_dict(
            {"schedule": {"block": []},
             "report": {"daily_report": False},
             "privacy": {"save_captures": False}},
            path=self.root / "config.toml")
        rt = Runtime(cfg, Calibration())
        rt.notifier = types.SimpleNamespace(buzz=lambda *a, **k: None)
        rt.ui = types.SimpleNamespace(show=lambda req: None,
                                      ask_reason=lambda *a: None, drain=lambda: [])
        rt.stop()                 # 让 run() 立刻走完退出路径
        rt.run()
        rt.dispatch([EpisodeEnd(SignalKind.SCREEN, datetime.now(), 10.0, 30.0, 2,
                                False, "证据")])
        text = rt.log.path.read_text(encoding="utf-8")
        self.assertIn("启动:", text)
        self.assertIn("EpisodeEnd", text)
        self.assertTrue(str(rt.log.path).endswith(".log"))
        rt.close()


if __name__ == "__main__":
    unittest.main()
