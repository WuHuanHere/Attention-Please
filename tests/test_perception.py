"""纯逻辑单测: MediaPipe 结果解析(不需要摄像头/模型)。

这里测的是真实踩过的坑: MediaPipe 的 face_blendshapes 在不同版本里形状不一样,
1.0.1 返回 `list[Category]`, 旧版返回带 `.categories` 的对象。写死一种就会 AttributeError。
"""
from __future__ import annotations

import math
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402

from attention_please.perception import (  # noqa: E402
    blendshape_categories,
    euler_from_matrix,
)


class FakeCategory:
    def __init__(self, name: str, score: float = 0.0):
        self.category_name = name
        self.score = score


class FakeClassifications:
    """旧版结构: 带 .categories。"""

    def __init__(self, cats):
        self.categories = cats


class TestBlendshapeCategories(unittest.TestCase):
    def test_mediapipe_1x_list_form(self):
        cats = [FakeCategory("eyeBlinkLeft"), FakeCategory("eyeBlinkRight")]
        got = blendshape_categories([cats])
        self.assertEqual([c.category_name for c in got],
                         ["eyeBlinkLeft", "eyeBlinkRight"])

    def test_legacy_classifications_form(self):
        cats = [FakeCategory("eyeBlinkLeft")]
        got = blendshape_categories([FakeClassifications(cats)])
        self.assertEqual([c.category_name for c in got], ["eyeBlinkLeft"])

    def test_empty_forms(self):
        for value in (None, [], [[]], [FakeClassifications([])]):
            self.assertEqual(blendshape_categories(value), [])


def rot_y(deg: float) -> np.ndarray:
    r = math.radians(deg)
    m = np.eye(4)
    m[0, 0] = m[2, 2] = math.cos(r)
    m[0, 2] = math.sin(r)
    m[2, 0] = -math.sin(r)
    return m


def rot_x(deg: float) -> np.ndarray:
    r = math.radians(deg)
    m = np.eye(4)
    m[1, 1] = m[2, 2] = math.cos(r)
    m[1, 2] = -math.sin(r)
    m[2, 1] = math.sin(r)
    return m


class TestEulerFromMatrix(unittest.TestCase):
    def test_identity_is_zero(self):
        pitch, yaw, roll = euler_from_matrix(np.eye(4))
        self.assertAlmostEqual(pitch, 0.0, places=4)
        self.assertAlmostEqual(yaw, 0.0, places=4)
        self.assertAlmostEqual(roll, 0.0, places=4)

    def test_yaw_around_y_axis(self):
        _, yaw, _ = euler_from_matrix(rot_y(30))
        self.assertAlmostEqual(abs(yaw), 30.0, places=3)

    def test_pitch_around_x_axis(self):
        pitch, _, _ = euler_from_matrix(rot_x(25))
        self.assertAlmostEqual(abs(pitch), 25.0, places=3)

    def test_monotonic_in_sign(self):
        """符号可以正可以负(取决于相机摆位), 但方向必须单调 —— 校准靠这个反推正负。"""
        vals = [euler_from_matrix(rot_y(d))[1] for d in (5, 10, 20, 30)]
        self.assertTrue(all(abs(b) > abs(a) for a, b in zip(vals, vals[1:])))

    def test_scale_noise_is_normalized(self):
        m = rot_y(30)
        m[:3, :3] *= 3.7
        _, yaw, _ = euler_from_matrix(m)
        self.assertAlmostEqual(abs(yaw), 30.0, places=3)


if __name__ == "__main__":
    unittest.main()
