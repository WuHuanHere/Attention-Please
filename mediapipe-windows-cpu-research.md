# MediaPipe / 摄像头实时监测 —— Windows 笔记本 CPU 事实清单

**目标环境（用户给定）**：Windows 笔记本，Python 3.11.4（`%LOCALAPPDATA%\Programs\Python\Python311`），将新建 venv，无独立显卡（CPU 推理），内置摄像头做实时监测。

**取证方式与预算说明（先声明）**
- 网页搜索：2 次调用（共 4 条 query）。搜索工具本次只返回"来源 URL 列表"，**不返回正文摘要**，因此搜索本身无法直接佐证具体数字。
- 网页抓取：约 10 次取得内容；另有约 11 次失败（`ai.google.dev`、`developers.google.com`、`raw.githubusercontent.com` 在沙箱内 **DNS 不可达**；`docs.opencv.org` 返回 **403 Cloudflare**；`google.github.io/mediapipe/...` 返回 **404**）。因官方域不可达，改用官方 GitHub API（`api.github.com`）+ 官方模型桶（`storage.googleapis.com`）等一等来源。
- 抓取次数**超出**了 4 搜索 + 3 抓取的上限（失败尝试不产生数据，但确实发出了请求）。下文每条事实都标注了来源；**凡本次未取得来源的，一律写"未确认"，不做推测**。

**结论速览**

| # | 问题 | 结论 |
|---|---|---|
| 1 | PyPI 版本 / Python 支持 | 最新 **1.0.1**（2026-08-14）；分类器声明 **3.9–3.12**，**3.11 支持**；3.13 **官方未声明**；`requires_python` 为空 |
| 2 | Windows wheel / 体积 | **有现成 wheel，无需编译**；1.0.1 `win_amd64` = **20.1 MB**；历史 0.10.x `cp311 win_amd64` ≈ **48.5 MiB** |
| 3 | Tasks vs legacy | legacy solutions **2023-03-01 起停止支持**（仍以 as-is 提供）；Tasks API 需**自备 .task 文件**（3.6 MB / 5.5 MB 起） |
| 4 | 三信号选型 | 在座+手腕 → Pose Landmarker；头部姿态 → Face Landmarker；Face Detector 只适合当"有没有脸"的廉价闸门。**欧拉角需自行从矩阵/关键点推算** |
| 5 | CPU 帧率 / 占用 | **未确认**（本次预算内未取得官方或可信第三方数字） |
| 6 | 离线 / 隐私 | **可完全离线**（模型是本地文件）；输入数据不上传 Google，**但会向 Google 上报性能/使用指标** |
| 7 | 摄像头占用 | **未确认**（OpenCV 官方文档被 403 拦截，未取得一手来源） |
| 8 | 替代方案 | **未确认**（性能对比无来源；仅能给出需核实的依赖/路径清单） |

---

## 1. MediaPipe Python 包版本与 Python 版本支持

**结论：** 最新版 **1.0.1（2026-08-14）**；分类器声明 Python **3.9 / 3.10 / 3.11 / 3.12**，故 **3.11 支持**、**3.12 支持**；**3.13 未被官方声明**（且 3.13 是否实际可用本次**未确认**）。

| 事实 | 值 | 来源 |
|---|---|---|
| 最新版本 | `1.0.1`，Released **Aug 14, 2026** | <https://pypi.org/project/mediapipe/> |
| 该版本分类器 | `Programming Language :: Python :: 3.9 / 3.10 / 3.11 / 3.12`（**无 3.13**） | <https://pypi.org/pypi/mediapipe/json> |
| `requires_python` | `null`（未声明上/下界） | <https://pypi.org/pypi/mediapipe/json> |
| 1.0.1 依赖 (`requires_dist`) | `absl-py~=2.3`, `certifi`, `numpy`, `sounddevice~=0.5`, `flatbuffers~=25.9`, **`opencv-contrib-python`**, `matplotlib` | <https://pypi.org/pypi/mediapipe/json> |
| 1.0.1 wheel 标签 | `py3-none-win_amd64` / `py3-none-win_arm64` / `py3-none-manylinux...` / `py3-none-macosx...`——**tag 不绑定 CPython ABI**（0.10.x 则是 `cp310/cp311/cp312`） | <https://pypi.org/project/mediapipe/#files> |
| 无 sdist | "No source distribution files available for this release" → 只能装 wheel | <https://pypi.org/project/mediapipe/> |

