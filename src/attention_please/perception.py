"""MediaPipe 封装: 一帧 BGR 图 -> 头姿角 / 眨眼 / 稳定性。

只做"特征提取", 不做判定 —— 判定在 signals.py(纯逻辑、可单测)。
输出的角度已经按校准的符号归一化成 **右偏为正 / 低头为正**, 所以下游不需要关心
MediaPipe 的坐标系到底是左手还是右手。

## 为什么是"双分辨率"管线(这是实测逼出来的, 不是设计洁癖)

实测数据(本机, 笔记本内置摄像头):
  - 640x480 整帧喂给 face_landmarker  -> 人脸检出 **0%**(任何阈值、任何提亮都无效)
  - 1280x720 整帧                      -> 依然 0%
  - 1280x720 + Pose 定位头部后裁剪放大   -> 头部框 >=240px 时 **100%**

根因: 640x480 下 Pose 给出的头部框只有 47x52 像素, 脸约 40px 高, 而 ModelPipe 的
face_landmarker 内部是 blaze_face_short_range 检测器, 对小脸基本无感。
同一张 820x1024 的标准人像它 100% 检出, 所以不是模型坏, 是脸在画面里太小。

于是管线固定为:
  1) 全分辨率帧 -> 缩到 640 宽 -> Pose(便宜, 只为定位头部)
  2) 用 Pose 的头部关键点(鼻/眼/耳/嘴)在原分辨率上裁出头部区域, 放大到 >=320 宽
  3) 放大后的头部图 -> FaceLandmarker(头姿矩阵 + blendshape 眨眼)
  4) 拿不到人脸时**不猜**: face_present=False, 由上层记为"看不清"状态
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .config import Calibration
from .pose_head import PoseHead, PoseHeadEstimator

STABILITY_WINDOW_SECONDS = 20.0  # "头没动"的观察窗(短窗 + 长连续时长 = 真发呆)
BLINK_WINDOW_SECONDS = 60.0      # 眨眼率统计窗
BLINK_THRESHOLD = 0.5
POSE_INPUT_WIDTH = 640           # Pose 只看这个宽度的缩略图
FACE_CROP_WIDTH = 320            # 头部裁剪后放大到的宽度(实测 320 够用)
HEAD_LANDMARKS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10)  # 鼻/眼/耳/嘴
MIN_VISIBILITY = 0.5


@dataclass
class FrameFeatures:
    pose_present: bool = False
    face_present: bool = False
    yaw: float | None = None
    pitch: float | None = None
    roll: float | None = None
    yaw_std: float | None = None
    pitch_std: float | None = None
    blink_rate: float | None = None
    eye_closed_ratio: float | None = None
    blink_event: bool = False
    head_box_px: int = 0          # 头部框高度(像素): 判断"距离够不够"的唯一硬指标
    # Pose 弱证据(看不到脸时的粗头姿)。None = 弃权。**只能记录, 不能报警**。
    pose_head: PoseHead | None = None
    pose_landmarks: object = None
    face_landmarks: object = None


MAX_TEARDOWN_IS_SLOW = True  # 见 close_async 的说明


def close_async(*objects) -> None:
    """把 MediaPipe 对象的 close() 丢到 daemon 线程里, 立刻返回。

    实测(本机 mediapipe 1.0.1): `FaceLandmarker.close()` 要 **42 秒**才返回,
    而所有模型的 *创建* 只要 0.1 秒 —— 启动很快, 退出极慢。
    如果在退出路径上同步 close, 用户会看到"关不掉、像卡死",
    诊断脚本更会被误判成"挂住了"。daemon 线程保证进程该退就退。

    代价: 进程退出时可能有原生内存没被释放 —— 进程都要结束了, 无所谓。
    """
    import threading

    def _run() -> None:
        for obj in objects:
            try:
                obj.close()
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_run, name="mp-teardown", daemon=True).start()


def euler_from_matrix(matrix) -> tuple[float, float, float]:
    """4x4 变换矩阵 -> (pitch, yaw, roll), 单位度。

    只取旋转部分做 Tait-Bryan 分解。具体哪个轴叫什么不重要 —— 符号由校准反推,
    这里只要能稳定反映"头往哪边转、低了多少"。
    """
    m = np.asarray(matrix, dtype="float64")
    r = m[:3, :3]
    for i in range(3):  # 去掉可能的尺度残留
        n = np.linalg.norm(r[:, i])
        if n > 1e-9:
            r[:, i] /= n
    sy = math.sqrt(r[0, 0] ** 2 + r[1, 0] ** 2)
    if sy > 1e-6:
        x = math.atan2(r[2, 1], r[2, 2])
        y = math.atan2(-r[2, 0], sy)
        z = math.atan2(r[1, 0], r[0, 0])
    else:
        x = math.atan2(-r[1, 2], r[1, 1])
        y = math.atan2(-r[2, 0], sy)
        z = 0.0
    return math.degrees(x), math.degrees(y), math.degrees(z)


def blendshape_categories(face_blendshapes) -> list:
    """取第一张脸的 blendshape 类别列表。

    MediaPipe 的返回结构跨版本不一致: 1.x 是 `list[Category]`,
    更早的版本是带 `.categories` 的 Classifications 对象。这里两种都吃。
    """
    if not face_blendshapes:
        return []
    first = face_blendshapes[0]
    cats = getattr(first, "categories", first)
    return list(cats) if cats is not None else []


def _blendshape_index(categories) -> dict[str, int]:
    return {c.category_name: i for i, c in enumerate(categories)}


def head_roi_from_pose(pose_landmarks, shape, pad: float = 0.45):
    """用 Pose 的头部关键点框出人脸区域, 返回 (x1, y1, x2, y2) 或 None。

    这是双分辨率管线的第一步: Pose 免费送我们一个"头在哪"的先验(0-10 号点),
    据此才能在原分辨率上把脸裁出来放大。
    """
    if pose_landmarks is None:
        return None
    h, w = shape[:2]
    xs, ys = [], []
    for idx in HEAD_LANDMARKS:
        if idx >= len(pose_landmarks):
            continue
        lm = pose_landmarks[idx]
        vis = getattr(lm, "visibility", 1.0)
        if vis is not None and vis < MIN_VISIBILITY:
            continue
        xs.append(lm.x * w)
        ys.append(lm.y * h)
    if len(xs) < 3:
        return None
    x1, x2 = min(xs), max(xs)
    y1, y2 = min(ys), max(ys)
    bw, bh = max(1.0, x2 - x1), max(1.0, y2 - y1)
    ox1 = int(max(0, x1 - bw * pad))
    ox2 = int(min(w, x2 + bw * pad))
    oy1 = int(max(0, y1 - bh * (pad + 0.3)))   # 头顶留更多(发际线/额头)
    oy2 = int(min(h, y2 + bh * (pad + 0.2)))
    if ox2 - ox1 < 12 or oy2 - oy1 < 12:
        return None
    return ox1, oy1, ox2, oy2


class FrameAnalyzer:
    """持有 MediaPipe 模型与滚动窗口状态。线程不安全, 单线程用。"""

    def __init__(self, pose_model: Path, face_model: Path,
                 calibration: Calibration | None = None,
                 stability_window: float = STABILITY_WINDOW_SECONDS,
                 blink_window: float = BLINK_WINDOW_SECONDS,
                 with_blendshapes: bool = True,
                 pose_head_kwargs: dict | None = None):
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision

        for path in (pose_model, face_model):
            if not Path(path).exists():
                raise FileNotFoundError(
                    f"缺少模型文件: {path}\n先跑 scripts/download_models.py")

        self.pose = vision.PoseLandmarker.create_from_options(
            vision.PoseLandmarkerOptions(
                base_options=mp_python.BaseOptions(model_asset_path=str(pose_model)),
                running_mode=vision.RunningMode.VIDEO,
                num_poses=1,
                min_pose_detection_confidence=0.5,
                min_pose_presence_confidence=0.5,
                min_tracking_confidence=0.5,
            )
        )
        # 注意: blendshape 图会让模型创建明显变慢(实测首次 ~40s)。
        # 不值得为它牺牲启动体验时, 把它关掉, 发呆检测会退化为"只看头没动"。
        self.face = vision.FaceLandmarker.create_from_options(
            vision.FaceLandmarkerOptions(
                base_options=mp_python.BaseOptions(model_asset_path=str(face_model)),
                running_mode=vision.RunningMode.VIDEO,
                num_faces=1,
                min_face_detection_confidence=0.5,
                min_face_presence_confidence=0.5,
                min_tracking_confidence=0.5,
                output_face_blendshapes=with_blendshapes,
                output_facial_transformation_matrixes=True,
            )
        )
        self.cal = calibration or Calibration()
        self.stability_window = stability_window
        self.blink_window = blink_window
        self._angles: deque[tuple[float, float]] = deque()   # (ts, yaw, pitch)
        self._blinks: deque[float] = deque()
        self._eye_closed: deque[tuple[float, bool]] = deque()
        self._blink_prev = False
        self._blink_names: dict[str, int] | None = None
        # Pose 弱证据: 看不到脸时的粗头姿。纯逻辑, 见 pose_head.py —— 它**只记录不报警**,
        # 是"低头写题时人脸覆盖率只有 12-15%"这个死角的兜底(实测那段时间 Pose 还有 80%)。
        self.pose_head_estimator = PoseHeadEstimator(**(pose_head_kwargs or {}))

    # ---- 主入口 ----
    def analyze(self, frame_bgr, ts_ms: int, now: float | None = None) -> FrameFeatures:
        import mediapipe as mp

        now = time.monotonic() if now is None else now
        feats = FrameFeatures()
        fh, fw = frame_bgr.shape[:2]

        # --- 1) 低分辨率跑 Pose, 只为定位头部 ---
        if fw > POSE_INPUT_WIDTH:
            scale = POSE_INPUT_WIDTH / fw
            small = cv2.resize(frame_bgr, (POSE_INPUT_WIDTH, max(1, int(fh * scale))),
                               interpolation=cv2.INTER_AREA)
        else:
            small = frame_bgr
        small_img = mp.Image(image_format=mp.ImageFormat.SRGB,
                             data=cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
        pres = self.pose.detect_for_video(small_img, ts_ms)
        roi = None
        if pres.pose_landmarks:
            feats.pose_present = True
            feats.pose_landmarks = pres.pose_landmarks[0]
            roi_small = head_roi_from_pose(pres.pose_landmarks[0], small.shape)
            if roi_small is not None:
                sx, sy = fw / small.shape[1], fh / small.shape[0]
                roi = (int(roi_small[0] * sx), int(roi_small[1] * sy),
                       min(int(roi_small[2] * sx), fw), min(int(roi_small[3] * sy), fh))
                feats.head_box_px = roi[3] - roi[1]

        # --- 2) 原分辨率裁头 + 放大 ---
        crop_img = None
        if roi is not None:
            crop = frame_bgr[roi[1]:roi[3], roi[0]:roi[2]]
            if crop.size:
                cw = crop.shape[1]
                if cw < FACE_CROP_WIDTH:
                    s = FACE_CROP_WIDTH / cw
                    crop = cv2.resize(crop, None, fx=s, fy=s,
                                      interpolation=cv2.INTER_CUBIC)
                crop_img = mp.Image(image_format=mp.ImageFormat.SRGB,
                                    data=cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))

        # --- 3) 在人脸图上跑 FaceLandmarker ---
        fres = None
        if crop_img is not None:
            fres = self.face.detect_for_video(crop_img, ts_ms)
            if fres.face_landmarks:
                feats.face_present = True
                feats.face_landmarks = fres.face_landmarks[0]

        # --- 4) 头姿角(旋转与平移/尺度无关, 所以瘦身后的裁剪图照样能用) ---
        if fres is not None and fres.facial_transformation_matrixes:
            pitch_r, yaw_r, roll = euler_from_matrix(fres.facial_transformation_matrixes[0])
            yaw = yaw_r * self.cal.yaw_sign
            pitch = pitch_r * self.cal.pitch_sign
            feats.yaw, feats.pitch, feats.roll = yaw, pitch, roll
            self._angles.append((now, yaw, pitch))
            while self._angles and now - self._angles[0][0] > self.stability_window:
                self._angles.popleft()
            if len(self._angles) >= 3:
                arr = np.array([(a[1], a[2]) for a in self._angles])
                feats.yaw_std = float(arr[:, 0].std())
                feats.pitch_std = float(arr[:, 1].std())

        # --- 5) 眨眼 ---
        if fres is not None and fres.face_blendshapes:
            cats = blendshape_categories(fres.face_blendshapes)
            if self._blink_names is None and cats:
                self._blink_names = _blendshape_index(cats)
            if self._blink_names:
                i_l = self._blink_names.get("eyeBlinkLeft")
                i_r = self._blink_names.get("eyeBlinkRight")
                scores = [cats[i].score for i in (i_l, i_r)
                          if i is not None and i < len(cats)]
                if scores:
                    closed = (sum(scores) / len(scores)) > BLINK_THRESHOLD
                    if closed and not self._blink_prev:
                        self._blinks.append(now)
                        feats.blink_event = True
                    self._blink_prev = closed
                    self._eye_closed.append((now, closed))
                    while self._eye_closed and now - self._eye_closed[0][0] > self.blink_window:
                        self._eye_closed.popleft()
                    while self._blinks and now - self._blinks[0] > self.blink_window:
                        self._blinks.popleft()
                    span = max(1.0, now - self._eye_closed[0][0]) if self._eye_closed \
                        else self.blink_window
                    feats.blink_rate = len(self._blinks) * 60.0 / span
                    feats.eye_closed_ratio = (sum(1 for _, c in self._eye_closed if c)
                                              / len(self._eye_closed))

        # --- 6) Pose 弱证据(看不到脸时的粗头姿) ---
        # 必须放在人脸之后: 它要用 face_pitch 来判断"这一帧算不算直立基准样本",
        # 而直立基准**只吃"人脸确认头没低"的帧** —— 这样长时间低头不会把基准拖走。
        # 注意用 frame_bgr.shape: Pose 关键点是归一化坐标, 与喂进去的缩略图无关。
        feats.pose_head = self.pose_head_estimator.observe(
            now, feats.pose_landmarks, frame_bgr.shape, face_pitch=feats.pitch)
        return feats

    def close(self) -> None:
        for obj in (self.pose, self.face):
            try:
                obj.close()
            except Exception:  # noqa: BLE001
                pass

    def close_async(self) -> None:
        """退出路径请用这个 —— 同步 close 在这台机器上要 40 多秒。"""
        close_async(self.pose, self.face)
