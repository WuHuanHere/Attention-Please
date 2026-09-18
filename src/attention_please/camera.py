"""摄像头开流与选型。

这台机器上有**三个**视频设备: 真 RGB 摄像头、Windows Hello 红外摄像头、以及 OBS 虚拟摄像头。
- 红外出灰度图, 姿态判定直接失效;
- OBS 虚拟摄像头给的是**静帧占位图**, 而且颜色很鲜艳 —— 实测中它把"按饱和度挑彩色设备"
  的判据骗过了(饱和度 136 排第一), 所以判据必须有第二根柱子: 帧间动态度。
自动判据只能把范围缩小, 最后一步用 scripts/pick_camera.py 人眼确认。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480


@dataclass
class CameraInfo:
    index: int
    opened: bool
    backend: str = ""
    width: int = 0
    height: int = 0
    saturation: float = 0.0
    motion: float = 0.0
    kind: str = ""
    error: str = ""

    @property
    def usable(self) -> bool:
        return (self.opened and self.width > 0
                and ("彩色" in self.kind or "偏暗" in self.kind))

    @property
    def is_virtual(self) -> bool:
        return "虚拟" in self.kind


def sample_motion(cap, frames: int = 8, gap: float = 0.06) -> float:
    """连续帧之间的平均变化量。虚拟摄像头(OBS 等)输出静帧, 变化量 ≈ 0。"""
    prev = None
    diffs: list[float] = []
    for _ in range(frames):
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        small = cv2.resize(frame, (160, 120))
        if prev is not None:
            diffs.append(float(np.abs(small.astype("int16") - prev.astype("int16")).mean()))
        prev = small
        time.sleep(gap)
    return float(np.mean(diffs)) if diffs else 0.0


def classify_frame(frame) -> tuple[str, float]:
    """用平均饱和度区分彩色摄像头和红外/灰度摄像头。"""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    sat = float(hsv[:, :, 1].mean())
    b, g, r = (frame[:, :, i].astype("int16") for i in range(3))
    chroma = float(np.mean(np.abs(r - g)) + np.mean(np.abs(g - b))) / 2.0
    if sat < 8 and chroma < 4:
        kind = "灰度/红外(不可用于姿态判定)"
    else:
        kind = "彩色"
    return kind, sat


def open_camera(index: int, width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT,
                fourcc: str | None = None):
    """优先 DirectShow(Windows 上打开更快更稳), 退回默认后端。返回 (cap, backend)。

    fourcc: 传 "MJPG" 时显式协商压缩格式。USB2 摄像头上不设 MJPG 往往只能拿到
    640x480@30, 设了才能要 1280x720 / 1920x1080 —— 而人脸检测需要足够的像素。
    """
    for name, backend in (("CAP_DSHOW", getattr(cv2, "CAP_DSHOW", 700)),
                          ("default", getattr(cv2, "CAP_ANY", 0))):
        cap = cv2.VideoCapture(index, backend)
        if cap.isOpened():
            if fourcc:
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            return cap, name
        cap.release()
    return None, "none"


def set_buffer_size(cap, size: int = 1) -> bool:
    """把摄像头内部缓冲压到最小。

    ⚠️ 必须用 cv2.CAP_PROP_BUFFERSIZE, **不要写魔数**: OpenCV 里 4 是
    CAP_PROP_FRAME_HEIGHT, 写 `cap.set(4, 1)` 等于把画面高度设成 1 像素。
    (这是实测踩到的: 改完采集速率直接掉到 2 Hz。)
    """
    try:
        return bool(cap.set(cv2.CAP_PROP_BUFFERSIZE, size))
    except Exception:  # noqa: BLE001
        return False


def grab_frame(cap, warmup: int = 5):
    """摄像头刚打开时常返回旧帧/空帧, 先丢几帧。"""
    for _ in range(warmup):
        cap.read()
        time.sleep(0.03)
    ok, frame = cap.read()
    return frame if ok else None


def probe_cameras(max_index: int = 5, preview_dir: Path | None = None) -> list[CameraInfo]:
    out: list[CameraInfo] = []
    if preview_dir is not None:
        preview_dir.mkdir(parents=True, exist_ok=True)
    for index in range(max_index + 1):
        info = CameraInfo(index=index, opened=False)
        cap = None
        try:
            cap, backend = open_camera(index)
            if cap is None:
                info.error = "打不开或不存在"
                out.append(info)
                continue
            info.opened = True
            info.backend = backend
            frame = grab_frame(cap)
            if frame is None:
                info.error = "打开了但读不到帧"
                out.append(info)
                continue
            info.height, info.width = frame.shape[:2]
            base_kind, info.saturation = classify_frame(frame)
            info.motion = sample_motion(cap)
            if base_kind.startswith("灰度"):
                info.kind = base_kind
            elif info.motion < 0.5:
                info.kind = f"静帧/虚拟摄像头(不可用, motion={info.motion:.2f})"
            elif info.saturation < 40:
                info.kind = f"彩色但偏暗(motion={info.motion:.2f}, 需人眼确认)"
            else:
                info.kind = f"彩色 RGB(motion={info.motion:.2f})"
            if preview_dir is not None:
                cv2.imwrite(str(preview_dir / f"cam_{index}.jpg"), frame)
        except Exception as exc:  # noqa: BLE001 - 枚举阶段任何异常都不该中断整轮
            info.error = f"{type(exc).__name__}: {exc}"
        finally:
            if cap is not None:
                cap.release()
        out.append(info)
    return out


def pick_usable(cameras: list[CameraInfo], prefer_index: int | None = None) -> CameraInfo | None:
    usable = [c for c in cameras if c.usable]
    if not usable:
        return None
    if prefer_index is not None:
        for c in usable:
            if c.index == prefer_index:
                return c
    return usable[0]


if __name__ == "__main__":
    for c in probe_cameras(preview_dir=Path("probe_out")):
        print(f"[{c.index}] opened={c.opened} {c.backend} {c.width}x{c.height} "
              f"sat={c.saturation:.1f} {c.kind} {c.error}")