**近期版本时间线（官方 release history）**

| 版本 | 发布日期 | 备注 |
|---|---|---|
| 1.0.1 | 2026-08-14 | 当前最新，5 个 wheel |
| 1.0.0 | 2026-07-27 | 5 个 wheel（1.x 分界点） |
| 0.10.35 | 2026-04-27 | 0.10.x 线收官附近 |
| 0.10.33 / 0.10.32 / 0.10.31 / 0.10.30 | 2026-03-18 / 2026-01-22 / 2025-12-18 / 2025-12-16 | 3 个 wheel/版 |
| 0.10.21 | 2025-02-06 | 16 个 wheel（2025 年主力版本） |
| 0.10.20 | 2024-12-13 | 16 个 wheel |
| 0.10.18 | 2024-11-01 | 20 个 wheel；**有 cp311 / cp312 win_amd64** |
| 0.10.15 | 2024-08-29 | 16 个 wheel；**有 cp312** |
| 0.10.10 | 2024-02-22 | 最早出现 cp310/cp311/cp312 的一批 |

来源（release history）：<https://pypi.org/project/mediapipe/#history>

**对 3.13 的精确表述（避免误读）**
- 官方 **只声明到 3.12**；分类器里没有 3.13 → 官方支持 = 未声明。
- 但 1.0.x 的 wheel 是 `py3-none-*`（非 `cp313`），且 `requires_python` 为空 → **pip 在 3.13 上大概率不会因 wheel 标签被拒**，装得进去是否能正常 import/推理，**本次未确认**（无官方声明，不建议押注）。
- 0.10.x 这类 `cp3xx` wheel 在 3.13 上会直接 "no matching distribution"（推断，依据 wheel 命名规则，未逐版验证）。

---

## 2. Windows 上的 wheel 与下载体积

**结论：** Windows 有**预编译 wheel，`pip install mediapipe` 不需要编译**；当前 1.0.1 的 `win_amd64` wheel 为 **20.1 MB**（另有依赖包体积，见下）。

| 文件 | 平台 | 体积 | 来源 |
|---|---|---|---|
| `mediapipe-1.0.1-py3-none-win_amd64.whl` | Windows x86-64 | **20.1 MB** | <https://pypi.org/project/mediapipe/#files> |
| `mediapipe-1.0.1-py3-none-win_arm64.whl` | Windows ARM64 | 17.7 MB | 同上 |
| `mediapipe-1.0.1-py3-none-manylinux_2_28_x86_64.whl` | Linux | 37.9 MB | 同上 |
| `mediapipe-1.0.1-py3-none-macosx_11_0_arm64.whl` | macOS | 33.7 MB | 同上 |
| `mediapipe-0.10.18-cp311-cp311-win_amd64.whl` | Win x86-64 (Py3.11) | **50,887,647 B ≈ 48.5 MiB** | <https://pypi.org/pypi/mediapipe/json> |
| `mediapipe-0.10.10-cp311-cp311-win_amd64.whl` | Win x86-64 (Py3.11) | 50,707,764 B | 同上 |

**注意（需自行核实体积）**：1.0.1 的 `requires_dist` 含 `opencv-contrib-python`、`matplotlib`、`numpy` 等，**实际磁盘/下载占用远大于 20.1 MB**；这些依赖各自的 wheel 体积本次**未逐一确认**。若要精确预算，应在新建 venv 里实测 `pip download` 的总量。

---

## 3. Tasks API vs legacy `mp.solutions.*`（2024–2025 状态、模型文件来源）

**结论：** **legacy `mp.solutions.*` 是被停止支持的一方**（官方 README 明示 legacy solutions 自 **2023-03-01** 起结束支持，仅按 as-is 继续提供）；**Tasks API（`mediapipe.tasks.python`）是现行主线**。模型文件方面：**Tasks API 必须自备本地 `.task`/`.tflite`**（运行时不会替你联网下载）；legacy solutions 的模型是否随包内置 —— **本次未取得一手来源，未确认（仅有体积旁证，见下）**。

