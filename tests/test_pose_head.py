"""纯逻辑单测: Pose 头部关键点 -> 粗头姿(M4 弱证据通道)。

守住的是 HANDOFF §9 M4 的三条口径:
  1. 只做弱证据(本模块根本不会返回"要不要提醒", 只返回姿势判断);
  2. 比值而不是像素(部分出画时肩宽 96-268px 乱跳, 绝对量不可用);
  3. 相对而不是绝对(拿"你自己的直立基准"比, 而基准只吃"人脸确认头没低"的帧)。

另外守住两条工程约束:
  - **拿不准就弃权**(返回 None), 绝不在看不清的时候替用户下结论;
  - **基准不许被低头污染**, 否则"低头"会慢慢变成"正常", 判定彻底失效。
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.pose_head import (  # noqa: E402
    EAR_L,
    EAR_R,
    EYE_L,
    EYE_R,
    NOSE,
    SHOULDER_L,
    SHOULDER_R,
    PoseGeometry,
    PoseHeadEstimator,
    geometry_from_landmarks,
)

SHAPE = (720, 1280)      # (h, w) —— 与生产管线同源
HZ = 5.0                 # 与 config.toml 的 tick_hz 一致

# --- 真实实测样本(probe_out/posenoise.md, 离线跑 captures 的摄像头那一半) ---
# 只取"人脸确认头没低"(face pitch < 17.4°)的帧当直立基准
REAL_HEAD_UP_BOW = [0.783, 0.506, 0.621, 0.627, 0.396, 0.369, 0.349,
                    0.412, 0.471, 0.458, 0.409, 0.555, 0.650, 0.438, 0.446]
REAL_HEAD_UP_SIDE = [0.094, 0.080, 0.067, 0.108, 0.143, 0.133, 0.003,
                     0.067, 0.136, -0.103, 0.019, -0.032, -0.089, -0.009, -0.004]
REAL_BOWED_BOW = 0.106        # 人脸 pitch 23.3° 那一帧
REAL_TURNED_SIDE = -0.618     # 人脸 yaw -41.0° 那一帧


class FakeLM:
    __slots__ = ("x", "y", "visibility")

    def __init__(self, x: float, y: float, visibility: float = 1.0):
        self.x, self.y, self.visibility = x, y, visibility


def landmarks(*, shoulder_w: float = 0.20, shoulder_y: float = 0.70,
              nose_up: float = 0.15, nose_right: float = 0.0,
              ear_half: float = 0.06, ear_skew: float = 0.0,
              shoulder_vis: float = 1.0, center_x: float = 0.5) -> list:
    """在归一化坐标里摆一个假人。只有 0/2/5/7/8/11/12 号点有意义。

    bow = nose_up * h / (shoulder_w * w);  side = nose_right / shoulder_w
    """
    lms = [FakeLM(0.5, 0.5) for _ in range(33)]
    half = shoulder_w / 2.0
    lms[SHOULDER_L] = FakeLM(center_x - half, shoulder_y, shoulder_vis)
    lms[SHOULDER_R] = FakeLM(center_x + half, shoulder_y, shoulder_vis)
    nx, ny = center_x + nose_right, shoulder_y - nose_up
    lms[NOSE] = FakeLM(nx, ny)
    lms[EAR_L] = FakeLM(nx - ear_half + ear_skew, ny + 0.01)
    lms[EAR_R] = FakeLM(nx + ear_half + ear_skew, ny + 0.01)
    lms[EYE_L] = FakeLM(nx - ear_half * 0.5, ny - 0.005)
    lms[EYE_R] = FakeLM(nx + ear_half * 0.5, ny - 0.005)
    return lms


def geom(*, bow: float = 0.42, side: float = 0.0, shoulder_w: float = 256.0,
         ear_ratio: float = 0.0) -> PoseGeometry:
    return PoseGeometry(shoulder_w=shoulder_w, bow=bow, side=side, ear_ratio=ear_ratio)


def feed(est: PoseHeadEstimator, start: float, seconds: float, *,
         bow: float = 0.42, side: float = 0.0, shoulder_w: float = 256.0,
         face_pitch: float | None = 5.0, hz: float = HZ,
         geom_fn=None, pitch_fn=None):
    """按 hz 喂 seconds 秒, 返回 (最后一次结论, 结束时刻)。"""
    outs, ts = feed_outs(est, start, seconds, bow=bow, side=side,
                         shoulder_w=shoulder_w, face_pitch=face_pitch, hz=hz,
                         geom_fn=geom_fn, pitch_fn=pitch_fn)
    return (outs[-1] if outs else None), ts


def feed_outs(est: PoseHeadEstimator, start: float, seconds: float, *,
              bow: float = 0.42, side: float = 0.0, shoulder_w: float = 256.0,
              face_pitch: float | None = 5.0, hz: float = HZ,
              geom_fn=None, pitch_fn=None) -> tuple[list, float]:
    """按 hz 喂 seconds 秒, 返回 (每一帧的结论列表, 结束时刻)。"""
    outs = []
    ts = start
    for i in range(int(round(seconds * hz))):
        ts += 1.0 / hz
        g = geom_fn(i, ts) if geom_fn else geom(bow=bow, side=side, shoulder_w=shoulder_w)
        p = pitch_fn(i, ts) if pitch_fn else face_pitch
        outs.append(est.observe_geometry(ts, g, face_pitch=p))
    return outs, ts


class TestGeometry(unittest.TestCase):
    def test_none_landmarks_abstains(self):
        self.assertIsNone(geometry_from_landmarks(None, SHAPE))

    def test_too_few_landmarks_abstains(self):
        self.assertIsNone(geometry_from_landmarks([FakeLM(0.5, 0.5)], SHAPE))

    def test_bow_is_positive_when_head_is_up(self):
        g = geometry_from_landmarks(landmarks(nose_up=0.15, shoulder_w=0.20), SHAPE)
        self.assertAlmostEqual(g.bow, 0.15 * 720 / (0.20 * 1280), places=4)

    def test_bow_drops_when_head_bows(self):
        up = geometry_from_landmarks(landmarks(nose_up=0.15), SHAPE)
        down = geometry_from_landmarks(landmarks(nose_up=0.05), SHAPE)
        self.assertLess(down.bow, up.bow)

    def test_side_is_positive_when_nose_moves_right(self):
        right = geometry_from_landmarks(landmarks(nose_right=0.04), SHAPE)
        left = geometry_from_landmarks(landmarks(nose_right=-0.04), SHAPE)
        self.assertGreater(right.side, 0.0)
        self.assertLess(left.side, 0.0)
        self.assertAlmostEqual(right.side, 0.04 / 0.20, places=4)

    def test_ear_ratio_is_antisymmetric_and_sensitive(self):
        """只断言**性质**, 不断言物理正负。

        符号是经验性的(和校准里 yaw_sign 自学同一个道理: MediaPipe 的正负方向
        取决于相机摆位, 硬编码会让信号彻底失效), 所以本模块只如实报告这个比值,
        物理方向由真人实测标定。
        """
        right = geometry_from_landmarks(landmarks(ear_skew=0.05), SHAPE)
        left = geometry_from_landmarks(landmarks(ear_skew=-0.05), SHAPE)
        self.assertAlmostEqual(right.ear_ratio, -left.ear_ratio, places=6)
        self.assertGreater(abs(right.ear_ratio), 0.5)   # 对转头足够敏感

    def test_ear_ratio_is_zero_when_symmetric(self):
        g = geometry_from_landmarks(landmarks(), SHAPE)
        self.assertAlmostEqual(g.ear_ratio, 0.0, places=6)

    def test_shoulder_out_of_frame_abstains(self):
        """部分出画时 Pose 照样给肩点, 但那是外推的 —— 必须弃权。"""
        g = geometry_from_landmarks(landmarks(center_x=0.97), SHAPE)
        self.assertIsNone(g)

    def test_shoulder_width_too_small_abstains(self):
        self.assertIsNone(geometry_from_landmarks(landmarks(shoulder_w=0.03), SHAPE))

    def test_invisible_shoulder_abstains(self):
        self.assertIsNone(
            geometry_from_landmarks(landmarks(shoulder_vis=0.2), SHAPE))

    def test_scale_invariance(self):
        """人前后移动(整体缩放)不该改变这些比值 —— 这正是用比值的原因。"""
        near = geometry_from_landmarks(landmarks(shoulder_w=0.30, nose_up=0.225), SHAPE)
        far = geometry_from_landmarks(landmarks(shoulder_w=0.15, nose_up=0.1125), SHAPE)
        self.assertAlmostEqual(near.bow, far.bow, places=3)


class TestEstimatorAbstains(unittest.TestCase):
    def test_abstains_until_reference_is_learned(self):
        est = PoseHeadEstimator()
        out, ts = feed(est, 0.0, 5.0)          # 只有 5 秒的直立样本
        self.assertIsNone(out)
        self.assertFalse(est._ref_bow.ready(ts))

    def test_abstains_when_pose_is_missing(self):
        est = PoseHeadEstimator()
        feed(est, 0.0, 40.0)                    # 先把基准学好
        self.assertIsNone(est.observe_geometry(45.0, None, face_pitch=None))

    def test_abstains_when_shoulder_scale_collapses(self):
        """实测那一帧: 肩宽 96px vs 常态 204px -> 比值全是垃圾, 必须弃权。"""
        est = PoseHeadEstimator()
        _, ts = feed(est, 0.0, 40.0)
        self.assertIsNotNone(feed(est, ts, 4.0)[0])          # 对照组: 正常帧有结论
        self.assertIsNone(est.observe_geometry(
            ts + 4.2, geom(shoulder_w=100.0), face_pitch=None))

    def test_reset_clears_the_reference(self):
        est = PoseHeadEstimator()
        feed(est, 0.0, 40.0)
        est.reset()
        self.assertIsNone(feed(est, 100.0, 4.0)[0])

    def test_abstains_when_geometry_is_only_intermittently_available(self):
        """几何可用率 < 50% 就弃权 —— 抖动中的 Pose 不值得信。

        注意要看**尾部**: 可用率是 60 秒的滑动统计, 所以刚进入抖动期时,
        之前那段好帧还在窗里, 那会儿给结论是合理的。
        """
        est = PoseHeadEstimator()
        feed(est, 0.0, 40.0)

        def flaky(i, ts):
            return geom(shoulder_w=100.0) if i % 5 >= 2 else geom()

        outs, _ = feed_outs(est, 40.0, 90.0, geom_fn=flaky, face_pitch=None)
        self.assertTrue(all(o is None for o in outs[-15:]),
                        "可用率 40% 时, 抖动期尾部必须弃权")
        self.assertGreater(sum(1 for o in outs if o is not None), 0,
                           "抖动期开头给过结论是合理的, 不该全弃权")

    def test_accepts_when_geometry_is_mostly_available(self):
        est = PoseHeadEstimator()
        feed(est, 0.0, 40.0)

        def mostly_ok(i, ts):
            return geom(shoulder_w=100.0) if i % 5 == 4 else geom()

        outs, _ = feed_outs(est, 40.0, 60.0, geom_fn=mostly_ok, face_pitch=None)
        # 逐帧看: 被拒的那些帧是 None, 但绝大多数帧要给得出结论
        self.assertGreater(sum(1 for o in outs if o is not None), len(outs) * 0.6)


class TestEstimatorJudgement(unittest.TestCase):
    def _learned(self) -> tuple[PoseHeadEstimator, float]:
        est = PoseHeadEstimator()
        _, ts = feed(est, 0.0, 40.0, bow=0.42, side=0.0, face_pitch=5.0)
        return est, ts

    def test_unchanged_posture_is_not_flagged(self):
        """最重要的一条: 什么都没变的时候不许报任何东西。"""
        est, ts = self._learned()
        out, _ = feed(est, ts, 6.0, bow=0.42, side=0.0, face_pitch=5.0)
        self.assertIsNotNone(out)
        self.assertFalse(out.head_down)
        self.assertFalse(out.head_turned)

    def test_bowing_head_is_flagged(self):
        est, ts = self._learned()
        out, _ = feed(est, ts, 8.0, bow=0.15, side=0.0, face_pitch=30.0)
        self.assertTrue(out.head_down)
        self.assertFalse(out.head_turned)

    def test_turning_head_is_flagged(self):
        est, ts = self._learned()
        out, _ = feed(est, ts, 8.0, bow=0.42, side=0.35, face_pitch=None)
        self.assertTrue(out.head_turned)
        self.assertFalse(out.head_down)

    def test_tiny_deviation_inside_deadband_is_ignored(self):
        """基线特别平稳时死区由 floor 兜住, 否则纯噪声就能触发(误报最烦)。"""
        est, ts = self._learned()
        out, _ = feed(est, ts, 8.0, bow=0.40, side=0.0, face_pitch=30.0)
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out.bow_delta, -0.02, places=3)
        self.assertFalse(out.head_down)

    def test_still_true_when_head_does_not_move(self):
        est, ts = self._learned()
        out, _ = feed(est, ts, 8.0, bow=0.15, side=0.0, face_pitch=30.0)
        self.assertTrue(out.still)

    def test_still_false_when_head_jitters(self):
        est, ts = self._learned()

        def jitter(i, _ts):
            return geom(bow=0.15 if i % 2 else 0.35)

        out, _ = feed(est, ts, 8.0, geom_fn=jitter, face_pitch=30.0)
        self.assertIsNotNone(out)
        self.assertFalse(out.still)

    def test_evidence_text_mentions_posture_and_baseline(self):
        est, ts = self._learned()
        out, _ = feed(est, ts, 8.0, bow=0.15, face_pitch=30.0)
        text = out.describe()
        self.assertIn("低头", text)
        self.assertIn("bow", text)


class TestReferenceIsNotPolluted(unittest.TestCase):
    def test_long_bowing_does_not_become_the_new_normal(self):
        """核心风险: 低头久了以后, "低头"被当成基准 -> 判定彻底失效。

        机制: 直立基准**只吃"人脸确认头没低"的帧**(face_pitch < book_pitch_deg)。
        所以先喂 2 分钟低头, 基准依然学不到东西。
        """
        est = PoseHeadEstimator()
        # 2 分钟低头: 有几何, 但人脸确认头是低的 -> 不是可信样本
        out, ts = feed(est, 0.0, 120.0, bow=0.10, face_pitch=30.0)
        self.assertIsNone(out)
        self.assertFalse(est._ref_bow.ready(ts))

        # 接着 40 秒直立 -> 基准这时才学出来
        out, ts = feed(est, ts, 40.0, bow=0.42, face_pitch=5.0)
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out.bow_delta, 0.0, places=3)

        # 关键断言: 现在低头**仍然**要被判成低头(而不是"正常")
        out, _ = feed(est, ts, 8.0, bow=0.10, face_pitch=30.0)
        self.assertTrue(out.head_down)

    def test_face_lost_frames_do_not_teach_the_reference(self):
        """看不到脸的时候 Pose 说什么都不能当基准 —— 否则就成了自证。"""
        est = PoseHeadEstimator()
        out, ts = feed(est, 0.0, 120.0, bow=0.10, face_pitch=None)
        self.assertIsNone(out)
        self.assertFalse(est._ref_bow.ready(ts))


class TestAgainstRealMeasurements(unittest.TestCase):
    """把判据钉在**实测数字**上, 而不是钉在我编的合成数上。

    数字出处: `probe_out/posenoise.md`(离线跑 captures 的摄像头画面, 与生产管线同样的
    640px Pose 输入)。分两拨: "人脸确认头没低"的 15 帧当直立基准, 另有 1 帧低头
    (人脸 pitch 23.3°)与 1 帧转头(人脸 yaw -41.0°)当正样本。

    ⚠️ 这**不能替代真人实测**: 这些是分心时刻的现场帧, 低头只有 1 帧。它守的是
    "机制在这批真实数字上成立", 不是"阈值已经标定好了"。
    """

    def _learn(self):
        est = PoseHeadEstimator()
        ts = 0.0
        for _ in range(3):                     # 循环 3 遍凑够基准样本数
            for bow, side in zip(REAL_HEAD_UP_BOW, REAL_HEAD_UP_SIDE):
                ts += 1.0
                est.observe_geometry(ts, geom(bow=bow, side=side), face_pitch=5.0)
        return est, ts

    def test_real_head_up_samples_are_not_flagged(self):
        """最重要的一条: 真实的直立样本一帧都不许被误判(误报最烦)。"""
        est, ts = self._learn()
        for bow, side in zip(REAL_HEAD_UP_BOW, REAL_HEAD_UP_SIDE):
            out, ts = feed(est, ts, 6.0, bow=bow, side=side, face_pitch=5.0)
            self.assertIsNotNone(out)
            self.assertFalse(out.head_down, f"bow={bow} 被误判成低头")
            self.assertFalse(out.head_turned, f"side={side} 被误判成转头")

    def test_real_bowed_sample_is_flagged(self):
        est, ts = self._learn()
        out, _ = feed(est, ts, 6.0, bow=REAL_BOWED_BOW, face_pitch=30.0)
        self.assertTrue(out.head_down)

    def test_real_turned_sample_is_flagged(self):
        est, ts = self._learn()
        out, _ = feed(est, ts, 6.0, bow=0.578, side=REAL_TURNED_SIDE, face_pitch=None)
        self.assertTrue(out.head_turned)

    def test_median_band_would_have_failed(self):
        """反向测试: 锁住"为什么用分位数范围而不是 中位数 ± k*MAD"这个决策。

        实测直立 bow 的离散度算出来的死区(0.39)**比低头造成的位移(0.35)还大** ——
        也就是说那样写的话, 真低头一帧都判不出来。以后谁想改回中位数带状判据,
        这条测试会拦住他。
        """
        est, ts = self._learn()
        ref = est._ref_bow.stats(ts)
        band = 2.5 * ref.spread
        signal = ref.median - REAL_BOWED_BOW
        self.assertGreater(
            band, signal,
            "如果这条不再成立, 说明基准的离散度变了, 该重新审视判据")


class TestLandmarkApi(unittest.TestCase):
    def test_observe_accepts_landmark_lists(self):
        est = PoseHeadEstimator()
        ts = 0.0
        for _ in range(int(40 * HZ)):
            ts += 1.0 / HZ
            est.observe(ts, landmarks(), SHAPE, face_pitch=5.0)
        for _ in range(int(8 * HZ)):
            ts += 1.0 / HZ
            out = est.observe(ts, landmarks(nose_up=0.05), SHAPE, face_pitch=30.0)
        self.assertIsNotNone(out)
        self.assertTrue(out.head_down)

    def test_observe_abstains_on_missing_landmarks(self):
        est = PoseHeadEstimator()
        self.assertIsNone(est.observe(0.0, None, SHAPE))


if __name__ == "__main__":
    unittest.main()
