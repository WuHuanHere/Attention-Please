"""`framehealth` 的单测: 判据是纯逻辑, 标定数字也必须对得上真实证据。

真实数据(2026-09-27, 见 framehealth 模块 docstring 的表):
  纯黑帧 std 0.01 / 正常画面 std 65 -> 阈值必须落在中间那条很宽的空隙里。
"""
from __future__ import annotations

import pathlib
import sys
import unittest

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.config import Detection  # noqa: E402
from attention_please.framehealth import (  # noqa: E402
    BLANK,
    OK,
    TRIP,
    FrameHealthMeter,
    FrameReading,
    LivenessWatch,
)

BLANK_STD = 6.0


def _blank_frame(w=1280, h=720):
    return np.zeros((h, w, 3), np.uint8)


def _live_frame(w=1280, h=720, shift=0):
    y = np.linspace(0, 255, h).astype(np.uint8)
    frame = np.repeat(y[:, None], w, axis=1)[:, :, None].repeat(3, axis=2)
    rng = np.random.default_rng(0)
    frame = (frame.astype("int16") + rng.integers(-6, 7, frame.shape)).clip(0, 255)
    frame = frame.astype(np.uint8)
    frame[40:160, 20 + shift:100 + shift] = 240
    return frame


class TestLivenessWatch(unittest.TestCase):
    def setUp(self):
        self.w = LivenessWatch()
        self.blank = FrameReading(mean=0.0, std=0.01)
        self.live = FrameReading(mean=140.0, std=65.0)

    def obs(self, reading, now):
        return self.w.observe(reading, now, blank_seconds=20.0, blank_std=BLANK_STD)

    def test_single_blank_frame_is_not_a_fault(self):
        self.assertEqual(self.obs(self.blank, 0.0), BLANK)
        self.assertEqual(self.obs(self.blank, 0.2), BLANK)

    def test_trips_exactly_at_the_threshold(self):
        self.obs(self.blank, 0.0)
        self.assertEqual(self.obs(self.blank, 19.8), BLANK)
        self.assertEqual(self.obs(self.blank, 20.0), TRIP)

    def test_trip_is_returned_only_once_per_episode(self):
        self.obs(self.blank, 0.0)
        self.assertEqual(self.obs(self.blank, 20.0), TRIP)
        self.assertEqual(self.obs(self.blank, 21.0), BLANK)
        self.assertEqual(self.obs(self.blank, 99.0), BLANK)

    def test_one_live_frame_resets_the_clock(self):
        self.obs(self.blank, 0.0)
        self.assertEqual(self.obs(self.live, 10.0), OK)
        self.assertEqual(self.obs(self.blank, 11.0), BLANK)
        self.assertEqual(self.obs(self.blank, 30.0), BLANK, "应该从 11 秒重新数")
        self.assertEqual(self.obs(self.blank, 31.0), TRIP)

    def test_threshold_is_inclusive_on_the_live_side(self):
        self.assertEqual(self.obs(FrameReading(std=BLANK_STD), 0.0), OK)

    def test_blank_for_and_reset(self):
        self.assertEqual(self.w.blank_for(5.0), 0.0)
        self.obs(self.blank, 100.0)
        self.assertAlmostEqual(self.w.blank_for(112.5), 12.5)
        self.w.reset()
        self.assertEqual(self.w.blank_for(112.5), 0.0)
        self.assertEqual(self.obs(self.blank, 113.0), BLANK)

    def test_config_default_sits_in_the_measured_gap(self):
        """默认阈值必须是"远离黑帧、也远离真实画面"的 —— 这是实测标定, 不是拍脑袋。"""
        default = Detection().blank_std
        self.assertLessEqual(default, 7.2 * 0.9, "阈值不能贴到合成均匀噪声(7.2)上")
        self.assertGreaterEqual(default, 0.01 * 20, "阈值必须远远高于真实黑帧(0.01)")
        self.assertLessEqual(default, 65.0 / 5, "阈值必须远远低于真实画面(65)")


class TestFrameHealthMeter(unittest.TestCase):
    def setUp(self):
        self.m = FrameHealthMeter()

    def test_black_frame_has_no_structure(self):
        r = self.m.reading(_blank_frame())
        self.assertLess(r.std, 1.0)
        self.assertEqual(self.m.watch.observe(r, 0.0, blank_seconds=20.0,
                                             blank_std=6.0), BLANK)

    def test_live_frame_is_far_above_the_threshold(self):
        r = self.m.reading(_live_frame())
        self.assertGreater(r.std, 40.0)
        self.assertEqual(self.m.watch.observe(r, 0.0, blank_seconds=20.0,
                                             blank_std=6.0), OK)

    def test_motion_is_none_on_first_frame_then_measured(self):
        first = self.m.reading(_live_frame(shift=0))
        self.assertIsNone(first.motion)
        same = self.m.reading(_live_frame(shift=0))
        self.assertIsNotNone(same.motion)
        self.assertAlmostEqual(same.motion, 0.0, places=6)
        moved = self.m.reading(_live_frame(shift=30))
        self.assertGreater(moved.motion or 0.0, 0.05)

    def test_black_frame_after_live_still_reads_blank(self):
        self.m.reading(_live_frame())
        r = self.m.reading(_blank_frame())
        self.assertLess(r.std, 1.0)

    def test_readings_are_resolution_independent(self):
        """小分辨率(比如摄像头静默回落到 640x480)也必须能判出来。"""
        big = self.m.reading(_live_frame(1280, 720))
        small = FrameHealthMeter().reading(_live_frame(640, 480))
        self.assertGreater(small.std, 40.0)
        self.assertLess(abs(big.std - small.std), 15.0)

    def test_reset_forgets_previous_frame(self):
        self.m.reading(_live_frame())
        self.m.reset()
        self.assertIsNone(self.m.reading(_live_frame()).motion)
        self.assertEqual(self.m.watch.blank_for(1.0), 0.0)


if __name__ == "__main__":
    unittest.main()
