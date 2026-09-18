# HANDOFF — attention_please 任务交接文档

> **给下一个 agent**:这份文档是自包含的。读它 + `PROGRESS.md`(事故与实测记录)+ `README.md`(用户手册)
> 就能接手。不要重新做已经做过的调研,不要重复踩下面"雷区清单"里的坑。
>
> 项目根目录:`E:\Users\Wu Fan\OneDrive\Code\attention_please`
> 最后更新:2026-09-18 13:00

---

## 0. 新对话第一件事(照着做,5 分钟)

```powershell
cd 'E:\Users\Wu Fan\OneDrive\Code\attention_please'

# 1) 确认基线:207 项单测全绿(不需要摄像头,约 2 秒)
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .

# 2) 看当前有没有实例在跑、跑的是哪版代码
Get-Process pythonw,python -ErrorAction SilentlyContinue | Select-Object Id,StartTime
Get-ChildItem src\attention_please\*.py | Sort-Object LastWriteTime -Descending | Select-Object -First 3

# 3) 体检日志与数据(会打印错误行、分集/提醒对照、覆盖率)
.\.venv\Scripts\python.exe .\probe_out\analyze_log.py
```

**判断"实例是否含最新代码"的方法**:比较 `pythonw` 进程的 `StartTime` 与
`src\attention_please\*.py` 的 `LastWriteTime`。进程比源码旧 → 需要用户重启托盘。

---

## 1. 这是什么

考研专注监控。笔记本摄像头 + MediaPipe 判断"你是不是分心了",按**信号可信度分级**提醒,
并给出**无法自欺的每日有效专注时长**。

### 两条铁律(所有设计决策都从这两条推出来)

1. **误报最烦** —— 打断心流比漏掉一次分心更伤。所以:
   每个信号都有连续时长门槛 + 迟滞 + 冷却;推断类信号(看手机/发呆)不许升级到最吵的那一档;
   **看不到你的脸时绝不替你下结论**。
2. **漏报必须可见** —— 摄像头被占用、脸不在画面、程序没跑、机器没开机,都要在日报里写清楚
   漏了多久,否则这份专注报告就是安慰剂。

### 用户已确认的产品决策(**不要擅自改**)

| 决策 | 内容 |
|---|---|
| 学习模式 | **混合**:看屏幕(网课/PDF/VSCode)+ 低头写题(纸笔)。**低头 = 纸笔模式,不判窗口** |
| 提醒通道 | 只用**提示音**(不要语音);18:00–24:00 安静时段自动改**只弹窗** |
| 升级策略 | 高可信可升到"循环+置顶弹窗";中/低可信封顶(三声 / 一声) |
| 暂停 | **必须写理由**,无配额;理由进日报 |
| 隐私 | 分心时存"屏幕截图+摄像头画面"**拼接图**到工作区,7 天自动清理,可一键清空 |
| 背单词 | 15:10–15:40 用手机背单词 → 该时段 `allow_phone = true`(只记录不报警) |
| 离开座位 | 90 秒后记录(不提醒);**超过 15 分钟提醒一次**;不算分心 |
| 开机自启 | 已安装(启动文件夹快捷方式) |
| 成本 | 技术调研单次 ≤ 0.5 元,不要过度调研 |

### 用户作息(`config.toml` 的 `[schedule]`)

```
08:30–10:30 高数强化听课     10:40–11:30 数学刷题
13:00–15:00 电路(听课+做题)  15:10–15:40 英语单词(allow_phone)
15:40–16:40 英语真题精读     16:50–17:50 数学/电路做题
17:50–18:30 当日复习整理     20:00–21:00 数学/政治
21:00–21:30 回顾整理+明日计划
```
区间外(休息/午休/晚饭/21:30 后)一律**待机**:不判定、不记录、不报警。

---

## 2. 当前状态

### 功能矩阵

