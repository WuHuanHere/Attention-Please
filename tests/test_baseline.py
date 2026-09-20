"""纯逻辑单测: 滚动基线(中位数 / MAD / 可信样本闸门 / 死区)。

为什么值得单独测: 眨眼基线和 Pose 直立基准都建立在它上面, 而这两处的共同毛病
正是"拿一个固定值当基准"(校准测出 3.0 次/分、bow 的相关系数只有 -0.66)。
基线错了, 上层所有判定跟着错, 所以这一层要有自己的回归测试。
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.baseline import (  # noqa: E402
    Baseline,
    TimeWindow,
    mad,
    median,
)


class TestMedianMad(unittest.TestCase):
    def test_median_odd(self):
        self.assertEqual(median([3, 1, 2]), 2)

    def test_median_even(self):
        self.assertEqual(median([4, 1, 3, 2]), 2.5)

    def test_median_rejects_empty(self):
        with self.assertRaises(ValueError):
            median([])

    def test_mad_uses_median_not_mean(self):
        # 均值会被那个 1000 拖走, 中位数不会 —— 这正是选 MAD 的唯一原因
        vals = [1.0, 1.0, 1.0, 1.0, 1000.0]
        self.assertEqual(mad(vals), 0.0)

    def test_mad_rejects_empty(self):
        with self.assertRaises(ValueError):
            mad([])


class TestTimeWindow(unittest.TestCase):
    def test_prunes_by_time(self):
        w = TimeWindow(seconds=10)
        w.add(0.0, 1.0)
        w.add(5.0, 2.0)
        w.add(11.0, 3.0)
        # 0.0 距 11.0 超过 10 秒 -> 被淘汰
        self.assertEqual(w.values(11.0), [2.0, 3.0])

    def test_keeps_boundary_sample(self):
        w = TimeWindow(seconds=10)
        w.add(0.0, 1.0)
        self.assertEqual(w.values(10.0), [1.0])   # 正好 10 秒不算超

    def test_maxlen_caps_memory(self):
        w = TimeWindow(seconds=10_000, maxlen=5)
        for i in range(50):
            w.add(float(i), float(i))
        self.assertEqual(len(w), 5)

    def test_rejects_nonpositive_window(self):
        with self.assertRaises(ValueError):
            TimeWindow(seconds=0)

    def test_stats_are_robust_to_outlier(self):
        w = TimeWindow(seconds=100)
        for i, v in enumerate([0.40, 0.41, 0.42, 0.40, 0.41, 0.05]):
            w.add(float(i), v)
        st = w.stats(10.0)
        self.assertAlmostEqual(st.median, 0.405, places=3)
        self.assertLess(st.mad, 0.02)      # 那个 0.05 没有把离散度炸开
        self.assertEqual(st.n, 6)

    def test_spread_falls_back_to_iqr_when_mad_collapses(self):
        """MAD 退化时死区不许塌成 0。

        样本里超过一半挤在同一个值上时 MAD 会变成 0(帧率固定、数值被量化时很常见),
        这时若只用 MAD, 死区就是 0 -> "比基线低 0.001" 也会被判成低头。IQR 兜住它。
        """
        w = TimeWindow(seconds=100)
        vals = [1.0] * 10 + [2.0] * 3 + [3.0] * 3
        for i, v in enumerate(vals):
            w.add(float(i), v)
        st = w.stats(20.0)
        self.assertEqual(st.mad, 0.0)
        self.assertGreater(st.iqr, 0.0)
        self.assertGreater(st.spread, 0.5)


class TestBaseline(unittest.TestCase):
    def test_not_ready_until_min_samples(self):
        b = Baseline(seconds=100, min_samples=3)
        b.observe(0.0, 1.0)
        b.observe(1.0, 1.0)
        self.assertFalse(b.ready(1.0))
        b.observe(2.0, 1.0)
        self.assertTrue(b.ready(2.0))

    def test_untrusted_samples_are_dropped(self):
        """这是"低头不许污染直立基准"的机制本体。"""
        b = Baseline(seconds=100, min_samples=2)
        b.observe(0.0, 0.40)
        b.observe(1.0, 0.40)
        for i in range(50):
            b.observe(10.0 + i, 0.05, trusted=False)   # 全是低头
        self.assertTrue(b.ready(60.0))
        self.assertAlmostEqual(b.median(60.0), 0.40, places=6)

    def test_median_returns_none_when_empty(self):
        self.assertIsNone(Baseline(seconds=100).median(0.0))

    def test_min_interval_decimates(self):
        b = Baseline(seconds=100, min_samples=1, min_interval=1.0)
        for i in range(20):
            b.observe(i * 0.2, float(i))     # 5 Hz
        self.assertEqual(len(b.window), 4)  # 0.0 / 1.0 / 2.0 / 3.0(0.2*... 见下)
        self.assertEqual(b.window.values()[0], 0.0)

    def test_min_interval_does_not_block_untrusted_before_trusted(self):
        b = Baseline(seconds=100, min_samples=1, min_interval=1.0)
        b.observe(0.0, 1.0)
        b.observe(0.9, 2.0, trusted=False)   # 被丢弃, 不该推进 _last_ts
        b.observe(1.1, 3.0)                  # 若被推进到 0.9 这里就会被抽稀掉
        self.assertEqual(b.window.values(), [1.0, 3.0])

    def test_deadband_is_gone(self):
        """`deadband()`(k*MAD 死区)已删除 —— 它比"低头"造成的位移还大, 判不出低头。

        现在 pose_head 用的是基准的分位数范围(p10/p90)。这条测试守着"别把它加回来"。
        """
        self.assertFalse(hasattr(Baseline(seconds=100), "deadband"),
                         "deadband 又被加回来了? 先看 baseline.py 的模块注释")

    def test_clear_resets_everything(self):
        b = Baseline(seconds=100, min_samples=1, min_interval=1.0)
        b.observe(0.0, 1.0)
        b.clear()
        self.assertFalse(b.ready(1.0))
        b.observe(0.1, 5.0)                  # clear 之后 _last_ts 也要清掉
        self.assertEqual(b.window.values(), [5.0])


if __name__ == "__main__":
    unittest.main()