| 子问题 | 结论 | 依据 |
|---|---|---|
| 谁被标 deprecated | **legacy**（`mp.solutions.face_mesh` / `mp.solutions.pose` 属 legacy 组）。原文："We have ended support for these MediaPipe Legacy Solutions as of **March 1, 2023** … prebuilt binaries for all MediaPipe Legacy Solutions will continue to be provided on an as-is basis." | <https://pypi.org/project/mediapipe/>（同时指向 <https://developers.google.com/mediapipe/solutions/guide#legacy>，该域本次不可达） |
| 现行 API | MediaPipe **Tasks**：`mediapipe.tasks.python`（跨平台 API，官方 README 主推） | 同上 |
| Tasks 模型来源 | **不自带、不自下载**：`BaseOptions` 只有 `model_asset_path`（本地文件路径）与 `model_asset_buffer`（内存字节）两个入口 | <https://github.com/google-ai-edge/mediapipe/blob/master/mediapipe/tasks/python/core/base_options.py> |
| Tasks 模型下载地址与体积 | 见下表（直接读官方模型桶的对象列表，**精确字节数**） | 见下表来源 |
| legacy 模型是否随 wheel 内置 | **未确认**。（旁证：0.10.x win wheel ≈48.5 MiB，1.0.1 仅 20.1 MB——体积差异与"模型内置/外置"的假设一致，但**不构成证据**） | 体积来源同第 1/2 节 |

**官方模型桶（`gs://mediapipe-models`）对象列表实测值**

| 模型文件 | 精确字节数 | 约 | 下载 URL |
|---|---|---|---|
| `face_landmarker.task` | **3,758,596** | 3.6 MiB | `https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task` |
| `pose_landmarker_lite.task` | **5,777,746** | 5.5 MiB | `https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task` |
| `pose_landmarker_full.task` | **9,398,198** | 9.0 MiB | `https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/1/pose_landmarker_full.task` |
| `pose_landmarker_heavy.task` | **30,664,242** | 29.2 MiB | `https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_heavy/float16/1/pose_landmarker_heavy.task` |
| `blaze_face_short_range.tflite` | **229,746** | 224 KiB | `https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite` |
| `blaze_face_full_range.tflite` | **1,083,786** | 1.03 MiB | `https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_full_range/float16/1/blaze_face_full_range.tflite` |
| `blaze_face_full_range_sparse.tflite` | **676,746** | 661 KiB | 同上目录（`.../blaze_face_full_range_sparse.tflite`） |

来源：
- face_landmarker：<https://storage.googleapis.com/storage/v1/b/mediapipe-models/o?prefix=face_landmarker/&fields=items(name,size)>
- pose_landmarker：<https://storage.googleapis.com/mediapipe-models?prefix=pose_landmarker/&max-keys=40>
- face_detector：<https://storage.googleapis.com/mediapipe-models?prefix=face_detector/&max-keys=20>

**URL 规则（由上述 listing 归纳）**：`https://storage.googleapis.com/mediapipe-models/<任务>/<模型名>/float16/<版本>/<文件名>`；版本位既可用具体版本 `1`，也有 `latest` 别名（两者 ETag/体积一致）。`blaze_face_full_range` 目录时间为 **2026-03-11**，是较新的模型。

---

## 4. 三个信号该用哪个模型；有没有官方欧拉角

**结论：** 「在座 + 手腕位置」用 **Pose Landmarker**（有归一化关键点 + 世界坐标 + 可选分割掩码）；「头部朝向/俯仰角」用 **Face Landmarker**（官方结果里带 **可选 52 维 blendshapes** 与 **可选 facial transformation matrix**）；**Face Detector 只输出人脸框，适合当廉价"有没有人/有没有脸"的闸门**。**三者都不直接给欧拉角**——需要自己从 4×4 变换矩阵或关键点推算。