| 模块 | 状态 |
|---|---|
| 摄像头选型(真RGB/红外/OBS虚拟) | ✅ 已定:索引 0;工具 `scripts/pick_camera.py` |
| 交互校准(三姿势 → 阈值 + 符号自学习) | ✅ 已完成,`calibration.json` 已生成 |
| 720p 双分辨率感知(Pose 定位头部 → 裁剪放大 → 人脸) | ✅ 实测人脸检出 99–100% |
| 切窗口信号(screen) | ✅ **已启用** |
| 看手机信号(phone) | ✅ **已启用**(2026-09-18 12:48 热重载);门槛 **15 秒**(9/18 13:08 从 25 调小) |
| 离开太久提醒(away) | ✅ 已实现,**需重启实例才生效** |
| 发呆信号(daze) | ❌ 已实现但**不建议开**(原因见 §9) |
| 分级提醒 + 带证据弹窗 + "判定错了" | ✅ |
| 暂停(必填理由)/ 手动学习 60 分钟 | ✅ |
| 分心截图拼接 + 7 天清理 | ✅ |
| 每日日报 + 自带查看器 + 21:30 自动生成 | ✅ |
| 托盘(5 状态颜色 + 菜单 + 通知) | ✅ 真人验证通过 |
| 开机自启 | ✅ 已安装 |
| 文件日志(托盘版可诊断) | ✅ `data/logs/` |
| 单测 | ✅ **207 项全绿**(7 项 UI 测试默认跳过) |
| 版本控制 | ✅ **已建 git 仓库**(2026-09-18),`main` 分支,58 个文件已提交;`.venv`/模型/`data/`/`captures/`/`calibration.json`/`probe_out/` 已忽略 |

### 代码规模

48 个 .py 文件、6842 行(源码 ~3100 行,测试 ~2500 行,脚本 ~1200 行)。

### 数据快照(截至 2026-09-18 13:00)

- `data/events.sqlite3` 57 KB;`data/reports/` 2 份;`captures/` 16 张;`data/logs/` 2 个
- 9/18 上午:判定 34017 tick(113 分钟)、人脸覆盖率 **93%**、提醒 2 次、**0 次"判定错了"**、
  暂停 17 分钟(理由「鸡蛋煮熟了,吃早饭」)、有效专注 1h48m / 实际监控 1h53m = **96%**

---

## 3. 环境与机器事实(实测,不是猜的)

| 项 | 值 |
|---|---|
| 机器 | MECHREVO 极光Pro GM5AR0O(RPL),单屏(逻辑 1707x960,有 DPI 缩放) |
| Python | 3.11.4 `C:\Users\Wu Fan\AppData\Local\Programs\Python\Python311`(venv 在 `.venv`) |
| 依赖 | mediapipe **1.0.1**、opencv-contrib-python 5.0.0.93、numpy 2.4.6、Pillow 12.3.0、pywin32 312、pystray 0.19.5 |
| 模型 | `models/pose_landmarker_lite.task`(5.5 MiB)、`face_landmarker.task`(3.6 MiB);下好后**完全离线** |
| 视频设备 | **0 = 真 RGB(唯一可用)**、1 = 红外(灰度)、2 = OBS 虚拟摄像头(**静帧占位图**) |
| 摄像头能力 | 720p 只给 **~10 fps**(USB2 未压缩带宽所限);请求 1920x1080 会让 `cap.read()` **永久卡死** |
| 硬件隐患 | WHEA `0x124` 蓝屏,9/13 与 9/17 各一次,**与本程序无关**;程序每 30 秒落盘一次以减小损失 |
| 系统级故障 | PowerShell 的 **schannel TLS 是坏的**(`SEC_E_NO_CREDENTIALS`)→ 下载必须用 Python |

### 关键实测数字(设计都建立在这些数字上)

| 指标 | 数值 | 含义 |
|---|---|---|
| 640x480 整帧喂 face_landmarker | **人脸检出 0%**(Pose 头框仅 47x52 px) | 所以分辨率不能降 |
| 1280x720 + Pose 裁头放大 | **人脸检出 99–100%**(头框中位 280 px,249–316) | 生产管线 |
| 判定循环 | **4.9–5.0 Hz**、单帧延迟中位 58 ms、**CPU ~12% 单核** | 跑一整天无压力 |
| Pose 检出率 | 100%(人在画面里时) | 在座判定的基础 |
| 人脸覆盖率 | 93%(9/18 上午)/ 69%(9/17 晚)/ **低头写题时 12–15%** | **最重要的约束** |
| 模型创建 / 销毁 | 创建 ~0.1 s / **`FaceLandmarker.close()` 42 s** | 退出路径必须异步 |

