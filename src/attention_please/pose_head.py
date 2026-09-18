"""Pose 头部关键点 -> 低精度头姿。**只做弱证据, 永不报警。**

## 为什么需要它

HANDOFF §9 M4: 低头写题时人脸覆盖率只有 12-15%, 而"发呆""看手机"都依赖人脸关键点
—— 在最需要它们的场景下天然不可用。Pose 的检出率则高得多。本机实测(2026-09-18,
`data/events.sqlite3` 的 `coverage_minute`): **人脸覆盖率 < 60% 的 54 个分钟里,
Pose 仍有 80.6% 的帧在**; 21:49 那段"疑似低头写题"(人脸 33%)Pose 是 100%。

## 三条口径(都是从实测推出来的, 不是设计洁癖)

1. **只做弱证据** —— 本模块的输出**永远不能升级成提醒**。它只回答"看不到脸的时候,
   Pose 觉得你在低头/转头/没动", 由上层记成一条 record-only 的分集。
   理由还是那条铁律: 误报最烦, 而 Pose 的头部关键点(0-10 号)是模型从身体**外推**出来的,
   精度本来就低。

2. **比值, 不是像素** —— 实测部分出画时肩宽在 **96-268 px** 之间跳, 绝对像素量不可用。
   所以特征要么归一化到肩宽(`bow`、`side`), 要么天生就是比值(`ear_ratio`)。

3. **相对, 不是绝对** —— `bow` 与真人脸 pitch 的相关系数只有 **-0.66**, 而且同一个
   pitch 下 bow 能差 0.3(相机高度/坐姿/前后距离都会改它)。所以判据是"相对**你自己
   最近的直立基准**", 而基准**只吃"人脸确认头没低"的帧** ——
   于是长时间低头绝不会把基准慢慢拖到低头的样子上去。样本不够就**弃权**(返回 None)。

   ⚠️ 而且判据用的是基准的**范围**(p10/p90), 不是"中位数 ± k*MAD"。这不是偏好问题:
   实测的直立样本里 bow 本身就在 **0.35-0.78** 之间铺开(坐姿/前后距离的**真实**差异,
   不是噪声), `spread = 0.157` → `k*MAD` 死区高达 0.39, **比"低头"造成的位移(0.35)
   还大**, 真低头反而判不出来。分位数描述的是"范围": 低头只要**掉出这个范围**就算。
   这一条是先用真实数字算出来、再改的设计(`probe_out/posenoise.md`)。

## 实测依据(`probe_out/posenoise.md`, 离线跑 captures 的真实画面)

- Pose 关键点 0-10 的 `visibility` **恒为 1.0**, 哪怕头已经转了 41° ——
  所以**不能用 visibility 当质量闸门**(这点和直觉相反), 必须用几何:
  肩点是否出画 + 肩宽是否和它自己的慢变基准一致;
- `side`(鼻子相对肩中线的横移 / 肩宽)跟着 yaw 走: yaw≈0 时 |side| < 0.15,
  yaw = -34°/-41° 时 side = -0.45/-0.62;
- `bow`(肩线高度 - 鼻子高度, 除以肩宽)见上: 方向对, 但离散度大 → 只能相对判断。
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from .baseline import Baseline, TimeWindow

# --- Pose 关键点索引(MediaPipe Pose, 33 点) ---
NOSE = 0
EYE_L, EYE_R = 2, 5
EAR_L, EAR_R = 7, 8
SHOULDER_L, SHOULDER_R = 11, 12

# --- 窗口与闸门 ---
FEATURE_WINDOW_SECONDS = 3.0     # 短窗: 回答"现在头是什么样"(多帧中位数 = 抗单帧抖动)
SCALE_WINDOW_SECONDS = 120.0     # 肩宽的慢变基准: 用来识破"部分出画把肩宽压掉一半"
REFERENCE_WINDOW_SECONDS = 1800.0  # 直立基准窗(30 分钟)
REFERENCE_MIN_SAMPLES = 30       # 基准样本不够就弃权
REFERENCE_MIN_INTERVAL = 1.0     # 基准是慢变量, 抽稀到 1 秒一个样本
SCALE_MIN_SAMPLES = 20
MIN_SHOULDER_FRAC = 0.06         # 肩宽至少占画面宽度的比例, 低于此必是出画/太远
SCALE_LO, SCALE_HI = 0.6, 1.7    # 肩宽相对慢变基准的合理区间
EDGE_MARGIN = 0.02               # 肩点必须离画面边缘这么远(否则身体被裁)
FEATURE_MIN_SAMPLES = 5          # 短窗里至少这么多帧才给结论
QUALITY_WINDOW_SECONDS = 60.0    # 几何可用率统计窗
MIN_QUALITY = 0.5                # 几何可用率低于此就弃权
STILL_RATIO = 0.5                # 短窗离散度小于基准 IQR 的这个比例才算"没动"
DEFAULT_BOOK_PITCH_DEG = 20.0
DEFAULT_DOWN_K = 1.5             # 判定余量 = max(floor, k * 短窗离散度)
DEFAULT_TURN_K = 1.5
# 余量下限。实测直立 bow 的 p10 ≈ 0.37, 而低头样本 bow ≈ 0.11 —— 中间有 0.26 的空档,
# 0.08 落在空档里。⚠️ 这个默认值目前只由**一个**低头样本支撑, 必须真人实测确认。
DEFAULT_MIN_MARGIN = 0.08


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


@dataclass(frozen=True)
class PoseGeometry:
    """尺度无关的粗头姿特征。"""

    shoulder_w: float   # 像素(只用于质量闸门, 不直接参与判定)
    bow: float          # (肩线 y - 鼻 y) / 肩宽 —— 越小 = 头越低
    side: float         # (鼻 x - 肩中线 x) / 肩宽 —— 越大 = 越偏一侧
    ear_ratio: float    # (右耳距 - 左耳距) / (右耳距 + 左耳距), 天生尺度无关


def geometry_from_landmarks(landmarks, shape) -> PoseGeometry | None:
    """33 个 Pose 关键点 -> 粗头姿特征。拿不准就返回 None(**弃权**, 不猜)。

    `landmarks` 用鸭子类型: 只要每个点有 `.x/.y`(归一化)和可选的 `.visibility`,
    所以可以直接喂 MediaPipe 的 `NormalizedLandmark` 列表, 测试里也能喂假的。
    """
    if landmarks is None:
        return None
    h, w = shape[:2]
    if len(landmarks) <= SHOULDER_R or w <= 0 or h <= 0:
        return None

    def vis(i: int) -> float:
        v = getattr(landmarks[i], "visibility", 1.0)
        return 1.0 if v is None else float(v)

    # 肩点必须**在画面里**(留边)且可见。部分出画时 Pose 照样给出肩点, 但那是外推的,
    # 肩宽会缩到 1/3, 后面所有比值全变垃圾(实测那一帧: 肩宽 96px, 常态 204px)。
    for i in (SHOULDER_L, SHOULDER_R):
        lm = landmarks[i]
        if not (EDGE_MARGIN <= lm.x <= 1.0 - EDGE_MARGIN):
            return None
        if not (0.0 <= lm.y <= 1.0):
            return None
        if vis(i) < 0.5:
            return None

    def px(i: int) -> tuple[float, float]:
        lm = landmarks[i]
        return (lm.x * w, lm.y * h)

    sh_w = _dist(px(SHOULDER_L), px(SHOULDER_R))
    if sh_w < MIN_SHOULDER_FRAC * w:
        return None

    sh_mid = ((px(SHOULDER_L)[0] + px(SHOULDER_R)[0]) / 2.0,
              (px(SHOULDER_L)[1] + px(SHOULDER_R)[1]) / 2.0)
    nose = px(NOSE)
    d_l, d_r = _dist(nose, px(EAR_L)), _dist(nose, px(EAR_R))
    return PoseGeometry(
        shoulder_w=sh_w,
        bow=(sh_mid[1] - nose[1]) / sh_w,
        side=(nose[0] - sh_mid[0]) / sh_w,
        ear_ratio=((d_r - d_l) / (d_r + d_l)) if (d_l + d_r) > 1e-6 else 0.0,
    )


@dataclass(frozen=True)
class PoseHead:
    """一帧粗头姿结论。`*_delta` 是相对"你自己的直立基准"的偏差。"""

    head_down: bool
    head_turned: bool
    still: bool
    bow: float
    side: float
    bow_delta: float
    side_delta: float
    ear_ratio: float
    quality: float
    samples: int

    @property
    def posture(self) -> str:
        parts = []
        if self.head_down:
            parts.append("低头")
        if self.head_turned:
            parts.append("转头")
        parts.append("没动" if self.still else "在动")
        return "/".join(parts)

    def describe(self) -> str:
        return (f"Pose 弱证据({self.posture}; "
                f"相对直立基准 bow {self.bow_delta:+.2f} side {self.side_delta:+.2f}, "
                f"采样 {self.samples} 帧/几何可用 {self.quality * 100:.0f}%)")


class PoseHeadEstimator:
    """有状态: 需要"直立基准"和"肩宽慢变基准", 所以必须跨帧累积。

    `ts` 必须是单调秒。线程不安全(单线程用)。
    """

    def __init__(self, *, down_k: float = DEFAULT_DOWN_K,
                 turn_k: float = DEFAULT_TURN_K,
                 min_margin: float = DEFAULT_MIN_MARGIN,
                 book_pitch_deg: float = DEFAULT_BOOK_PITCH_DEG,
                 feature_window: float = FEATURE_WINDOW_SECONDS,
                 scale_window: float = SCALE_WINDOW_SECONDS,
                 reference_window: float = REFERENCE_WINDOW_SECONDS,
                 reference_min_samples: int = REFERENCE_MIN_SAMPLES):
        self.down_k = float(down_k)
        self.turn_k = float(turn_k)
        self.min_margin = float(min_margin)
        self.book_pitch_deg = float(book_pitch_deg)
        self._bow = TimeWindow(feature_window)
        self._side = TimeWindow(feature_window)
        self._scale = Baseline(scale_window, min_samples=SCALE_MIN_SAMPLES)
        # 直立基准只吃"人脸确认头没低"的帧, 所以低头不会污染它
        self._ref_bow = Baseline(reference_window,
                                 min_samples=reference_min_samples,
                                 min_interval=REFERENCE_MIN_INTERVAL)
        self._ref_side = Baseline(reference_window,
                                  min_samples=reference_min_samples,
                                  min_interval=REFERENCE_MIN_INTERVAL)
        self._marks: deque[tuple[float, bool]] = deque()

    # ---- 对外 ----
    def observe(self, ts: float, landmarks, shape, *,
                face_pitch: float | None = None) -> PoseHead | None:
        return self.observe_geometry(ts, geometry_from_landmarks(landmarks, shape),
                                     face_pitch=face_pitch)

    def observe_geometry(self, ts: float, geom: PoseGeometry | None, *,
                         face_pitch: float | None = None) -> PoseHead | None:
        """喂一帧。返回 None = **弃权**(Pose 没有 / 几何不可信 / 基准还没学够)。"""
        if geom is None:
            self._mark(ts, False)
            return None

        # --- 尺度闸门: 肩宽必须和它自己的慢变基准一致 ---
        if self._scale.ready(ts):
            med = self._scale.median(ts)
            if med and med > 0 and not (SCALE_LO <= geom.shoulder_w / med <= SCALE_HI):
                self._mark(ts, False)
                return None
        self._scale.observe(ts, geom.shoulder_w)

        self._bow.add(ts, geom.bow)
        self._side.add(ts, geom.side)
        self._mark(ts, True)

        # --- 直立基准: 只有"人脸确认头没低"的帧才算可信样本 ---
        trusted = face_pitch is not None and face_pitch < self.book_pitch_deg
        self._ref_bow.observe(ts, geom.bow, trusted=trusted)
        self._ref_side.observe(ts, geom.side, trusted=trusted)

        if not (self._ref_bow.ready(ts) and self._ref_side.ready(ts)):
            return None
        bst, sst = self._bow.stats(ts), self._side.stats(ts)
        if bst is None or sst is None or bst.n < FEATURE_MIN_SAMPLES:
            return None
        quality = self._quality()
        if quality < MIN_QUALITY:
            return None

        ref_bow = self._ref_bow.stats(ts)
        ref_side = self._ref_side.stats(ts)
        if ref_bow is None or ref_side is None:
            return None

        # 判定余量随"当前头在动得多厉害"放大: 头在乱动时 Pose 本来就不可信,
        # 那就要求更大的偏移才算数。下限 min_margin 兜住"头很稳"的情况。
        bow_margin = max(self.min_margin, self.down_k * bst.spread)
        turn_margin = max(self.min_margin, self.turn_k * sst.spread)

        return PoseHead(
            # 低头 = 掉出"你平时直立时的 bow 范围"的下沿
            head_down=bst.median < ref_bow.low - bow_margin,
            # 转头 = 掉出 side 范围的任一侧(转头左右都可能)
            head_turned=(sst.median < ref_side.low - turn_margin
                         or sst.median > ref_side.high + turn_margin),
            # 没动 = 比"你平时直立时的姿势变化"还要稳
            still=(bst.spread <= STILL_RATIO * max(ref_bow.iqr, self.min_margin)
                   and sst.spread <= STILL_RATIO * max(ref_side.iqr, self.min_margin)),
            bow=bst.median,
            side=sst.median,
            bow_delta=bst.median - ref_bow.median,
            side_delta=sst.median - ref_side.median,
            ear_ratio=geom.ear_ratio,
            quality=quality,
            samples=bst.n,
        )

    def reset(self) -> None:
        """暂停/退出时清掉跨帧状态 —— 隔了一小时再回来, 旧基准已经没有意义。"""
        self._bow.clear()
        self._side.clear()
        self._scale.clear()
        self._ref_bow.clear()
        self._ref_side.clear()
        self._marks.clear()

    # ---- 内部 ----
    def _mark(self, ts: float, ok: bool) -> None:
        self._marks.append((float(ts), bool(ok)))
        while self._marks and ts - self._marks[0][0] > QUALITY_WINDOW_SECONDS:
            self._marks.popleft()

    def _quality(self) -> float:
        if not self._marks:
            return 0.0
        return sum(1 for _, ok in self._marks if ok) / len(self._marks)
