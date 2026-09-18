"""跨线程与 UI 事件处理的回归测试。

守的是托盘版实测踩到的**致命** bug:
  Runtime 在主线程创建(SQLite 连接也是), 监控循环在 worker 线程跑 ——
  于是每一帧都抛 `ProgrammingError: SQLite objects created in a thread ...`,
  整条监控链路静默瘫痪, 而托盘菜单还是好的(菜单动作不查库),
  表现就是"图标在、菜单能点、但怎么点都没反应"。

另外还守一个次生 bug: UI 事件(暂停理由)只在"判定中"才被处理,
导致暂停/恢复在某些状态下丢事件。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.config import Calibration, Config  # noqa: E402
from attention_please.runtime import Runtime  # noqa: E402
from attention_please.store import Store  # noqa: E402

RAW = {
    "schedule": {"block": [{"name": "上午", "start": "08:30", "end": "10:30"}]},
    "report": {"daily_report": False, "report_dir": "data/reports"},
    "privacy": {"save_captures": False},
}


class TestStoreCrossThread(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(pathlib.Path(self.tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_other_thread_can_write_and_read(self):
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                now = datetime.now()
                self.store.coverage_tick(now, pose_hit=True, face_hit=True, judging=True)
                self.store.event("nudge", level=1, signal="screen")
                self.store.day_stats(now.date().isoformat())
                self.store.flush()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=worker)
        t.start()
        t.join(5)
        self.assertEqual(errors, [], f"跨线程访问数据库失败: {errors}")

    def test_two_threads_alternating(self):
        """多个线程各写各的(每写一次提交一次, 和真实运行时的节奏一致)。"""
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(5):
                    self.store.coverage_tick(datetime.now(), True, True, True)
                    self.store.flush()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(errors, [], f"多线程写入失败: {errors}")

    def test_reopen_after_close_in_same_thread(self):
        self.store.event("nudge", level=1)
        self.store.close()
        self.assertEqual(self.store.day_stats(datetime.now().date().isoformat()).nudges, 1)


class TestRuntimeFromWorkerThread(unittest.TestCase):
    """Runtime 在主线程建好, 但在别的线程里用 —— 托盘版就是这么跑的。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_and_ticks_from_worker_thread(self):
        cfg = Config.from_dict(RAW, path=self.root / "config.toml")
        rt = Runtime(cfg, Calibration())
        rt.ui = types.SimpleNamespace(show=lambda req: None,
                                      ask_reason=lambda *a: None, drain=lambda: [])
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(3):
                    rt._tick()          # 时间表外 -> 不碰摄像头, 只走记账路径
                rt.status_text()
                rt.state_key()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=worker)
        t.start()
        t.join(10)
        self.assertEqual(errors, [], f"工作线程驱动 Runtime 失败: {errors}")
        rt.close()


class TestUiEventsAlwaysDrained(unittest.TestCase):
    """暂停理由必须在**任何状态下**都被处理(不能只在判定中)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _runtime(self, events):
        cfg = Config.from_dict(RAW, path=self.root / "config.toml")
        rt = Runtime(cfg, Calibration())
        rt.notifier = types.SimpleNamespace(buzz=lambda *a, **k: None)
        rt.ui = types.SimpleNamespace(show=lambda req: None,
                                      ask_reason=lambda *a: None,
                                      drain=lambda: list(events))
        return rt

    def test_reason_event_pauses_even_outside_schedule(self):
        rt = self._runtime([("reason", "去吃饭")])
        rt._tick()                      # 时间表外, 判定为 False
        self.assertTrue(rt.paused, "暂停理由在'非判定状态'下被丢掉了")
        self.assertEqual(rt.state_key(), "paused")
        rt.close()

    def test_cancelled_reason_does_not_pause(self):
        rt = self._runtime([("reason_cancelled", "")])
        rt._tick()
        self.assertFalse(rt.paused)
        rt.close()

    def test_wrong_feedback_event_recorded(self):
        rt = self._runtime([("wrong", "screen")])
        rt._tick()
        rows = rt.store.conn.execute(
            "SELECT COUNT(*) FROM event WHERE kind = 'wrong'").fetchone()
        self.assertEqual(rows[0], 1)
        rt.close()


if __name__ == "__main__":
    unittest.main()