### 校准结果(`calibration.json`,2026-09-17)

| 姿势 | yaw | pitch |
|---|---|---|
| 看屏幕 | 1.7° ± 0.4 | 6.5° ± 0.7 |
| 低头看书 | −0.4° ± 0.6 | 28.3° ± 0.8 |
| 转头看手机 | 36.3° ± 10.5 | 12.4° ± 2.3 |

阈值:看手机 `yaw > 19.0°`、低头 `pitch > 17.4°`;**符号自学习:`yaw_sign = −1`、`pitch_sign = 1`**
(MediaPipe 原始 yaw 方向是反的 —— 硬编码正负会让"看手机"信号彻底失效)。
基线眨眼率测得 **3.0 次/分**(正常人 15–20)→ **不可靠**,见 §9。

---

## 4. 架构与文件地图

分层原则:**纯逻辑与 IO 严格分离**,所以判定逻辑可以完全用假数据单测。

```
src/attention_please/
  config.py        配置(config.toml)+ 校准(calibration.json)。纯逻辑。
                   关键:Schedule.block_at/planned_seconds/elapsed_planned_seconds、
                   Calibration.derive(符号自学习 + 阈值取两姿势中点)
  wordlist.py      窗口标题 → 专注/分心/未知(白名单优先)。纯逻辑。
  signals.py       ★核心状态机(纯逻辑):Observation → 三个信号 → 分集 → 分级提醒。
                   含 _update_away、_update_signal、suspend、snapshot。
                   抑制类逻辑(冷却/静默/宽限期)全部只抑制"提醒", 不抑制"记账"。
  store.py         SQLite:event(事件流)+ coverage_minute(每分钟覆盖率)。
                   **每线程一份连接**(thread-local)+ WAL + close() 关全部。
  report.py        日报 markdown:比值分母 = 实际监控时长;含"未监控时段"警告。
  capture.py       屏幕+摄像头拼接图(JPEG q70, 宽 960)+ 7 天清理。
  notifier.py      分级提示音(winsound, 三级模式)。
  ui.py            ★单 Tk 线程:带证据弹窗 / 暂停理由输入 / 日报查看器。
                   request 队列进、event 队列出;`root.after` 必须在 finally;
                   控件构造函数不能收元组 padding;`<Destroy>` 复位状态。
  opener.py        用外部程序打开文件(.md 无关联 → 显式找 VS Code/记事本)。
  tray.py          pystray 托盘:5 状态颜色 + 菜单 + Windows 通知。
  camera.py        开流/选型:饱和度 + **帧间动态度**判虚拟摄像头;set_buffer_size。
  perception.py    ★MediaPipe 封装:720p 帧 → 缩到 640 跑 Pose → 头部 ROI →
                   原分辨率裁剪放大到 320 → FaceLandmarker → yaw/pitch/roll + 眨眼 +
                   20 秒稳定性窗口。**close_async() 必须用它退出**。
  foreground.py    前台窗口标题(win32gui,失败返回 None)。
  input_activity.py 全局键鼠空闲时长(GetLastInputInfo,不记录按了什么)。
  logbook.py       文件日志 + install_streams(把 stdout/stderr 接进日志)。
  runtime.py       ★编排(580 行):读帧/策略/派发/记账/每日任务/暂停/单实例锁/热重载。

scripts/
  run.py           试跑入口(带终端输出)
  run_tray.py      日常入口(只有托盘, 供开机自启)
  autostart.py     开机自启 安装/卸载/检查(用 Python+pywin32, 不用 .ps1)
  calibrate.py     交互校准(三姿势)
  pick_camera.py   选摄像头(人眼确认, 逐台按 Y/N)
  tune_distance.py 距离/取景调试器(人脸检不出时用它)
  probe.py         实测工具:list / bench / faces / res / pipe / contend / offline
  check_face_model.py 人脸模型自检(用标准人像验证模型本身好不好)
  download_models.py  下载模型(Python, 因为 PowerShell TLS 坏)
```

### 关键接口约定