| 信号 | 推荐模型 | 官方输出（可核验） | 能否直接拿到头部欧拉角 |
|---|---|---|---|
| 人是否在座位 | **Pose Landmarker**（主）；Face Detector 可做轻量闸门 | `pose_landmarks`（归一化图像坐标）、`pose_world_landmarks`（世界坐标）、可选 `segmentation_masks` | 否 |
| 头部朝向 / 俯仰角 | **Face Landmarker** | `face_landmarks`、可选 `face_blendshapes`、可选 `facial_transformation_matrixes` | **否**，但官方**提供 4×4 变换矩阵**（可直接分解出旋转 → yaw/pitch/roll） |
| 手腕位置 | **Pose Landmarker** | 同上的 `pose_landmarks` / `pose_world_landmarks` | 否 |

一手依据（C++ 结果结构体，字段名逐字）：
- **FaceLandmarkerResult**：
  - `std::vector<...NormalizedLandmarks> face_landmarks;` — "Detected face landmarks in normalized image coordinates."
  - `std::optional<std::vector<...Classifications>> face_blendshapes;` — "Optional face blendshapes results."
  - `std::optional<std::vector<Matrix>> facial_transformation_matrixes;` — "Optional facial transformation matrix."
  来源：<https://github.com/google-ai-edge/mediapipe/blob/master/mediapipe/tasks/cc/vision/face_landmarker/face_landmarker_result.h>
  （Python 侧 `face_landmarker.py` 里也有 `class Blendshapes(enum.IntEnum)`，注释为 "The 52 blendshape coefficients."，共 52 项：来源 <https://github.com/google-ai-edge/mediapipe/blob/master/mediapipe/tasks/python/vision/face_landmarker.py>）
- **PoseLandmarkerResult**：`segmentation_masks`（可选）、`pose_landmarks`、`pose_world_landmarks`。**无任何姿态角字段**。
  来源：<https://github.com/google-ai-edge/mediapipe/blob/master/mediapipe/tasks/cc/vision/pose_landmarker/pose_landmarker_result.h>

**关于"如何从关键点推算"**：本次**未取得官方文档来源**来说明具体算法（Google 文档域不可达）。行业常见做法（**未验证，仅作为待核实项**）是从 face landmarks 做 `solvePnP`、或直接分解 `facial_transformation_matrixes` 的旋转部分。**结论：官方给了矩阵，欧拉角要自己算；具体换算与坐标系定义（左手/右手、矩阵是 row/column-major）必须以官方文档或实测确认，本次未确认。**

**补充事实**：`BaseOptions.delegate` 支持 `CPU=0 / GPU=1 / LITERT=4`，并注明 **"GPU support is currently limited to Ubuntu platforms"** → 在 Windows 笔记本上就是 CPU delegate。
来源：<https://github.com/google-ai-edge/mediapipe/blob/master/mediapipe/tasks/python/core/base_options.py>

---

## 5. CPU 性能预期（640×480 的 FPS / CPU 占用）

**结论：未确认。** 本次预算内**没有取得任何可引用的官方或可信第三方数字**（Google 的文档域与 Studio 基准页在沙箱内不可达；搜索工具本次不返回正文，只返回 URL 列表；返回的第三方结果多为无出处的聚合博客，不足以作为事实来源，故不引用具体帧率/占用数字）。

| 想要的数字 | 本次状态 | 说明 |
|---|---|---|
| Pose Landmarker @640×480 在笔记本 CPU 的 FPS | **未确认** | 无来源 |
| Face Landmarker @640×480 在笔记本 CPU 的 FPS | **未确认** | 无来源 |
| 常见 CPU 占用百分比 | **未确认** | 无来源 |
| 是否官方给出过数字 | **未确认**（官方 Studio 基准页 <https://developers.google.com/mediapipe/solutions/studio> 本次不可达） | 该 URL 由官方 README 列出，可作后续核实入口 |

**可以确定、且与性能决策直接相关的两条事实**
1. Windows 上只能用 **CPU delegate**（GPU delegate 官方注明仅限 Ubuntu）→ 参见第 4 节的 `base_options.py`。
2. 模型体积/档位差距很大（`pose_lite` 5.5 MiB ↔ `pose_heavy` 29.2 MiB）→ 在 CPU 上档位选择比任何配置项都更影响帧率；但**具体 FPS 仍需实测**。

