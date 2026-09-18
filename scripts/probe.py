"""M0 实测脚本: 摄像头选型 / MediaPipe 帧率与 CPU / 离线可用性 / 摄像头占用行为。

用途是在目标机上把"未确认项"一次性闭合, 而不是做产品功能。
所有结论都写进 probe_out/probe-report.md。

摄像头相关的逻辑复用 attention_please.camera, 不在这里重复一份(彩色/红外判据只该有一处)。

用法(在项目根目录):
  .\\.venv\\Scripts\\python.exe scripts\\probe.py list                 # 枚举摄像头 + 存预览图 + 判定彩色/红外
  .\\.venv\\Scripts\\python.exe scripts\\probe.py bench 0 30           # 对设备 0 跑 30 秒, 报 fps/CPU/检出率
  .\\.venv\\Scripts\\python.exe scripts\\probe.py contend 0            # 测试摄像头能否被同时打开两次
  .\\.venv\\Scripts\\python.exe scripts\\probe.py offline 0 10         # 死代理模拟断网, 验证推理不依赖网络
  .\\.venv\\Scripts\\python.exe scripts\\probe.py all 0 30             # 依次跑 list/contend/bench/offline
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from attention_please import camera  # noqa: E402
from attention_please.perception import close_async, head_roi_from_pose  # noqa: E402

OUT = ROOT / "probe_out"
MODELS = ROOT / "models"
POSE_MODEL = MODELS / "pose_landmarker_lite.task"
FACE_MODEL = MODELS / "face_landmarker.task"

MAX_INDEX = 5
REPORT_LINES: list[str] = []


def say(line: str = "") -> None:
    print(line, flush=True)
    REPORT_LINES.append(line)


def save_report(name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text("\n".join(REPORT_LINES) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# 摄像头
# --------------------------------------------------------------------------
def cmd_list() -> None:
    import cv2

    say("## 摄像头枚举\n")
    say(f"cv2 {cv2.__version__}\n")
    cameras = camera.probe_cameras(max_index=MAX_INDEX, preview_dir=OUT)
    say("| index | 打开 | 后端 | 分辨率 | 平均饱和度 | 判定 | 预览图 |")
    say("|---|---|---|---|---|---|---|")
    for c in cameras:
        if not c.opened:
            say(f"| {c.index} | 否 | - | - | - | {c.error} | - |")
        elif c.width == 0:
            say(f"| {c.index} | 是 | {c.backend} | - | - | {c.error} | - |")
        else:
            say(f"| {c.index} | 是 | {c.backend} | {c.width}x{c.height} | {c.saturation:.1f} | "
                f"{c.kind} | `cam_{c.index}.jpg` |")
    usable = [c for c in cameras if c.usable]
    say("")
    if usable:
        say(f"可用彩色设备: {[c.index for c in usable]} -> config.toml 里 camera_index 填 "
            f"**{usable[0].index}**")
    else:
        say("⚠️ 没有检测到彩色摄像头。红外/灰度画面无法做姿态判定, 请先解决硬件。")
    say(f"人眼确认: 打开 `probe_out/cam_*.jpg`, 看哪张是你自己、哪张是黑白红外。\n")


def cmd_contend(index: int) -> None:
    """同一进程内二次打开, 近似回答"摄像头能否被两个程序同时用"。"""
    say("## 摄像头占用行为\n")
    cap1, backend = camera.open_camera(index)
    if cap1 is None:
        say(f"设备 {index} 打不开, 跳过。\n")
        return
    f1 = camera.grab_frame(cap1)
    say(f"- 第一个句柄: 后端 {backend}, 读到帧: {'是' if f1 is not None else '否'}")
    cap2 = None
    try:
        cap2, backend2 = camera.open_camera(index)
        if cap2 is None:
            say("- 第二个句柄: 打不开 → 摄像头是**独占**的(需要让路逻辑)")
        else:
            f2 = camera.grab_frame(cap2)
            say(f"- 第二个句柄: 后端 {backend2}, 读到帧: {'是' if f2 is not None else '否'}")
            if f2 is not None:
                say("- 结论: 同一进程内可重复打开。**但这不等于跟会议软件能共存**, 需人工复测。")
    except Exception as exc:  # noqa: BLE001
        say(f"- 第二个句柄: 异常 {type(exc).__name__}: {exc}")
    finally:
        if cap2 is not None:
            cap2.release()
        cap1.release()
    say('\n**人工复测步骤**: 先启动本程序占住摄像头, 再打开 Windows"相机"App 或腾讯会议, '
        "看谁赢——把结果写回本节。\n")


# --------------------------------------------------------------------------
# MediaPipe
# --------------------------------------------------------------------------
def build_landmarkers(offline: bool = False):
    """返回 (pose, face, notes)。offline=True 时把代理指向死端口。"""
    if offline:
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
            os.environ[var] = "http://127.0.0.1:1"
        os.environ["NO_PROXY"] = ""

    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision

    notes = [f"mediapipe {getattr(mp, '__version__', 'unknown')}"]
    for path in (POSE_MODEL, FACE_MODEL):
        if not path.exists():
            raise SystemExit(f"缺少模型文件: {path} (先跑 scripts/download_models.py)")

    pose = vision.PoseLandmarker.create_from_options(
        vision.PoseLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(POSE_MODEL)),
            running_mode=vision.RunningMode.VIDEO,
            num_poses=1,
            min_pose_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )
    )
    face = vision.FaceLandmarker.create_from_options(
        vision.FaceLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(FACE_MODEL)),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
            output_face_blendshapes=True,
            output_facial_transformation_matrixes=True,
        )
    )
    return pose, face, notes


def cmd_bench(index: int, seconds: int) -> None:
    import cv2
    import mediapipe as mp

    say("## 帧率 / CPU 实测\n")
    pose, face, notes = build_landmarkers()
    say("- " + "; ".join(notes))
    say(f"- 模型: {POSE_MODEL.name}, {FACE_MODEL.name}")

    cap, backend = camera.open_camera(index)
    if cap is None:
        say(f"- 设备 {index} 打不开, 无法实测。\n")
        return
    frame = camera.grab_frame(cap)
    if frame is None:
        say("- 读不到帧, 无法实测。\n")
        cap.release()
        return
    h, w = frame.shape[:2]
    say(f"- 采集: 设备 {index}, 后端 {backend}, 实际 {w}x{h}, "
        f"摄像头自报 fps={cap.get(cv2.CAP_PROP_FPS):.1f}")

    wall0 = time.perf_counter()
    cpu0 = time.process_time()
    frames = pose_hits = face_hits = 0
    matrix_ok = False
    blink_dim = None
    pose_ms: list[float] = []
    face_ms: list[float] = []

    while time.perf_counter() - wall0 < seconds:
        ok, frame = cap.read()
        if not ok:
            break
        frames += 1
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB,
                            data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        ts = int((time.perf_counter() - wall0) * 1000)
        t0 = time.perf_counter()
        pres = pose.detect_for_video(mp_image, ts)
        t1 = time.perf_counter()
        fres = face.detect_for_video(mp_image, ts)
        t2 = time.perf_counter()
        pose_ms.append((t1 - t0) * 1000)
        face_ms.append((t2 - t1) * 1000)
        pose_hits += 1 if pres.pose_landmarks else 0
        face_hits += 1 if fres.face_landmarks else 0
        if fres.facial_transformation_matrixes:
            matrix_ok = True
        if fres.face_blendshapes and blink_dim is None:
            from attention_please.perception import blendshape_categories

            cats = blendshape_categories(fres.face_blendshapes)
            names = [c.category_name for c in cats]
            blink_dim = len(names)
            say(f"- blendshape 维度: {blink_dim}, 含眨眼类别: "
                f"{'eyeBlinkLeft' in names}/{'eyeBlinkRight' in names}")

    wall = time.perf_counter() - wall0
    cpu = time.process_time() - cpu0
    cap.release()

    def pct(vals, q):
        if not vals:
            return 0.0
        s = sorted(vals)
        return s[min(len(s) - 1, int(len(s) * q))]

    say("")
    say(f"- 循环 {frames} 帧 / {wall:.1f} 秒 → **{frames / wall:.1f} fps**(含采集与两次推理)")
    say(f"- 进程 CPU 时间 {cpu:.1f}s / 墙钟 {wall:.1f}s → **CPU 占用约 "
        f"{cpu / wall * 100:.0f}%**(单进程, 多线程可超 100%)")
    say(f"- Pose 推理 中位 {pct(pose_ms, 0.5):.1f} ms / p90 {pct(pose_ms, 0.9):.1f} ms")
    say(f"- Face 推理 中位 {pct(face_ms, 0.5):.1f} ms / p90 {pct(face_ms, 0.9):.1f} ms")
    say(f"- Pose 检出率 {pose_hits / max(frames, 1) * 100:.0f}% / "
        f"Face 检出率 {face_hits / max(frames, 1) * 100:.0f}%")
    say(f"- facial_transformation_matrix 可用: {matrix_ok}")
    if pose_hits == 0:
        say("- ⚠️ Pose 一帧都没检出 → 要么人不在画面里, 要么设备选错了(红外/虚拟摄像头)")
    say("")


def cmd_offline(index: int, seconds: int) -> None:
    say("## 离线可用性(死代理模拟断网)\n")
    try:
        pose, face, _ = build_landmarkers(offline=True)
    except Exception as exc:  # noqa: BLE001
        say(f"- 代理指向死端口后初始化失败: {type(exc).__name__}: {exc}")
        say("- 结论: 初始化阶段有同步网络依赖, 需要进一步定位。\n")
        return
    import cv2
    import mediapipe as mp

    cap, _ = camera.open_camera(index)
    if cap is None:
        say(f"- 设备 {index} 打不开, 只能确认模型初始化不依赖网络。\n")
        return
    frame = camera.grab_frame(cap)
    if frame is not None:
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB,
                            data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        pres = pose.detect_for_video(mp_image, 0)
        face.detect_for_video(mp_image, 0)
        say(f"- 断网状态下推理成功, Pose 检出: {bool(pres.pose_landmarks)}")
        say("- 结论: **模型初始化与推理都不依赖网络**, 首次下载模型后可完全离线运行。")
        say("- 残留风险: MediaPipe 可能异步上报使用指标(不阻塞推理)。要坐实有没有外联:")
        say("  1) 记下本进程 PID; 2) `Get-NetTCPConnection -OwningProcess <PID> -State Established`;")
        say("  3) 或禁用网卡跑 10 分钟看是否有报错。\n")
    cap.release()


def enhance_image(frame):
    """受限光照下的提亮: LAB 的 L 通道做 CLAHE, 再轻微 gamma。"""
    import cv2
    import numpy as np

    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l)
    out = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)
    table = np.array([((i / 255.0) ** 0.8) * 255 for i in range(256)]).astype("uint8")
    return cv2.LUT(out, table)


def cmd_pipe(index: int, seconds: int) -> None:
    """**验收用的实测**: 跑产品真正要用的那条管线(720p + Pose 裁头 + Face)。

    这是 M0 最该看的一张表: 帧率、CPU、Pose 检出率、人脸检出率、头部框像素。
    人脸检出率就是"看手机/发呆"两个信号今天能不能用的直接前提。
    """
    import cv2

    from attention_please.config import Config
    from attention_please.perception import FrameAnalyzer

    cfg = Config.load()
    w, h = cfg.general.camera_width, cfg.general.camera_height
    say("## 生产管线实测(720p + Pose 裁头 + 人脸)\n")
    cap, backend = camera.open_camera(index, w, h)
    if cap is None:
        say(f"- 设备 {index} 打不开。\n")
        return
    aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    say(f"- 采集: 设备 {index}, 后端 {backend}, 请求 {w}x{h} -> 实际 {aw}x{ah}")

    analyzer = FrameAnalyzer(cfg.models_dir / "pose_landmarker_lite.task",
                             cfg.models_dir / "face_landmarker.task")
    say("- 模型加载完成, 开始计时。请保持你**平时学习时的坐姿**。\n")

    wall0 = time.perf_counter()
    cpu0 = time.process_time()
    frames = pose_hits = face_hits = 0
    boxes: list[int] = []
    lat: list[float] = []
    while time.perf_counter() - wall0 < seconds:
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        frames += 1
        t0 = time.perf_counter()
        feats = analyzer.analyze(frame, int((time.perf_counter() - wall0) * 1000))
        lat.append((time.perf_counter() - t0) * 1000)
        pose_hits += 1 if feats.pose_present else 0
        face_hits += 1 if feats.face_present else 0
        if feats.head_box_px:
            boxes.append(feats.head_box_px)
    wall = time.perf_counter() - wall0
    cpu = time.process_time() - cpu0
    cap.release()
    analyzer.close_async()

    def pct(vals, q):
        s = sorted(vals)
        return s[min(len(s) - 1, int(len(s) * q))] if s else 0.0

    say("")
    say(f"- **{frames} 帧 / {wall:.1f} 秒 = {frames / max(wall, 1e-6):.1f} fps**")
    say(f"- **CPU 约 {cpu / wall * 100:.0f}%**(单核口径; 多线程可超 100%)")
    say(f"- 单帧延迟 中位 {pct(lat, 0.5):.0f} ms / p90 {pct(lat, 0.9):.0f} ms")
    say("")
    say("| 指标 | 数值 | 含义 |")
    say("|---|---|---|")
    say(f"| Pose 检出率 | **{pose_hits / max(frames, 1) * 100:.0f}%** | 人在不在画面里 |")
    say(f"| 人脸检出率 | **{face_hits / max(frames, 1) * 100:.0f}%** | 看手机/发呆信号能否工作 |")
    if boxes:
        say(f"| 头部框像素 | 中位 {pct(boxes, 0.5)} px (最小 {min(boxes)}, 最大 {max(boxes)}) | "
            f"小于 200px 时人脸检测会开始漏 |")
    say("")
    face_rate = face_hits / max(frames, 1) * 100
    if face_rate >= 90:
        say("- ✅ 人脸覆盖率达标(≥90%), 三个信号都能工作。")
    elif face_rate >= 50:
        say("- ⚠️ 人脸覆盖率一般。会有一段时间判不出'看手机/发呆', "
            "运行时会把这些时间记为**看不清**并写进日报。")
    else:
        say("- ❌ 人脸覆盖率过低: 坐近一点 / 把笔记本拉近 / 抬高摄像头对准脸, "
            "或改用外接摄像头。")
    say("")
    say("（对照: 640x480 整帧喂人脸模型是 0% 检出, 所以分辨率不能降）\n")


def cmd_faces(index: int, seconds: int) -> None:
    """人脸检测诊断: 找出 Face 检出率为 0 的原因。

    头姿(yaw/pitch)完全依赖 face_landmarker —— 它一帧检不出, "看手机"和"发呆"
    两个信号就全是废的, 所以这一项必须在 M0 里闭合。

    对照实验: 同一批帧, 分别喂 (a) 整帧 (b) 用 Pose 定位后裁剪放大的头部区域。
    如果只有 (b) 能检出, 结论就是"脸在画面里太小", 修法是裁剪切人脸。
    """
    import cv2
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision

    say("## 人脸检测诊断\n")
    cap, backend = camera.open_camera(index)
    if cap is None:
        say(f"- 设备 {index} 打不开。\n")
        return

    pose = vision.PoseLandmarker.create_from_options(
        vision.PoseLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(POSE_MODEL)),
            running_mode=vision.RunningMode.VIDEO,
            num_poses=1,
        )
    )

    frames = []
    crops = []
    lumas = []
    box_sizes = []
    t0 = time.time()
    while time.time() - t0 < 4.0 and len(frames) < 30:
        ok, f = cap.read()
        if not ok or f is None:
            continue
        lumas.append(float(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).mean()))
        mpimg = mp.Image(image_format=mp.ImageFormat.SRGB,
                         data=cv2.cvtColor(f, cv2.COLOR_BGR2RGB))
        pres = pose.detect_for_video(mpimg, int((time.time() - t0) * 1000))
        roi = head_roi_from_pose(pres.pose_landmarks[0] if pres.pose_landmarks else None,
                                 f.shape)
        if roi is None:
            continue
        x1, y1, x2, y2 = roi
        box_sizes.append((x2 - x1, y2 - y1))
        crop = f[y1:y2, x1:x2]
        if crop.size == 0:
            continue
        # 放大到至少 320 宽 —— 小脸放大后检测率会显著提升
        scale = max(1.0, 320.0 / crop.shape[1])
        if scale > 1.0:
            crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        crops.append(crop)
        frames.append(f)
        cv2.imwrite(str(OUT / "face_roi.jpg"), crop)
    cap.release()
    close_async(pose)

    if not frames:
        say("- Pose 一帧都没定位到头部, 无法做对照。先确认人坐在画面里。\n")
        return
    mean_luma = sum(lumas) / len(lumas)
    mw = sum(b[0] for b in box_sizes) / len(box_sizes)
    mh = sum(b[1] for b in box_sizes) / len(box_sizes)
    say(f"- 采样 {len(frames)} 帧, 平均亮度 **{mean_luma:.1f}/255**")
    say(f"- Pose 定位到的头部框: 平均 **{mw:.0f} x {mh:.0f} 像素**(裁剪放大前)")
    say(f"- 裁剪放大后的图已存 `probe_out/face_roi.jpg`, 你可以自己看一眼是不是脸")

    base = mp_python.BaseOptions(model_asset_path=str(FACE_MODEL))

    def run(images, conf: float, enhance: bool) -> tuple[float, float]:
        lm = vision.FaceLandmarker.create_from_options(
            vision.FaceLandmarkerOptions(
                base_options=base,
                running_mode=vision.RunningMode.VIDEO,
                num_faces=1,
                min_face_detection_confidence=conf,
                min_face_presence_confidence=conf,
                min_tracking_confidence=conf,
                output_face_blendshapes=True,
                output_facial_transformation_matrixes=True,
            )
        )
        hits = mats = 0
        for i, img in enumerate(images):
            use = enhance_image(img) if enhance else img
            mpimg = mp.Image(image_format=mp.ImageFormat.SRGB,
                             data=cv2.cvtColor(use, cv2.COLOR_BGR2RGB))
            res = lm.detect_for_video(mpimg, int(i * 100))
            hits += 1 if res.face_landmarks else 0
            mats += 1 if res.facial_transformation_matrixes else 0
        close_async(lm)
        return hits / len(images) * 100, mats / len(images) * 100

    variants = [
        ("整帧 conf=0.5", frames, 0.5, False),
        ("整帧 conf=0.3", frames, 0.3, False),
        ("头部裁剪放大 conf=0.5", crops, 0.5, False),
        ("头部裁剪放大 conf=0.3", crops, 0.3, False),
        ("头部裁剪放大+提亮 conf=0.5", crops, 0.5, True),
    ]
    say("")
    say("| 配置 | 人脸检出率 | 头姿矩阵可用率 |")
    say("|---|---|---|")
    best = None
    for label, imgs, conf, enhance in variants:
        rate, mat_rate = run(imgs, conf, enhance)
        say(f"| {label} | {rate:.0f}% | {mat_rate:.0f}% |")
        if best is None or rate > best[1]:
            best = (label, rate, mat_rate)
    say("")
    say(f"- 最好的一档: **{best[0]}** —— 检出 {best[1]:.0f}%, 头姿矩阵 {best[2]:.0f}%")
    if best[1] < 30:
        say("- ⚠️ 连裁剪放大都检不出 -> 不是尺度问题。剩下两个可能: "
            "(1) 你的脸没在画面里(摄像头被裁切/角度偏); (2) 换一个版本或换模型再验证。")
    elif "整帧" not in best[0]:
        say("- 结论: **脸在整帧里太小**, 必须先用 Pose 定位头部、裁剪放大再做人脸检测。"
            "这正好也是我们要在 perception.py 里做的。")
    say("")
    say(f"（亮度 < 60/255 时 MediaPipe 人脸检测会明显退化, 当前 {mean_luma:.1f}）\n")


def cmd_res(index: int, seconds: int) -> None:
    """分辨率对照: 人脸检测需要足够的像素。

    实测 640x480 下 Pose 给出的头部框只有 47x52 像素, 脸约 40px 高,
    MediaPipe 的人脸检测器在这个尺寸上完全失效(裁剪放大到 320 也只救回 3%)。
    所以要么你坐近点, 要么让摄像头吐更高分辨率 —— 这里测后者能不能拿到。
    """
    import cv2
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision

    say("## 分辨率 / 人脸可检出性对照\n")
    pose = vision.PoseLandmarker.create_from_options(
        vision.PoseLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(POSE_MODEL)),
            running_mode=vision.RunningMode.VIDEO,
            num_poses=1,
        )
    )
    base = mp_python.BaseOptions(model_asset_path=str(FACE_MODEL))
    ts = [0]

    def next_ts() -> int:
        ts[0] += 33
        return ts[0]

    def face_hits(images, conf: float = 0.5) -> float:
        lm = vision.FaceLandmarker.create_from_options(
            vision.FaceLandmarkerOptions(
                base_options=base, running_mode=vision.RunningMode.VIDEO, num_faces=1,
                min_face_detection_confidence=conf,
                min_face_presence_confidence=conf,
                min_tracking_confidence=conf,
                output_facial_transformation_matrixes=True,
            )
        )
        hits = 0
        for img in images:
            mpimg = mp.Image(image_format=mp.ImageFormat.SRGB,
                             data=cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
            hits += 1 if lm.detect_for_video(mpimg, next_ts()).face_landmarks else 0
        close_async(lm)
        return hits / max(1, len(images)) * 100

    variants = [
        ("640x480", 640, 480, None),
        ("1280x720", 1280, 720, None),
        ("1280x720 MJPG", 1280, 720, "MJPG"),
        ("1920x1080 MJPG", 1920, 1080, "MJPG"),
    ]
    say("| 请求 | 实际拿到 | 头部框(像素) | 整帧检出 | 裁剪后检出 | 20帧耗时 |")
    say("|---|---|---|---|---|---|")
    for label, w, h, fourcc in variants:
        cap, backend = camera.open_camera(index, w, h, fourcc)
        if cap is None:
            say(f"| {label} | 打不开 | - | - | - | - |")
            continue
        aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        frames: list = []
        boxes: list[tuple[int, int]] = []
        crops: list = []
        t0 = time.perf_counter()
        tries = 0
        while len(frames) < 20 and tries < 80:
            tries += 1
            ok, f = cap.read()
            if not ok or f is None:
                continue
            small = cv2.resize(f, (640, int(640 * f.shape[0] / f.shape[1])))
            mpimg = mp.Image(image_format=mp.ImageFormat.SRGB,
                             data=cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
            pres = pose.detect_for_video(mpimg, next_ts())
            frames.append(f)
            if not pres.pose_landmarks:
                continue
            roi = head_roi_from_pose(pres.pose_landmarks[0], small.shape)
            if roi is None:
                continue
            sx, sy = aw / small.shape[1], ah / small.shape[0]
            x1, y1, x2, y2 = (int(roi[0] * sx), int(roi[1] * sy),
                              int(roi[2] * sx), int(roi[3] * sy))
            y2 = min(y2, f.shape[0])
            x2 = min(x2, f.shape[1])
            boxes.append((x2 - x1, y2 - y1))
            crop = f[y1:y2, x1:x2]
            if crop.size:
                sc = max(1.0, 320.0 / crop.shape[1])
                if sc > 1.0:
                    crop = cv2.resize(crop, None, fx=sc, fy=sc,
                                      interpolation=cv2.INTER_CUBIC)
                crops.append(crop)
        elapsed = time.perf_counter() - t0
        cap.release()
        box_txt = (f"{sum(b[0] for b in boxes) / len(boxes):.0f}x"
                   f"{sum(b[1] for b in boxes) / len(boxes):.0f}") if boxes else "-"
        full_rate = face_hits(frames)
        crop_rate = face_hits(crops) if crops else -1.0
        crop_txt = f"{crop_rate:.0f}%" if crop_rate >= 0 else "-"
        say(f"| {label} | {aw}x{ah} | {box_txt} | {full_rate:.0f}% | {crop_txt} | "
            f"{elapsed:.1f}s ({len(frames) / max(elapsed, 1e-6):.1f} fps) |")
    close_async(pose)
    say("")
    say("读法: **裁剪后检出**才是我们真正能用的指标(产品里就是用 Pose 先框头再检人脸)。")
    say("若高分辨率下这个数字明显上升, 就把 perception.py 改成'低分辨率跑 Pose、"
        "原分辨率裁脸跑 Face'的双分辨率管线。\n")


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "all"
    index = int(argv[2]) if len(argv) > 2 else 0
    seconds = int(argv[3]) if len(argv) > 3 else 30

    say("# M0 实测报告")
    say(f"\n- 时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    say(f"- Python: {sys.version.split()[0]} ({sys.executable})")
    say(f"- 设备索引: {index}, 时长: {seconds}s\n")

    if cmd in ("list", "all"):
        cmd_list()
    if cmd in ("contend", "all"):
        cmd_contend(index)
    if cmd in ("bench", "all"):
        cmd_bench(index, seconds)
    if cmd in ("offline", "all"):
        cmd_offline(index, seconds)
    if cmd == "faces":
        cmd_faces(index, seconds)
    if cmd == "res":
        cmd_res(index, seconds)
    if cmd == "pipe":
        cmd_pipe(index, seconds)

    save_report("probe-report.md")
    say(f"\n报告已写入: {OUT / 'probe-report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