- `Observation(ts, at, pose_present, yaw, pitch, yaw_std, pitch_std, blink_rate,
  eye_closed_ratio, idle_seconds, title)` — `ts` 是**单调秒**,`at` 是墙上时间。
- `Policy(judging, allow_phone, quiet, block_name, enabled)` — 由 runtime 按时间表填。
- 动作:`Nudge(signal, level, evidence, sound, at, ts, block_name)` /
  `EpisodeStart` / `EpisodeEnd` / `AwayChange(away, at, ts, duration)`。
- `SignalKind`:SCREEN / PHONE / DAZE / **AWAY**(away 不受 `enabled_signals` 控制,
  由 `away_reminder_seconds` 控制)。

---

## 5. 雷区清单(每条都是真踩过的,别再犯)

### 环境类

1. **pip 必须 `--no-cache-dir`**。默认缓存目录 `E:\Programs\Python\Python\pipcache` 在工作区之外,
   受限环境下 pip 会**挂死**(显示 Downloading 但字节数不动)。诊断:`-vvv` + 看临时目录字节数。
2. **PowerShell 的 TLS 在这台机器上是坏的** → 所有下载用 Python(`urllib`,自带 OpenSSL)。
3. **PowerShell 5.1 读 UTF-8 无 BOM 的中文 .ps1 会解析失败** → 用 pwsh 7 跑,或干脆写成 .py。
4. **本仓库部分文件上,文件编辑工具会报 `ReplaceFileW EIO (Win32 1175)`**
   (`config.toml`、`PROGRESS.md` 出现过, 都是工作区**根目录**下、刚被写过的文件)。
   `1175 = ERROR_UNABLE_TO_REMOVE_REPLACED` —— 编辑工具用 `ReplaceFileW` 做原子替换,
   而"移除被替换的那个文件"这一步失败了。

   **已查证(2026-09-18, 别再重复查)**:
   - **不是我们的程序占用**:`config.toml` 只在监控热重载时被读 ~1ms/60s;
   - **不是持久锁**:改名探针 40/40 次成功(改名需要的 DELETE 权限正是 ReplaceFile 需要的那一步),
     连"刚写完立刻改名"也 40/40 成功;
   - **不是 OneDrive 专有**:`[IO.File]::Replace` 在 %TEMP%(OneDrive 之外)一样被拒 ——
     因为**沙箱本身对子进程拦了这个 API**,所以从 pwsh 里根本复现不到编辑工具那个错误;
   - OneDrive 确实在跑(该目录位于含同步根标记 `.849C9593-…` 的 OneDrive 树下), 但其日志里
     **没有出现过本项目**;Defender 状态查询被拒(WinDefend 服务查不到)。两者都**未能证实**。

   **结论**:是环境/工具链层面的原子替换被拦, **与项目代码无关**, 且是偶发的
   (同一个文件隔一会儿重试往往就成功)。**别再花时间追根因。**

   **绕法**(已验证可靠):
   `Get-Content -Raw` → `-replace` → `Set-Content` 到 `xxx.new` → `Move-Item -Force`。
   它走 `MoveFileEx(REPLACE_EXISTING)`:把新文件**改名盖上去**, 不需要"删除"目标文件,
   所以不受这个限制。等价的 Python 写法是 `os.replace()`。
   另外:harness 要求**先 read 再 edit**(否则报 `file has not been read`),而 read 状态会
   在某些操作后失效 —— 报错就重新 read 一次,别怀疑文件坏了。
5. **`pythonw` 下 stdout 是"存在但没人看"**(不报错,内容消失)。所以必须有文件日志
   (`logbook.install_streams`),否则托盘版出事现场一片空白。

### MediaPipe / OpenCV 类

6. **`FaceLandmarker.close()` 要 42 秒**(创建只要 0.1 秒)→ 退出路径一律 `close_async()`
   (daemon 线程),否则"关程序"变成"卡死 40 秒"。
7. **别在 OpenCV 里写属性魔数**:`cap.set(4, 1)` 的 4 是 `CAP_PROP_FRAME_HEIGHT`,
   等于把画面高度设成 1 像素,采集速率从 5 Hz 掉到 2 Hz。用 `cv2.CAP_PROP_BUFFERSIZE`。