**后续取证建议（不在本次预算内）**：用官方 MediaPipe Studio 的 benchmark 页，或在目标笔记本上自行做 5 分钟实测（不作代码实验，本次按要求未执行）。

---

## 6. 网络依赖：能否完全离线？是否联网/上传？

**结论：** **下好模型后可完全离线推理**——Tasks API 只接受本地文件路径或内存字节，运行时**不存在自动下载模型**的行为。隐私上：**输入数据（图像/视频）不上传 Google**；**但 MediaPipe Tasks API 会把"性能与使用情况"指标发送给 Google**。

| 问题 | 结论 | 依据 |
|---|---|---|
| 运行时会自动下载模型吗 | **不会**。`BaseOptions` 只有 `model_asset_path`（路径）与 `model_asset_buffer`（字节）两种来源 | <https://github.com/google-ai-edge/mediapipe/blob/master/mediapipe/tasks/python/core/base_options.py> |
| 能否完全离线 | **能**（前提：模型文件已落盘；1.0.1 的 Linux wheel 37.9 MB / Win 20.1 MB 已含二进制运行时，pip 装完后无需再联网） | 同上 + <https://pypi.org/project/mediapipe/#files> |
| 输入数据是否上传 | **不上传**。原文："processing of the input data (e.g. images, video, text) takes place **on device**, and MediaPipe does **not** send that input data to Google servers." | <https://pypi.org/project/mediapipe/>（Privacy Notice, Last modified June 5, 2026） |
| 是否有联网行为 | **有**：原文 "MediaPipe Tasks APIs send **metrics about the performance and utilization of the APIs in your app to Google**."，并要求开发者依法取得用户对 Google 处理这些指标的知情同意 | 同上 |
| 指标上报能否关闭（"实时监测"隐私敏感场景的关键点） | **未确认** | 本次未取得来源；`BaseOptions` 字段中未见明显的关闭开关 |

---

## 7. 摄像头被占用：`cv2.VideoCapture(0)` 与腾讯会议/微信视频并存

**结论：未确认。** 一手来源未取得（`docs.opencv.org` 返回 **403 Cloudflare**，`raw.githubusercontent.com` DNS 不可达），因此**不能**在这里断言"能否共享""DSHOW 还是 MSMF 更稳""是否必须 `cv2.CAP_DSHOW`"。

| 待核实项 | 本次状态 |
|---|---|
| Windows 摄像头是否独占（会议软件占用时 OpenCV 是否打不开） | **未确认** |
| 腾讯会议 / 微信视频 与 OpenCV 同时打开的行为 | **未确认** |
| DirectShow (`cv2.CAP_DSHOW`) vs MSMF 稳定性差异 | **未确认** |
| 已知坑：打开慢、首次打开卡死、`read()` 返回全零、必须指定后端 | **未确认**（未取得官方 issue/docs 来源） |

**本次搜索返回的线索 URL（社区来源，均未抓取、未验证，仅作后续核实起点）**
- <https://stackoverflow.com/posts/59379935/revisions> （OpenCV `video.read()` 返回全零的讨论）
- <https://thelinuxcode.com/how-to-install-opencv-for-python-on-windows-and-avoid-the-usual-cv2dll-traps/>
- <https://blog.csdn.net/weixin_30908941/article/details/95621890> （`CAP_DSHOW` 与 USB 摄像头抓帧失败诊断）
- <https://m.jb51.net/python/369731nfn.htm> （Windows 打不开 USB 摄像头）

**可复核的官方入口**：`https://docs.opencv.org/4.x/d4/d15/group__videoio__flags__base.html`（定义 `CAP_DSHOW` / `CAP_MSMF` 等后端枚举）——本次被 403 拦截。

---

## 8. 替代方案（OpenCV DNN + YOLO / Windows Media Foundation Face Detection）

