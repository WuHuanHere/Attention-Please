"""滚动基线: 用"你最近的样子"当基准, 而不是校准那一刻的固定值。

## 为什么需要它(不是设计洁癖, 是实测逼出来的)

HANDOFF §9 M4 第 2 条: 校准测出的眨眼基线是 **3.0 次/分**(正常人 15-20), 说明眨眼
检测欠计数 —— 拿这个固定值当"下降 50% 就算发呆"的基准, 判据本身就是坏的。

本次 M4 调研又发现同一个毛病出现在 Pose 上(见 `probe_out/posenoise.md`):
用 Pose 的"鼻子相对肩线的高度"当低头判据时, 它和真人脸 pitch 的相关系数只有 **-0.66**,
而且**同一个 pitch 下 bow 能差 0.3**(pitch≈10° 的样本里 bow 从 0.35 到 0.65)。
相机高度、坐姿、前后距离都会改这个比值 —— 所以**绝对阈值不成立**。

结论: 这类"被相机几何污染的量"只能用**相对判断** —— 拿"你自己最近的样子"当中位数,
再看现在偏离了多少个 MAD。这个模块就是那个机制, 眨眼基线与 Pose 头姿共用它。

## 口径

- `TimeWindow`  按**时间**滑动的样本窗(顺带按条数封顶, 防爆内存);
- `WindowStats` 中位数 / MAD / 四分位 —— 用中位数而不是均值, 因为要能扛住
  "部分出画时肩宽从 240px 掉到 96px"这种离群帧;
- `Baseline`    带**可信样本闸门**(`trusted=False` 的样本不进窗)与**最小样本数**
  (样本不够就 `ready()=False`, 让上层**弃权**而不是猜);
- `deadband()`  `k * MAD` 但**不小于 floor** —— 基线太干净时不许变成风吹草动就触发。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


def median(values: list[float]) -> float:
    vals = sorted(values)
    n = len(vals)
    if n == 0:
        raise ValueError("median() 需要至少一个样本")
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def mad(values: list[float], center: float | None = None) -> float:
    """中位绝对偏差。比标准差抗离群 —— 这是本模块选它的唯一原因。"""
    if not values:
        raise ValueError("mad() 需要至少一个样本")
    c = median(values) if center is None else center
    return median([abs(v - c) for v in values])


@dataclass(frozen=True)
class WindowStats:
    n: int
    median: float
    mad: float
    p10: float
    p25: float
    p75: float
    p90: float

    @property
    def iqr(self) -> float:
        return self.p75 - self.p25

    @property
    def spread(self) -> float:
        """一个稳健的"离散度": MAD 与 IQR/1.35 取大。

        只用 MAD 时, 样本里有超过一半挤在同一个值上会让 MAD 变成 0
        (例如帧率固定、量化到同一个刻度), 于是死区塌成 0 → 风吹草动都触发。
        IQR 在这种退化情况下仍然非零, 两者取大正好补上。
        """
        return max(self.mad, self.iqr / 1.35)

    @property
    def low(self) -> float:
        """p10 —— "你平时这个量的下沿"。"""
        return self.p10

    @property
    def high(self) -> float:
        """p90 —— "你平时这个量的上沿"。"""
        return self.p90


class TimeWindow:
    """按时间滑动的样本窗。`ts` 必须是**单调秒**(与 Observation.ts 同源)。"""

    def __init__(self, seconds: float, maxlen: int = 4096):
        if seconds <= 0:
            raise ValueError("seconds 必须为正")
        self.seconds = float(seconds)
        self.maxlen = int(maxlen)
        self._data: deque[tuple[float, float]] = deque()

    def add(self, ts: float, value: float) -> None:
        self._data.append((float(ts), float(value)))
        self._prune(ts)

    def _prune(self, now: float) -> None:
        while self._data and now - self._data[0][0] > self.seconds:
            self._data.popleft()
        while len(self._data) > self.maxlen:
            self._data.popleft()

    def values(self, now: float | None = None) -> list[float]:
        if now is not None:
            self._prune(now)
        return [v for _, v in self._data]

    def stats(self, now: float | None = None) -> WindowStats | None:
        vals = self.values(now)
        if not vals:
            return None
        vals.sort()
        n = len(vals)
        return WindowStats(
            n=n,
            median=median(vals),
            mad=mad(vals),
            p10=vals[n // 10],
            p25=vals[n // 4],
            p75=vals[(3 * n) // 4],
            p90=vals[min(n - 1, (9 * n) // 10)],
        )

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


class Baseline:
    """一个滚动中位数基线, 带"可信样本"闸门与最小样本数。

    `trusted=False` 的样本被丢弃, 这是**防止基准被污染**的关键:
    例如 Pose 头姿的"直立基准"只接受"人脸确认头没低下"的帧,
    于是长时间低头绝不会把基准慢慢拖到低头的样子上去。
    """

    def __init__(self, seconds: float, *, min_samples: int = 1,
                 maxlen: int = 4096, min_interval: float = 0.0):
        self.window = TimeWindow(seconds, maxlen=maxlen)
        self.min_samples = int(min_samples)
        # 抽稀: 基线是慢变量, 没必要每帧都喂。1800 秒的基准窗若按 5 Hz 全喂,
        # 每帧都要给几千个样本排序, 纯属浪费 CPU。设 1 秒就够。
        self.min_interval = float(min_interval)
        self._last_ts: float | None = None

    def observe(self, ts: float, value: float, *, trusted: bool = True) -> None:
        if not trusted:
            return
        if (self.min_interval > 0 and self._last_ts is not None
                and ts - self._last_ts < self.min_interval):
            return
        self._last_ts = ts
        self.window.add(ts, value)

    def stats(self, now: float | None = None) -> WindowStats | None:
        return self.window.stats(now)

    def ready(self, now: float | None = None) -> bool:
        st = self.window.stats(now)
        return st is not None and st.n >= self.min_samples

    def median(self, now: float | None = None) -> float | None:
        st = self.window.stats(now)
        return None if st is None else st.median

    def clear(self) -> None:
        self.window.clear()
        self._last_ts = None

    def deadband(self, now: float, k: float, floor: float) -> float:
        """`k * spread`, 但不小于 `floor`。

        没有 floor 的话, 一段特别平稳的样本会把死区压到接近 0, 于是
        "比基线低 0.01" 也会被判成低头 —— 那是纯噪声。
        """
        st = self.window.stats(now)
        if st is None:
            return float(floor)
        return max(float(floor), k * st.spread)