8. **640x480 下人脸检出率是 0%** → 必须 720p + Pose 裁头放大;`perception.py` 已固化这条管线。
9. **请求不支持的采集分辨率(如 1920x1080)会让 `cap.read()` 永久卡死** → 只用实测过的组合。
10. **MediaPipe 的 `face_blendshapes[0]` 形状跨版本不同**(1.x 是 `list[Category]`,
    旧版是带 `.categories` 的对象)→ 用 `perception.blendshape_categories()`。

### 线程 / 生命周期类

11. **sqlite3 连接不能跨线程**:托盘版里 Runtime 在主线程创建、监控循环在工作线程跑
    → 每帧抛 `ProgrammingError`,**整条监控链路静默瘫痪而托盘菜单看起来正常**。
    现在:thread-local 连接 + `check_same_thread=False` + WAL + `close()` 关全部。
12. **Tk 的 `root.after()` 必须放在 `finally`**:否则回调里任何一次异常都会让轮询链
    **永久断掉**(mainloop 还活着,但再没人处理请求)。
13. **Tk 控件构造函数的 `padx/pady` 只接受整数**;`(0, 10)` 是 `pack()` 的选项,
    写进构造函数会在窗口**画出来之后**抛 `TclError` → 表现为"第一次能用,之后全死"。
    有静态测试 `tests/test_ui_static.py` 守着。
14. **点右上角 X 关窗时 Tk 直接销毁 widget**,必须用 `<Destroy>` 复位内部状态。

### 判定逻辑类(最贵的三条)

15. **抑制类逻辑只能抑制"提醒",绝不能抑制"记账"**。曾经的写法是
    `if obs.ts < self.resume_until: return actions`(坐下宽限期),而 `resume_until` 会被
    **每一个"检测到人"的 tick** 推成 `now+20s` → Pose 一抖动就把整个信号判定饿死:
    不升级、不提醒、连"回到座位"都不判,分集还会长到 209 秒。
16. **冷却的锚点必须是"上一次真的响过的时刻"**,不是"上一次分集结束" ——
    否则一集接一集地切窗口会把冷却无限顺延(实测出现过连续 109 秒、18 分钟一声不吭)。
    另加硬底线:`max_silent_episode_seconds = 90`(连续分心超过 90 秒必出声)。
17. **拿不到头姿(`pitch is None`)不能当成"在看屏幕"**。旧逻辑
    `looking = obs.pitch is None or obs.pitch < 阈值` 让"低头写题 + 把 QQ/B站晾在前台"
    一路误报。现在:窗口信号要求"**能看到脸 + 头没低下**"。
18. **新信号枚举值不能与 `snapshot()` 的布尔键同名**:`SignalKind.AWAY.value == "away"`
    会把布尔量覆盖成字典(字典恒为真)→ 判定中时托盘永远显示蓝色。布尔键叫 `is_away`。
19. **离座时长不能跨"不判定"的时段**:否则"11:20 离开 → 午休 → 13:00 回来"会被记成
    1 小时 40 分,而午休本不在监控时间里,日报会把它再从有效专注里扣一次。
    `suspend()` 现在会把离开收口。
20. **AI 生成的中文里不要用嵌套双引号** —— 本会话多次因此产生 `SyntaxError`
    (f-string 里套 `"..."`)。用「」代替。

---

### 已知偶发(不是 bug, 别慌)

- **单测偶尔报 `FAILED (errors=12, skipped=7)` 而没有任何 `FAIL`**:2026-09-18 遇到一次,
  随后**连跑 11 次(含 4 路 I/O 负载)全绿**。判断是临时目录/线程 join 的时序竞争。
  **处理**:先重跑;若复现, 先把完整 traceback 存下来再动手(别直接改测试)。
- **编辑工具偶发 `ReplaceFileW EIO`**:见 §5 第 4 条(已查证, 有绕法)。
## 6. 已修 bug 全表(现象 / 根因 / 回归测试)

