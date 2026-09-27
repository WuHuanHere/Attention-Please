"""画面有效性: 把「**没有画面**」和「画面里没有人」分开。

## 为什么单开一个模块(这是 2026-09-27 那个 bug 的根因)

那天上午用户一直在桌前看网课, 日报却写着「离开座位 40 分钟」。现场重建:

  - 09:27:45 起 DirectShow 采集流死了: `cap.read()` 每次等满 **1000ms** 超时才返回,
    于是 tick 频率从 5.0 Hz 掉到 **0.95 Hz**(1/1.05s);
  - 但它返回的是 `ok=True` + 一帧**纯黑**(不是 None, 不是异常) —— 所以
    runtime 里唯一的失败检查 `if not ok or frame is None` 永远不会触发;
  - 人脸 0% / Pose 0% -> 90 秒后状态机判定「离开座位」, 一错就是 36 分钟;
  - 只有手动"让出 + 收回摄像头"(release + 重开采集流)才恢复(10:07 立刻回到 5.0Hz/99%)。
  - 铁证: `captures/2026-09-27_09-42-29.jpg` 下半张(摄像头画面)灰度 std = **0.05**,
    而正常画面是 **65~70**。

结论: 「Pose 丢了」被当成了「人走了」, 而真相是「摄像头瞎了」。这两件事必须分开 ——
前者是**我们看不到**, 后者才是**你不在**。铁律里"误报最烦"说的是同一个道理:
拿"没有画面"去指控你离开, 是最没道理的那种误报。

## 判据只用了灰度标准差(实测标定, 不是拍的)

1280x720 灰度图降到 160x120(INTER_AREA)之后:

| 画面 | 均值 | **标准差** |
|---|---|---|
| 纯黑帧(2026-09-27 09:42 真实证据图) | 1.0 | **0.01** |
| 纯黑帧(合成) | 0 | **0.00** |
| 正常画面(2026-09-27 10:53 / 10:55 真实证据图) | 143~150 | **65.2 / 65.6** |
| 均匀噪声(合成, 最坏情况) | 127.5 | **7.2** |

所以默认阈值 `blank_std = 6.0` 落在一条很宽的空隙里: 比正常画面低 13 倍,
比真实黑帧高 500 倍。**只看标准差, 不看均值** —— 全黑、全白、镜头被挡住、
镜头对着白墙都是一回事: 这张图里没有任何信息, 谁也别想从里面读出"你在不在"。

## 帧间变化(帧被"冻住")只记录, 不判定

`FrameReading.motion` 是量出来的(实测静坐时 1.1~10.1, 而 0.95Hz 那次是真黑帧),
但我们**没有**拿它当判据: 没有现场证据表明出现过"退回同一张旧帧"的故障,
而误判的代价不对称 —— 假"没有画面"会让监控静默停摆(漏报)。
哪天真出现了, 日志里那行 `motion=` 就是证据, 到时候再加规则。

## 分层

`LivenessWatch` 是**纯逻辑**(不碰 numpy/时间), 用假数据就能单测;
`FrameHealthMeter` 负责从真图里量那几个数(实测 0.7~0.9ms/帧, 5Hz 下约 0.4% 单核)。
"""
from __future__ import annotations

from dataclasses import dataclass

# ---- LivenessWatch.observe() 的三种结论 ----
OK = "ok"        # 这一帧有画面
BLANK = "blank"  # 这一帧没有画面, 但还没到认定为设备故障的时长
TRIP = "trip"    # 刚刚到达门槛: 可以按设备故障处理了(**每段故障只返回一次**)

STATS_WIDTH = 160       # 统计用的小图: 再小就看不出渐变, 再大就白花 CPU
STATS_HEIGHT = 120


@dataclass(frozen=True)
class FrameReading:
    """一帧的统计量。全是调用方量好的数字, 这里不碰图像。"""

    mean: float = 0.0
    std: float = 0.0
    motion: float | None = None      # 与上一帧的平均绝对差; 首帧为 None

    def describe(self) -> str:
        motion = "—" if self.motion is None else f"{self.motion:.2f}"
        return f"灰度均值 {self.mean:.1f}, 标准差 {self.std:.2f}, 帧间变化 {motion}"


class LivenessWatch:
    """连续多久"没有画面"才算摄像头故障。

    为什么要连续一段时间而不是一帧: 摄像头刚打开的头几帧本来就可能是黑的
    (所以每次重开都要 `reset()`), 偶发的掉帧也一样。`blank_seconds` 默认 20 秒,
    远小于"离开座位"的 90 秒门槛 —— 也就是说**故障一定先于离开被判定**, 不会打架。
    """

    def __init__(self) -> None:
        self.dead_since: float | None = None    # 第一次读到"没有画面"的单调时刻
        self.tripped = False

    def observe(self, reading: FrameReading, now: float, *,
                blank_seconds: float, blank_std: float) -> str:
        if reading.std >= blank_std:
            self.dead_since = None
            self.tripped = False
            return OK
        if self.dead_since is None:
            self.dead_since = now
            return BLANK
        if not self.tripped and now - self.dead_since >= blank_seconds:
            self.tripped = True
            return TRIP
        return BLANK

    def blank_for(self, now: float) -> float:
        """这一轮"没有画面"已经持续了多少秒。"""
        return 0.0 if self.dead_since is None else now - self.dead_since

    def reset(self) -> None:
        self.dead_since = None
        self.tripped = False


class FrameHealthMeter:
    """从真实的 BGR 帧里量出 `FrameReading`, 并持有上一帧用来算帧间变化。"""

    def __init__(self) -> None:
        self.watch = LivenessWatch()
        self._prev = None

    def reading(self, frame_bgr) -> FrameReading:
        import cv2
        import numpy as np

        gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        if gray.shape != (STATS_HEIGHT, STATS_WIDTH):
            gray = cv2.resize(gray, (STATS_WIDTH, STATS_HEIGHT),
                              interpolation=cv2.INTER_AREA)
        motion = None
        if self._prev is not None and self._prev.shape == gray.shape:
            motion = float(np.abs(gray.astype("int16")
                                  - self._prev.astype("int16")).mean())
        self._prev = gray
        return FrameReading(mean=float(gray.mean()), std=float(gray.std()),
                            motion=motion)

    def reset(self) -> None:
        """重开摄像头之后调 —— 上一帧的画面和"已经黑了多久"都不再作数。"""
        self._prev = None
        self.watch.reset()
