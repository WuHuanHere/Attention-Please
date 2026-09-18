"""距离/取景调试器: 让你自己看着屏幕, 把座位距离调到"人脸能被检出"为止。

背景(实测): 这个摄像头在 640x480 下 Pose 给出的头部框只有 47x52 像素,
人脸检测器在这个尺寸上 0% 检出; 而它在一张 820x1024 的标准人像上 100% 检出。
结论是**脸在画面里太小** —— 这不是代码能修的, 只能改距离/分辨率。

它做什么:
  - 用 1280x720 采集(比 640x480 多一倍像素);
  - 低分辨率跑 Pose 定位头部 -> 在原分辨率上裁出头部区域 -> 放大到 320 宽 -> 跑人脸检测;
  - 屏幕上实时显示: 头框像素尺寸、肩宽像素、人脸是否检出;
  - 按 R 重新采样, 按 q 退出。

用法:
  .\\.venv\\Scripts\\python.exe scripts\\tune_distance.py
"""
from __future__ import annotations

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please import camera  # noqa: E402
from attention_please.config import Config  # noqa: E402
from attention_please.perception import close_async  # noqa: E402

WINDOW = "attention_please - distance tuner"
REQ_W, REQ_H = 1280, 720


def head_box(pose_landmarks, shape, pad: float = 0.45):
    h, w = shape[:2]
    xs, ys = [], []
    for idx in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10):
        if idx >= len(pose_landmarks):
            continue
        lm = pose_landmarks[idx]
        vis = getattr(lm, "visibility", 1.0)
        if vis is not None and vis < 0.5:
            continue
        xs.append(lm.x * w)
        ys.append(lm.y * h)
    if len(xs) < 3:
        return None
    x1, x2, y1, y2 = min(xs), max(xs), min(ys), max(ys)
    bw, bh = max(1.0, x2 - x1), max(1.0, y2 - y1)
    return (int(max(0, x1 - bw * pad)), int(max(0, y1 - bh * (pad + 0.3))),
            int(min(w, x2 + bw * pad)), int(min(h, y2 + bh * (pad + 0.2))))


def overlay(frame, lines):
    import cv2

    y = 34
    for text, color in lines:
        cv2.putText(frame, text, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 0, 0), 4)
        cv2.putText(frame, text, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.75, color, 2)
        y += 34
    return frame


def main() -> int:
    import cv2
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision

    cfg = Config.load()
    idx = cfg.general.camera_index
    cap, backend = camera.open_camera(idx, REQ_W, REQ_H)
    if cap is None:
        print(f"设备 {idx} 打不开。")
        return 1
    aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"设备 {idx} 后端 {backend}, 请求 {REQ_W}x{REQ_H} -> 实际 {aw}x{ah}")

    pose = vision.PoseLandmarker.create_from_options(
        vision.PoseLandmarkerOptions(
            base_options=mp_python.BaseOptions(
                model_asset_path=str(cfg.models_dir / "pose_landmarker_lite.task")),
            running_mode=vision.RunningMode.VIDEO, num_poses=1))
    face = vision.FaceLandmarker.create_from_options(
        vision.FaceLandmarkerOptions(
            base_options=mp_python.BaseOptions(
                model_asset_path=str(cfg.models_dir / "face_landmarker.task")),
            running_mode=vision.RunningMode.VIDEO, num_faces=1,
            min_face_detection_confidence=0.5, min_face_presence_confidence=0.5,
            min_tracking_confidence=0.5))

    ts = 0
    stats: list[tuple[int, int, bool]] = []
    last_report = 0.0
    print("\n把脸对着摄像头。屏幕上的 FACE 显示 OK 时, 这个距离就可以用。按 q 退出。\n")

    try:
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                print("读帧失败")
                break
            ts += 33
            small = cv2.resize(frame, (640, int(640 * frame.shape[0] / frame.shape[1])))
            mp_img = mp.Image(image_format=mp.ImageFormat.SRGB,
                              data=cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
            pres = pose.detect_for_video(mp_img, ts)

            head_px = shoulder_px = 0
            face_ok = False
            roi = None
            if pres.pose_landmarks:
                lms = pres.pose_landmarks[0]
                roi_small = head_box(lms, small.shape)
                sx, sy = aw / small.shape[1], ah / small.shape[0]
                if roi_small:
                    roi = (int(roi_small[0] * sx), int(roi_small[1] * sy),
                           min(int(roi_small[2] * sx), frame.shape[1]),
                           min(int(roi_small[3] * sy), frame.shape[0]))
                    head_px = roi[3] - roi[1]
                if len(lms) > 12:
                    shoulder_px = int(abs(lms[11].x - lms[12].x) * aw)

            if roi is not None:
                crop = frame[roi[1]:roi[3], roi[0]:roi[2]]
                if crop.size:
                    sc = max(1.0, 320.0 / crop.shape[1])
                    if sc > 1.0:
                        crop = cv2.resize(crop, None, fx=sc, fy=sc,
                                          interpolation=cv2.INTER_CUBIC)
                    fmp = mp.Image(image_format=mp.ImageFormat.SRGB,
                                   data=cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
                    fres = face.detect_for_video(fmp, ts)
                    face_ok = bool(fres.face_landmarks)
                    if face_ok:
                        cv2.imwrite(str(pathlib.Path(__file__).resolve().parents[1]
                                        / "probe_out" / "tuner_ok_crop.jpg"), crop)
                cv2.rectangle(frame, (roi[0], roi[1]), (roi[2], roi[3]),
                              (0, 255, 0) if face_ok else (0, 0, 255), 2)

            if head_px:
                stats.append((head_px, shoulder_px, face_ok))

            hint = ("OK - this distance works" if face_ok else
                    "MOVE CLOSER - face too small")
            color = (0, 255, 0) if face_ok else (0, 165, 255)
            overlay(frame, [
                (hint, color),
                (f"head box: {head_px} px    shoulder: {shoulder_px} px", (255, 255, 255)),
                (f"face detected: {'YES' if face_ok else 'no'}", color),
                ("q = quit", (200, 200, 200)),
            ])
            cv2.imshow(WINDOW, frame)
            if (cv2.waitKey(1) & 0xFF) in (ord("q"), 27):
                break

            now = time.time()
            if now - last_report > 3 and stats:
                hits = sum(1 for s in stats[-45:] if s[2])
                heads = [s[0] for s in stats[-45:]]
                sh = [s[1] for s in stats[-45:] if s[1]]
                print(f"  头框 {sum(heads) / len(heads):5.0f} px | "
                      f"肩宽 {sum(sh) / len(sh) if sh else 0:5.0f} px | "
                      f"人脸检出 {hits / max(1, len(stats[-45:])) * 100:3.0f}%")
                last_report = now
    finally:
        cap.release()
        cv2.destroyAllWindows()
        # 同步 close 在这台机器上要 40 多秒(FaceLandmarker 图排空), 一律异步
        close_async(pose, face)

    if stats:
        heads = [s[0] for s in stats]
        hits = sum(1 for s in stats if s[2])
        print(f"\n本次会话: 头框平均 {sum(heads) / len(heads):.0f} px "
              f"(最小 {min(heads)}, 最大 {max(heads)}), 人脸检出 {hits / len(stats) * 100:.0f}%")
        if hits == 0:
            print("全程没检出人脸 -> 脸确实不在画面里, 或者小到无法检测。")
            print("可以试: 坐近到 50cm 以内 / 把笔记本屏幕稍微朝自己转 / 抬高摄像头对准脸。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