| # | 现象 | 根因 | 回归测试 |
|---|---|---|---|
| 1 | pip 装不上、像卡死 | 缓存目录在工作区外 | (环境,无测试) |
| 2 | 下载全失败 | PowerShell schannel 坏 | — |
| 3 | 选错摄像头(选了 OBS) | 饱和度判据被骗 | `test_tray`/人眼确认流程 |
| 4 | 采集只有 2 Hz | `cap.set(4,1)` = FRAME_HEIGHT=1 | — |
| 5 | 人脸检出 0% | 640x480 脸太小 | `test_perception` |
| 6 | 关程序像卡死 42 秒 | `close()` 慢 | `test_teardown`(5 项) |
| 7 | 托盘菜单点了没反应 | sqlite 跨线程 → 监控链静默瘫痪 | `test_threading`(7 项) |
| 8 | 暂停后图标不变黄 | 刷新循环的 `icon.visible` 退出条件写错 | `test_tray` |
| 9 | 日报打不开/暂停框弹不出 | 控件构造函数收元组 `pady` → TclError 杀死轮询链 | `test_ui_lifecycle`(7 项, 需 `AP_UI_TESTS=1`)、`test_ui_static` |
| 10 | 打开日报"没反应" | `.md` 无文件关联 + 后台 GUI 抢不到前台 | `test_tray`(viewer 探测) |
| 11 | **该提醒却不提醒**(18 分钟) | 宽限期被 Pose 抖动反复重置 → 判定被饿死 | `test_pose_flicker`(8 项) |
| 12 | 109 秒完全静默 | 冷却锚点挂在"上次分集结束" | `test_cooldown`(5 项) |
| 13 | **低头写题被误判成看娱乐窗口** | `pitch is None` 被当成"在看屏幕" | `test_screen_evidence`(9 项) |
| 14 | 日报 19% 像摸鱼 | 比值分母用了"计划时长" | `test_report_coverage`(6 项) |
| 15 | 离开时长跨午休 | `suspend()` 没收口离开 | `test_away`(20 项) |
| 16 | 判定中托盘永远蓝色 | `snapshot()` 键名冲突 | `test_away` |

---

## 7. 数据与可观测性

### SQLite(`data/events.sqlite3`)

```sql
event(id, ts, at, day, kind, signal, level, duration, evidence, block, detail)
coverage_minute(day, minute, ticks, pose_hits, face_hits, judging, PRIMARY KEY(day,minute))
```

`kind` 取值:`episode_start` / `episode_end` / `nudge` / `alert_dismissed` / `wrong` /
`capture` / `pause_start` / `pause_end` / `away_start` / `away_end` /
`camera_busy` / `camera_busy_end` / `camera_read_fail` / `report`。

**重要**:`signal = 'away'` 的 `episode_end` / `nudge` **不计入分心**与分心提醒次数
(已算进 away_seconds),`store.day_stats()` 里做了排除。

### 文件日志(`data/logs/YYYY-MM-DD.log`)

每行 `HH:MM:SS LEVEL msg`。关键行:

- `派发: EpisodeStart ...; Nudge screen level=1 「...」` — **状态机决定了什么**(定位"该提醒没提醒"的第一现场)
- `[HH:MM:SS] 判定中·<块名> | 5.0 Hz | 人脸 93% | screen:分集中65s/L0 静默剩 40s` — 每分钟状态机快照
- `ERR` + traceback — 异常
- `配置已热重载(启用信号: [...])` — 热重载确认

### 日报(`data/reports/YYYY-MM-DD.md`)

固定四行(有效专注 / 实际监控 / 计划 / 分心 / 暂停 / 离开 / 看不清)+ 最常分心时段 +
分心前窗口 top5 + 暂停理由 + 数据可信度(人脸覆盖率、摄像头占用漏检)+ 未监控时段警告。

### 诊断脚本(都在 `probe_out/`, 不进版本库但**很有用**)

| 脚本 | 用途 |
|---|---|
| `analyze_log.py` | 一键体检:错误行、派发记录、状态机快照、分集/提醒对照、低覆盖率分钟 |
| `replay_real.py` | 用数据库真实分集时间戳回放状态机(定位"该提醒没提醒") |
| `verify_fix.py` | 修复前后逐分集对比"提醒数"(证明修复有效) |
| `phone_sim.py` | 用真实校准值模拟看手机/低头/右偏各场景是否触发 |
| `false_positive_estimate.py` | 分集时间 × 每分钟覆盖率,估算哪些提醒可能是误报 |
| `replay_escalation.py` | 抖动/间歇信号下的升级行为实验 |