**结论：未确认。** 三者的 CPU 表现与部署难度的对比**没有任何本次取得的一手来源**（`learn.microsoft.com`、`pypi.org/pypi/ultralytics` 本次均未抓取；不做推测）。

| 方案 | CPU 表现 | 部署难度 / 依赖体积 | 本次状态 |
|---|---|---|---|
| MediaPipe Tasks（现状） | 见第 5 节（未确认） | wheel 20.1 MB + 模型 3.6–5.5 MB；Windows 上 CPU delegate | 部分已确认（1/2/3/4/6 节） |
| OpenCV DNN + YOLO（如 yolov8n-pose） | **未确认** | **未确认**（业界普遍通过 `ultralytics` 间接引入 `torch`/`torchvision`，CPU 版体积可达数百 MB——**此句为待核实项，非本次来源**） | 未确认 |
| Windows Media Foundation Face Detection | **未确认** | **未确认**（系统自带、无需下载模型，但需 C++/WinRT 或相应 Python 绑定——**待核实**） | 未确认 |

**若要把这三者补齐，需要的最小取证动作**：抓 `pypi.org/pypi/ultralytics/json`（确认 `requires_dist` 是否含 torch → 决定依赖体积量级），抓 `learn.microsoft.com` 的 Face Detection 文档（确认 API/绑定方式），再补一篇有硬件说明的 CPU 基准（本次无法满足）。

---

## 对笔记本 CPU 场景的推荐结论

1. **技术栈可定：Python 3.11 + `mediapipe`（1.0.x）+ CPU delegate 可行且无需编译**——Windows `win_amd64` 现成 wheel 仅 20.1 MB；**不要用 3.13**（官方只声明到 3.12，本次也未确认 3.13 可用）。
2. **三信号映射**：在座与手腕用 **`pose_landmarker_lite`（5.5 MiB）**；头部姿态用 **`face_landmarker`（3.6 MiB）**，姿态角从官方 `facial_transformation_matrixes` / landmarks 自行推算（三模型都不直接给欧拉角，矩阵的坐标系与换算方式必须先核实）。**避免 `pose_landmarker_heavy`（29.2 MiB）**，并尽量**不要**依赖 legacy `mp.solutions.*`（2023-03-01 起停止支持）。
3. **部署形态**：首次联网把 2–3 个 `.task` 下载到项目内 `models/`，之后 **100% 离线**运行；隐私上图像不出本机，但**要接受"性能指标上报 Google"**这一既成事实（能否关闭未确认），如合规要求严格需另行评估。
4. **尚未闭合、会影响架构的三件事**：CPU 帧率/占用（第 5 节）、与腾讯会议/微信的摄像头共存行为（第 7 节）、以及是否需要降级到 YOLO/WinRT 备选（第 8 节）。建议在目标笔记本上先做**一次短实测**（单进程双模型同帧 vs 交替降频；必要时显式指定 OpenCV 后端）再冻结架构。
5. **信息新鲜度提醒**：PyPI 最新为 **1.0.1（2026-08-14）**，1.x 相对 0.10.x 的依赖已变化（新增 `sounddevice`、已含 `opencv-contrib-python`）；网上多数教程仍针对 0.10.x，注意 API 与安装体积差异。

---

## 附：本次"未确认"清单（需要额外预算或本地实测）

| 编号 | 未确认内容 |
|---|---|
| 1 | Python 3.13/3.14 上 mediapipe 1.0.x 是否真能 import 并推理 |
| 2 | opencv-contrib-python 等依赖的实际下载体积 |
| 3 | legacy `mp.solutions.*` 的模型是否确实内置在 0.10.x wheel 内（仅体积旁证） |
| 4 | 头部欧拉角的官方换算方式与 `facial_transformation_matrixes` 的坐标系/矩阵布局 |
| 5 | Pose/Face Landmarker 在笔记本 CPU @640×480 的 FPS 与 CPU 占用 |
| 6 | MediaPipe 指标上报是否可关闭 |
| 7 | Windows 摄像头独占/共享行为、DSHOW vs MSMF、已知打开坑 |
| 8 | yolov8n-pose / Windows Media Foundation Face Detection 的 CPU 表现与依赖体积 |
