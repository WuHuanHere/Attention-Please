"""取景检查 + 摄像头选型(不写代码就能跑的第一步)。

做的事:
  1. 枚举摄像头, 逐个抓一帧, 判定彩色/红外, 存预览图;
  2. 选一台(默认自动挑彩色那台)打开实时预览, 让你确认:
     - 画框里能不能看到你的脸和上半身
     - 手机架在右手边, 到底进不进画面
  3. 按 q 或 ESC 退出。

用法:
  .\\.venv\\Scripts\\python.exe scripts\\preview.py            # 自动挑彩色摄像头
  .\\.venv\\Scripts\\python.exe scripts\\preview.py --index 1  # 指定索引
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please import camera  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=None)
    ap.add_argument("--max-index", type=int, default=5)
    ap.add_argument("--seconds", type=float, default=0, help="0 = 一直预览到按键")
    args = ap.parse_args()

    import cv2

    out = pathlib.Path(__file__).resolve().parents[1] / "probe_out"
    print("枚举摄像头 ...")
    cams = camera.probe_cameras(max_index=args.max_index, preview_dir=out)
    for c in cams:
        print(f"  [{c.index}] opened={c.opened} {c.backend} {c.width}x{c.height} "
              f"sat={c.saturation:.1f} {c.kind} {c.error}")

    picked = None
    if args.index is not None:
        picked = next((c for c in cams if c.index == args.index and c.opened), None)
    if picked is None:
        picked = camera.pick_usable(cams)
    if picked is None:
        print("\n没有可用的彩色摄像头。先看 probe_out/cam_*.jpg 确认硬件, 或用 --index 指定。")
        return 1
    print(f"\n使用设备 [{picked.index}] ({picked.kind})。预览窗口: 按 q/ESC 退出。")

    cap, backend = camera.open_camera(picked.index)
    if cap is None:
        print(f"设备 {picked.index} 打不开(可能被别的程序占用)。")
        return 1
    print(f"后端: {backend}")
    t0 = time.time()
    n = 0
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                print("读帧失败, 退出。")
                break
            n += 1
            if n % 15 == 0:
                loop_fps = n / max(1e-6, time.time() - t0)
                cv2.putText(frame, f"{loop_fps:.1f} fps  dev {picked.index}",
                            (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
            cv2.imshow("attention_please - preview (q to quit)", frame)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            if args.seconds and time.time() - t0 > args.seconds:
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()
    print(f"采集 {n} 帧, 平均 {n / max(1e-6, time.time() - t0):.1f} fps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