---

## 8. 操作手册

```powershell
# 跑(试跑, 带终端输出)
.\.venv\Scripts\python.exe scripts\run.py

# 跑(日常, 只有托盘)
.\.venv\Scripts\pythonw.exe scripts\run_tray.py

# 单测(不需要摄像头)
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .

# 真开窗口的 UI 生命周期测试(会弹几个深色窗口, 7 项)
$env:AP_UI_TESTS='1'; .\.venv\Scripts\python.exe -m unittest tests.test_ui_lifecycle -v

# 校准 / 选摄像头 / 距离调试
.\.venv\Scripts\python.exe scripts\calibrate.py
.\.venv\Scripts\python.exe scripts\pick_camera.py
.\.venv\Scripts\python.exe scripts\tune_distance.py

# 实测工具
.\.venv\Scripts\python.exe scripts\probe.py list        # 枚举设备 + 预览图 + 彩色/红外/静帧判定
.\.venv\Scripts\python.exe scripts\probe.py pipe 0 30   # 生产管线 fps/CPU/检出率(验收用)
.\.venv\Scripts\python.exe scripts\probe.py faces 0 30  # 人脸检测诊断(整帧 vs 裁剪放大)
.\.venv\Scripts\python.exe scripts\probe.py res 0 30    # 分辨率对照(注意 1080p 会卡死)

# 开机自启
.\.venv\Scripts\python.exe scripts\autostart.py status|install|uninstall
```

**运行期约定**:单实例锁 = 本地端口 **47731**;配置**每 60 秒热重载**(改完保存即可,
但**新增的配置键需要新代码**才能读到 → 涉及新键时必须重启);摄像头被占用时每 30 秒重试并记
`camera_busy`;长时间不在判定区间(>120 秒)会释放摄像头。

---

## 9. 待办:下一步该做什么

### M4(最高优先级):让"发呆"可用,或按预案砍掉

**问题**:发呆信号要求"低头姿态 + 头几乎不动 + 眨眼率下降",而
**低头写题时人脸覆盖率只有 12–15%** —— 它依赖人脸关键点,在最需要它的场景下天然不可用。
另外基线眨眼率测得 3.0 次/分(正常 15–20),说明眨眼检测欠计数,这个判据不可靠。

**已确认的路线(用户已同意)**:
1. **人脸丢失时改用 Pose 的头部关键点(0–10 号:鼻/眼/耳/嘴)做低精度头姿** ——
   Pose 检出率实测 100%,所以"低头/右偏/长时间不动"在看不到脸时仍可粗判。
   **但只能作为"只记录不报警"的弱证据**(误报最烦),报警仍只在人脸可见时进行。
   ⚠️ 需要先验证 Pose 头部关键点的噪声:实测在部分出画时**肩宽会在 5–500 px 之间跳**,
   所以要设计成"多帧中位数 + 死区"而不是直接用瞬时值。
2. **眨眼基线改成自适应**:用会话内滚动中位数(比如最近 30 分钟)而不是校准那一分钟的样本;
   或者干脆把眨眼降级为"辅助证据",发呆只靠"头长时间不动"。
3. **预案**:M4 试跑 3 天后如果发呆信号误报仍然高,**直接砍掉发呆检测**,只留 screen + phone。

### 交接时"未验证"清单(**下一轮要补的实测**)

| 项 | 现状 | 怎么验 |
|---|---|---|
| **离座 15 分钟提醒** | 单测 + 状态机模拟通过,**但从未在真实机器上响过**(实例不含该代码) | 重启托盘后, 离开座位 15 分钟以上, 听是否响一声 + 回来看见常驻弹窗 |
| 托盘"蓝色离开"状态 | 单测通过, 未真人看过 | 离开座位 90 秒后看托盘图标是否变蓝 |
| 日报新口径 | 已用 9/18 真实数据生成过, 未看过自动生成的版本 | 重启后等 21:30 自动生成, 看比值分母是不是"实际监控时长" |
| 看手机信号的真实误报 | 9/18 中午刚启用, 数据还在攒 | 看 `event` 表 `signal='phone'` 的分集; 对照弹窗里的 yaw 证据 |
| 开机自启 | 快捷方式已装, 但**没经历过一次真实登录** | 下次登录后确认托盘自动出现 |



