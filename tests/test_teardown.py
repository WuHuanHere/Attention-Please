"""close_async 的回归测试。

这条测试守的是一个实测踩到的坑: 本机 mediapipe 1.0.1 的 `FaceLandmarker.close()`
要 42 秒才返回(而模型创建只要 0.1 秒)。如果谁把退出路径改回同步 close,
用户就会看到"关不掉、像卡死", 诊断脚本会被误判成挂住 —— 所以这条必须一直绿。
"""
from __future__ import annotations

import pathlib
import sys
import threading
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.perception import close_async  # noqa: E402


class TestCloseAsync(unittest.TestCase):
    def test_returns_immediately(self):
        gate = threading.Event()

        class Slow:
            def close(self):
                gate.wait(5)  # 模拟 42 秒的排空

        t0 = time.perf_counter()
        close_async(Slow())
        elapsed = time.perf_counter() - t0
        gate.set()
        self.assertLess(elapsed, 0.1, "close_async 必须立刻返回, 不能等 close 跑完")

    def test_calls_close(self):
        called = threading.Event()

        class Obj:
            def close(self):
                called.set()

        close_async(Obj())
        self.assertTrue(called.wait(2.0), "close 最终要被执行")

    def test_handles_multiple_objects(self):
        hits: list[int] = []
        lock = threading.Lock()

        class Obj:
            def __init__(self, n):
                self.n = n

            def close(self):
                with lock:
                    hits.append(self.n)

        close_async(Obj(1), Obj(2))
        for _ in range(50):
            if len(hits) == 2:
                break
            time.sleep(0.02)
        self.assertEqual(sorted(hits), [1, 2])

    def test_swallows_exceptions(self):
        class Boom:
            def close(self):
                raise RuntimeError("不该冒出来")

        close_async(Boom())  # 不抛异常即通过
        time.sleep(0.05)
        self.assertTrue(True)

    def test_accepts_none(self):
        close_async()  # 空参不该炸
        close_async(None)


if __name__ == "__main__":
    unittest.main()
