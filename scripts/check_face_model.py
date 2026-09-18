"""人脸模型自检: 用一张公开人像验证 face_landmarker 到底能不能工作。

为什么需要它: 现场画面检不出人脸时, 必须能分清两种完全不同的故障 ——
  (a) 模型/API/选项有问题(换谁都检不出) -> 要改代码或换模型;
  (b) 现场画面不行(脸太小/太偏/太暗) -> 要改物理布置或分辨率。
没有这张对照, 就只能靠猜。

用法:
  .\\.venv\\Scripts\\python.exe scripts\\check_face_model.py
  .\\.venv\\Scripts\\python.exe scripts\\check_face_model.py --image 某张你自己的照片.jpg
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.config import PROJECT_ROOT, Config  # noqa: E402
from attention_please.perception import close_async  # noqa: E402

# MediaPipe 自己的测试素材桶(storage.googleapis.com 在受限网络里也能通)
PORTRAITS = [
    "https://storage.googleapis.com/mediapipe-assets/portrait.jpg",
    "https://storage.googleapis.com/mediapipe-assets/face.jpg",
    "https://raw.githubusercontent.com/opencv/opencv/4.x/samples/data/lena.jpg",
]


def fetch_portrait(dest: pathlib.Path) -> pathlib.Path | None:
    if dest.exists() and dest.stat().st_size > 5000:
        return dest
    for url in PORTRAITS:
        try:
            print(f"  尝试下载 {url}")
            with urllib.request.urlopen(url, timeout=20) as r:
                data = r.read()
            if len(data) < 5000:
                continue
            dest.write_bytes(data)
            print(f"  下载成功 {len(data) / 1024:.0f} KB -> {dest}")
            return dest
        except Exception as exc:  # noqa: BLE001
            print(f"  失败: {type(exc).__name__}: {exc}")
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default=None, help="用你自己的照片代替下载")
    args = ap.parse_args()

    import cv2
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision

    cfg = Config.load()
    face_model = cfg.models_dir / "face_landmarker.task"
    if not face_model.exists():
        print(f"缺少模型: {face_model}")
        return 1

    if args.image:
        img_path = pathlib.Path(args.image)
    else:
        out = PROJECT_ROOT / "probe_out"
        out.mkdir(exist_ok=True)
        img_path = fetch_portrait(out / "portrait.jpg")
    if img_path is None or not img_path.exists():
        print("没有可用图片, 无法自检。")
        return 1

    frame = cv2.imread(str(img_path))
    if frame is None:
        print(f"读不了图片: {img_path}")
        return 1
    print(f"\n测试图: {img_path}  {frame.shape[1]}x{frame.shape[0]}")

    mp_img = mp.Image(image_format=mp.ImageFormat.SRGB,
                      data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    base = mp_python.BaseOptions(model_asset_path=str(face_model))
    results: list[tuple[str, bool]] = []

    def video_mode(label: str, **opts) -> None:
        defaults = dict(num_faces=1, min_face_detection_confidence=0.5,
                        min_face_presence_confidence=0.5, min_tracking_confidence=0.5)
        defaults.update(opts)
        lm = vision.FaceLandmarker.create_from_options(
            vision.FaceLandmarkerOptions(base_options=base,
                                         running_mode=vision.RunningMode.VIDEO, **defaults))
        res = lm.detect_for_video(mp_img, 0)
        ok = bool(res.face_landmarks)
        results.append((label, ok))
        print(f"  {label:38s} {'✅ 检出' if ok else '❌ 未检出'}"
              f"   矩阵={'有' if res.facial_transformation_matrixes else '无'}")
        close_async(lm)

    def image_mode(label: str) -> None:
        lm = vision.FaceLandmarker.create_from_options(
            vision.FaceLandmarkerOptions(base_options=base,
                                         running_mode=vision.RunningMode.IMAGE,
                                         num_faces=1))
        res = lm.detect(mp_img)
        ok = bool(res.face_landmarks)
        results.append((label, ok))
        print(f"  {label:38s} {'✅ 检出' if ok else '❌ 未检出'}")
        close_async(lm)

    print("\n配置对照:")
    video_mode("VIDEO + 我们的选项(含blendshape/矩阵)",
               output_face_blendshapes=True, output_facial_transformation_matrixes=True)
    video_mode("VIDEO + 默认选项")
    video_mode("VIDEO + conf=0.3", min_face_detection_confidence=0.3,
               min_face_presence_confidence=0.3, min_tracking_confidence=0.3)
    image_mode("IMAGE 模式")

    ok_any = any(r[1] for r in results)
    print()
    if ok_any:
        passes = [label for label, ok in results if ok]
        print(f"结论: **模型本身能工作**(通过: {', '.join(passes)})。")
        print("      现场检不出 -> 是画面问题: 脸太小/太偏/太暗, 或人脸根本不在取景范围里。")
    else:
        print("结论: **模型在任何配置下都检不出这张标准人像** —— 问题在模型/版本/API 用法,"
              "不是现场画面。")
        print("      下一步: 换 mediapipe 版本(如 0.10.x)或改用 blaze_face 检测器 + 自算头姿。")
    print()
    # 顺手把关键点画出来, 方便人眼看模型看到了什么
    try:
        lm = vision.FaceLandmarker.create_from_options(
            vision.FaceLandmarkerOptions(base_options=base,
                                         running_mode=vision.RunningMode.IMAGE, num_faces=1))
        res = lm.detect(mp_img)
        if res.face_landmarks:
            h, w = frame.shape[:2]
            for lm_pt in res.face_landmarks[0]:
                cv2.circle(frame, (int(lm_pt.x * w), int(lm_pt.y * h)), 1, (0, 255, 0), -1)
            marked = PROJECT_ROOT / "probe_out" / "face_sanity_marked.jpg"
            cv2.imwrite(str(marked), frame)
            print(f"已把检出的关键点画到: {marked}")
        close_async(lm)
    except Exception as exc:  # noqa: BLE001
        print(f"(标注图生成失败: {exc})")
    return 0 if ok_any else 2


if __name__ == "__main__":
    raise SystemExit(main())