- **观察 phone 信号的真实误报**(9/18 中午刚开):看有没有"向右拿东西/看笔记 >15 秒"被误报;
  太敏感就调 `phone_continuous_seconds`(现在 15, 往上调回 20-25)。数据在 `event` 表 `signal='phone'`。
- **离座提醒需要重启实例才生效**(代码已就绪)。
- **日报口径修复也需要重启**(否则 21:30 的日报还是旧分母)。
- ~~**`git init`**~~ → **已完成**(2026-09-18):`main` 分支,首次提交 `6016a2c`(57 文件/9719 行)
  + `.gitattributes`(行尾统一 LF)。改代码前建议先开分支,改完 `git diff` 自查。
- 可选:周报、打包 exe、多显示器支持、把"离开提醒通道"做成独立配置(现在跟随安静时段)。

---

## 10. 已知限制与风险

1. **人脸可见性是一切推断类信号的前提**。覆盖率低(低头/靠后/光线差)时 phone/daze 都是瞎的,
   此时只有"窗口标题"信号有效。覆盖率会如实写进日报,不会假装你在专注。
2. **`有效专注` 依赖覆盖率记账**:`focus = 监控时长 − 分心 − 看不清 − 离开`。
   如果覆盖率长期偏低,"看不清"会吃掉大量时间 —— 这是诚实的设计,但用户可能误读为"我没学"。
3. **校准会漂移**:坐姿/笔记本高度一变,阈值就不准 → 建议大改桌面布置后重跑 `calibrate.py`。
4. **机器会蓝屏(0x124 硬件错误)**:程序每 30 秒落盘,但一次蓝屏仍会丢最多 30 秒数据;
   重启后 `coverage_minute` 会出现空洞(日报会体现为覆盖率下降)。
5. **托盘版依赖 `pythonw`**:崩溃时没有控制台,只有 `data/logs/` 里有线索。

---

## 11. 给下一个 agent 的工作约定

1. **先跑测试再改代码**(207 项,2 秒,不需要摄像头)。改完再跑一次。
2. **行为类 bug 先复现再修**:本会话最有价值的三个 bug(饿死/冷却/误报)都是靠
   **"用数据库里的真实时间戳回放状态机"** 定位的,而不是靠读代码猜。
   新 bug 优先写一个 `probe_out/replay_*.py` 复现。
3. **每个修复都必须带回归测试**,并且测试要能**在修复前失败**(否则它没有守住任何东西)。
4. **记账优先于提醒**:任何新增的抑制逻辑(冷却/静默/宽限/暂停)都要问一句
   "它会不会把记账也掐掉?"。
5. **验证要落到可断言的证据上**,不要依赖"用户说看起来正常了" ——
   本会话就吃过一次亏:日报窗口"先画出来、后抛异常",看起来成功,实际已经把 UI 弄死了。
6. **改动 UI 后提醒用户重启托盘**;`config.toml` 的新键也需要重启才能被读到。
7. **汇报要诚实**:误报就是误报,漏报就是漏报,把不确定的地方写清楚(用户明确偏好准确而非安慰)。
8. **成本**:调研单次 ≤ 0.5 元;本机环境限制多(见 §5),优先本地实测而不是网上查。
9. **不要改这些已确认的口径**:误报最烦 > 漏报;低头=纸笔模式不判窗口;看不到脸不猜;
   提示音不要语音;暂停必须写理由;离座 15 分钟提醒一次;安静时段只弹窗。
10. **本机特有环境的绕法**(pip/PowerShell/文件替换)已经写在 §5,**直接照做,不要重新探索**。

---

## 12. 快速索引

| 想了解 | 看 |
|---|---|
| 每个 bug 的完整事故记录与实测数据 | `PROGRESS.md` |
| 用户手册、判定口径表、配置说明 | `README.md` |
| 配置项(全部带注释) | `config.toml` |
| 你的个人阈值 | `calibration.json` |
| 今天/昨天的实际表现 | `data/reports/*.md`、`data/logs/*.log` |
| 分心现场证据 | `captures/*.jpg`(屏幕+摄像头拼接) |
