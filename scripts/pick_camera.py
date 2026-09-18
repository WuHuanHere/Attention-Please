"""逐个给你看摄像头, 你自己确认哪台对着你, 并写回 config.toml。

为什么需要这个: 这台机器上有三个视频设备 —— 真 RGB 摄像头、Windows Hello 红外摄像头、
以及 OBS 虚拟摄像头。自动判据(饱和度/静态帧)只能把范围缩小, 最后一步必须人眼确认:
OBS 的占位图颜色很鲜艳, 光看饱和度会被骗。

用法:
  .\\.venv\\Scripts\\python.exe scripts\\pick_camera.py          # 逐个预览, 按 Y 确认
  .\\.venv\\Scripts\\python.exe scripts\\pick_camera.py --index 0 # 只看某一台
  .\\.venv\\Scripts\\python.exe scripts\\pick_camera.py --no-save # 只预览, 不写配置
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please import camera  # noqa: E402
from attention_please.config import PROJECT_ROOT, Config  # noqa: E402

WINDOW = "attention_please - pick camera"


def overlay(frame, lines: list[str]):
    import cv2

    y = 32
    for text in lines:
        cv2.putText(frame, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4)
        cv2.putText(frame, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
        y += 32
    return frame


def write_camera_index(index: int, cfg_path: pathlib.Path) -> str:
    """只改 camera_index 那一行, 其余原样保留(配置文件是人手写的, 不能被程序重排)。"""
    text = cfg_path.read_text(encoding="utf-8")
    new_line = f"camera_index = {index}"
    updated, n = re.subn(r"(?m)^camera_index\s*=\s*\d+.*$", new_line, text)
    if n == 0:
        return "没找到 camera_index 行, 请手动填写"
    cfg_path.write_text(updated, encoding="utf-8")
    return f"已写入 {cfg_path}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=None, help="只看指定设备")
    ap.add_argument("--max-index", type=int, default=5)
    ap.add_argument("--no-save", action="store_true", help="不写回 config.toml")
    ap.add_argument("--seconds", type=float, default=0, help="每个设备自动停留秒数(0=手动)")
    args = ap.parse_args()

    import cv2

    cfg = Config.load()
    out = PROJECT_ROOT / "probe_out"
    print("枚举视频设备(会闪一下摄像头指示灯, 正常)...\n")
    cams = camera.probe_cameras(max_index=args.max_index, preview_dir=out)
    print(f"{'idx':>3} {'打开':<4} {'分辨率':<10} {'饱和度':>7} {'动态度':>7}  判定")
    for c in cams:
        res = f"{c.width}x{c.height}" if c.width else "-"
        print(f"{c.index:>3} {'是' if c.opened else '否':<4} {res:<10} "
              f"{c.saturation:>7.1f} {c.motion:>7.2f}  {c.kind or c.error}")

    candidates = ([c for c in cams if c.index == args.index] if args.index is not None
                  else [c for c in cams if c.opened and c.width > 0])
    if not candidates:
        print("\n没有可用设备。")
        return 1

    chosen: int | None = None
    for c in candidates:
        cap, backend = camera.open_camera(c.index)
        if cap is None:
            print(f"\n设备 {c.index}: 打不开(可能被占用), 跳过。")
            continue
        print(f"\n>>> 正在显示设备 {c.index}  ({c.kind})")
        print("    看到你自己就按 Y / Enter, 不是你按 N 看下一个, Q 退出。")
        t0 = time.time()
        n = 0
        decided = None
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    print("   读帧失败, 跳到下一个。")
                    break
                n += 1
                fps = n / max(1e-6, time.time() - t0)
                overlay(frame, [
                    f"DEVICE {c.index}   {c.width}x{c.height}   {fps:4.1f} fps",
                    "Y / ENTER = this is me",
                    "N = next device      Q = quit",
                ])
                cv2.imshow(WINDOW, frame)
                key = cv2.waitKey(1) & 0xFF
                if key in (ord("y"), ord("Y"), 13, 10) or (
                        args.seconds and time.time() - t0 > args.seconds and False):
                    decided = True
                    break
                if key in (ord("n"), ord("N")):
                    decided = False
                    break
                if key in (ord("q"), 27):
                    decided = None
                    break
        finally:
            cap.release()
            cv2.destroyAllWindows()
        if decided is None:
            print("已取消。")
            return 1
        if decided:
            chosen = c.index
            break

    if chosen is None:
        print("\n没有选中任何设备。红外/OBS 虚拟摄像头都不能用, 请确认真摄像头是否被其它程序占用。")
        return 1

    print(f"\n选中设备 {chosen}。")
    if not args.no_save:
        print(write_camera_index(chosen, cfg.path))
    print("下一步: .\\.venv\\Scripts\\python.exe scripts\\calibrate.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
