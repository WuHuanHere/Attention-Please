"""M1 交互式校准: 按提示摆三个姿势, 自动生成你的个人阈值 calibration.json。

为什么要校准: MediaPipe 只给关键点/矩阵, "低多少度算低头""偏多少度算看手机"
完全取决于你的坐姿、身高、笔记本高度和摄像头角度。用默认值上线, 第一天就会被误报烦死。

默认值只在你拒绝校准时兜底(宁可漏报)。

用法:
  .\\.venv\\Scripts\\python.exe scripts\\calibrate.py
  .\\.venv\\Scripts\\python.exe scripts\\calibrate.py --index 1 --seconds 12
"""
from __future__ import annotations

import argparse
import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please import camera  # noqa: E402
from attention_please.config import PROJECT_ROOT, Calibration, Config  # noqa: E402
from attention_please.perception import FrameAnalyzer  # noqa: E402

# OpenCV 窗口画不了中文(会变问号), 所以窗口里用英文, 控制台里用中文。
STAGES = [
    ("screen", "1/3  Look at the SCREEN normally", "正常看屏幕(你平时学习的样子)", 20.0),
    ("book", "2/3  Look DOWN at your book", "低头看书/写字(手也放上去)", 12.0),
    ("phone", "3/3  Turn head RIGHT to your phone", "转头看右手边的手机", 12.0),
]

POSE_POINTS = [0, 7, 8, 9, 10, 11, 12]  # 鼻/耳/肩, 够看清朝向了


def draw(frame, feats, lines: list[str]):
    import cv2

    if feats.pose_landmarks is not None:
        h, w = frame.shape[:2]
        for idx in POSE_POINTS:
            if idx < len(feats.pose_landmarks):
                lm = feats.pose_landmarks[idx]
                cv2.circle(frame, (int(lm.x * w), int(lm.y * h)), 4, (0, 255, 0), -1)
    y = 30
    for text in lines:
        cv2.putText(frame, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4)
        cv2.putText(frame, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
        y += 30


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=None)
    ap.add_argument("--seconds", type=float, default=None, help="覆盖每个姿势的采集时长")
    ap.add_argument("--out", default=str(PROJECT_ROOT / "calibration.json"))
    args = ap.parse_args()

    import cv2

    cfg = Config.load()
    cams = camera.probe_cameras()
    picked = None
    if args.index is not None:
        picked = next((c for c in cams if c.index == args.index and c.opened), None)
    picked = picked or camera.pick_usable(cams, cfg.general.camera_index)
    if picked is None:
        print("没有可用的彩色摄像头。先跑 scripts\\probe.py list 看设备。")
        return 1
    print(f"摄像头: 设备 [{picked.index}] {picked.width}x{picked.height} {picked.kind}")

    cap, backend = camera.open_camera(picked.index, cfg.general.camera_width,
                                      cfg.general.camera_height)
    if cap is None:
        print(f"设备 {picked.index} 打不开(可能被会议软件占用)。")
        return 1
    real_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    real_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"采集分辨率 {real_w}x{real_h} (配置要求 "
          f"{cfg.general.camera_width}x{cfg.general.camera_height})")
    if real_h < 600:
        print("⚠️ 没拿到 720p —— 实测这个分辨率下人脸检出率会掉到 0%, 先解决它再校准。")

    analyzer = FrameAnalyzer(cfg.models_dir / "pose_landmarker_lite.task",
                             cfg.models_dir / "face_landmarker.task")
    print("MediaPipe 已加载。")

    samples: dict[str, list[tuple[float, float]]] = {}
    blink_rate_baseline = 15.0
    eye_closed_baseline = 0.1
    t0 = time.monotonic()

    def ts_ms() -> int:
        return int((time.monotonic() - t0) * 1000)

    try:
        print("\n按【空格】开始一个姿势的采集, 按 q 放弃退出。\n")
        for key, en, zh, dur in STAGES:
            duration = args.seconds or dur
            print(f"—— {zh} ——")
            print(f"   摆好姿势后按空格, 保持 {duration:.0f} 秒不要动。")
            # 等待空格
            while True:
                ok, frame = cap.read()
                if not ok:
                    print("读帧失败。")
                    return 1
                if frame is None:
                    continue
                feats = analyzer.analyze(frame, ts_ms())
                draw(frame, feats, [en, "SPACE = start    q = abort",
                                    f"face={'Y' if feats.face_present else 'N'} "
                                    f"pose={'Y' if feats.pose_present else 'N'}"])
                cv2.imshow("attention_please - calibrate", frame)
                k = cv2.waitKey(1) & 0xFF
                if k == ord(" "):
                    break
                if k in (ord("q"), 27):
                    print("已放弃, 未写入 calibration.json")
                    return 1

            # 采集
            collected: list[tuple[float, float]] = []
            blinks = 0
            closed_frames = 0
            face_frames = 0
            start = time.monotonic()
            while time.monotonic() - start < duration:
                ok, frame = cap.read()
                if not ok:
                    break
                feats = analyzer.analyze(frame, ts_ms())
                if feats.blink_event:
                    blinks += 1
                if feats.eye_closed_ratio is not None and feats.eye_closed_ratio > 0.3:
                    closed_frames += 1
                if feats.face_present:
                    face_frames += 1
                if feats.yaw is not None and feats.pitch is not None:
                    collected.append((feats.yaw, feats.pitch))
                left = duration - (time.monotonic() - start)
                draw(frame, feats, [en, f"recording... {left:4.1f}s",
                                    f"samples={len(collected)}",
                                    f"yaw={feats.yaw if feats.yaw is None else round(feats.yaw,1)} "
                                    f"pitch={feats.pitch if feats.pitch is None else round(feats.pitch,1)}"])
                cv2.imshow("attention_please - calibrate", frame)
                if (cv2.waitKey(1) & 0xFF) in (ord("q"), 27):
                    print("已放弃, 未写入 calibration.json")
                    return 1

            if len(collected) < 10:
                print(f"   警告: 只采到 {len(collected)} 个样本(脸没被稳定检测到?), "
                      f"这个姿势的阈值会不可靠。")
            samples[key] = collected
            if collected:
                ys = [c[0] for c in collected]
                ps = [c[1] for c in collected]
                print(f"   {len(collected)} 个样本   yaw 均值 {statistics.fmean(ys):6.1f}°  "
                      f"pitch 均值 {statistics.fmean(ps):6.1f}°")
            if key == "screen":
                span = max(1.0, time.monotonic() - start)
                blink_rate_baseline = blinks * 60.0 / span
                eye_closed_baseline = closed_frames / max(1, face_frames)
                print(f"   基线眨眼率 {blink_rate_baseline:.1f} 次/分, "
                      f"闭眼帧占比 {eye_closed_baseline:.3f}")
            time.sleep(0.4)

    finally:
        cap.release()
        cv2.destroyAllWindows()
        analyzer.close_async()

    cal = Calibration.derive(
        samples,
        camera_index=picked.index,
        res_width=real_w,
        res_height=real_h,
        blink_rate_per_min=blink_rate_baseline or 15.0,
        eye_closed_ratio=eye_closed_baseline or 0.1,
    )
    path = cal.save(args.out)

    print("\n=== 校准结果 ===")
    print(f"看屏幕: yaw {cal.screen_yaw.mean:6.1f}° ± {cal.screen_yaw.std:4.1f}   "
          f"pitch {cal.screen_pitch.mean:6.1f}° ± {cal.screen_pitch.std:4.1f}")
    print(f"看书:   yaw {cal.book_yaw.mean:6.1f}° ± {cal.book_yaw.std:4.1f}   "
          f"pitch {cal.book_pitch.mean:6.1f}° ± {cal.book_pitch.std:4.1f}")
    print(f"看手机: yaw {cal.phone_yaw.mean:6.1f}° ± {cal.phone_yaw.std:4.1f}   "
          f"pitch {cal.phone_pitch.mean:6.1f}° ± {cal.phone_pitch.std:4.1f}")
    print(f"符号自学习: yaw_sign={cal.yaw_sign}  pitch_sign={cal.pitch_sign}")
    print(f"阈值: 看手机 yaw > {cal.phone_yaw_deg:.1f}°   低头 pitch > {cal.book_pitch_deg:.1f}°")
    print(f"基线眨眼率 {cal.blink_rate_per_min:.1f} 次/分 -> 发呆判据 < "
          f"{cal.blink_rate_per_min * 0.5:.1f} 次/分")
    print(f"\n已写入: {path}")

    # 合理性检查: 三个姿势必须在角度上分得开, 否则阈值没意义
    problems = []
    if abs(cal.phone_yaw_deg - cal.screen_yaw.mean) < 8:
        problems.append("看手机和看屏幕的 yaw 几乎没差别 -> 摄像头可能拍不到你的头转向, 手机检测会失效")
    if abs(cal.book_pitch_deg - cal.screen_pitch.mean) < 6:
        problems.append("低头和看屏幕的 pitch 几乎没差别 -> 发呆检测会失效")
    if cal.screen_yaw.std > 8 or cal.screen_pitch.std > 8:
        problems.append("看屏幕时头就在大幅晃动 -> 门槛需要放宽, 或坐姿要稳一点")
    for p in problems:
        print(f"⚠️  {p}")
    if not problems:
        print("✅ 三个姿势在角度上分得开, 阈值可用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
