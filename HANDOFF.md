# HANDOFF — attention_please 任务交接文档

> **给下一个 agent**:这份文档是自包含的。读它 + `PROGRESS.md`(事故与实测记录)+ `README.md`(用户手册)
> 就能接手。不要重新做已经做过的调研,不要重复踩下面"雷区清单"里的坑。
>
> 项目根目录:**`E:\Code\attention_please`**(2026-09-18 已从 OneDrive 目录树搬出,
> 原因见 §5 第 4 条;搬移后自启快捷方式已重装指向新路径)
> 最后更新:2026-09-18 18:20

---

## 0. 新对话第一件事(照着做,5 分钟)

```powershell
cd 'E:\Code\attention_please'

# 1) 确认基线:285 项单测全绿(不需要摄像头, 约 3 秒)
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .

# 2) 看当前有没有实例在跑、跑的是哪版代码
Get-Process pythonw,python -ErrorAction SilentlyContinue | Select-Object Id,StartTime
Get-ChildItem src\attention_please\*.py | Sort-Object LastWriteTime -Descending | Select-Object -First 3

# 3) 体检日志与数据(错误行、分集/提醒对照、按信号的分集统计、低覆盖率分钟)
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
   每个信号都有连续时长门槛 + 迟滞 + 冷却;推断类信号(看手机)不许升级到最吵的那一档;
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
| 发呆信号(daze) | ❌ **已于 2026-09-18 砍掉**(用户拍板;三条证据见 §9)。它想解决的价值由 **Pose 记录通道**承担 |
| **Pose 弱证据通道(pose)** | ✅ 代码 + 19 项接线测试已就绪,**只记录不报警**;⚠️ **实例是旧代码, 从未真机跑过**(见 §9) |
| 分级提醒 + 带证据弹窗 + "判定错了" | ✅ |
| 暂停(必填理由)/ 手动学习 60 分钟 | ✅ |
| 分心截图拼接 + 7 天清理 | ✅ |
| 每日日报 + 自带查看器 + 21:30 自动生成 | ✅ |
| **日报「各时段专注情况」(按作息表分时段)** | ✅ 2026-09-25 加, 26 项测试;真实库核对过 9/24、9/25 两天 |
| **日报窗口的 markdown 预览渲染** | ✅ 2026-09-25 加, 28 项测试(23 项纯逻辑 + 5 项真窗口);表格像素级对齐已量化验证 |
| 托盘(5 状态颜色 + 菜单 + 通知) | ✅ 真人验证通过 |
| 开机自启 | ✅ 已安装 |
| 文件日志(托盘版可诊断) | ✅ `data/logs/` |
| 单测 | ✅ **429 项全绿**(15 项 UI 测试默认跳过) |
| 版本控制 | ✅ **已建 git 仓库**(2026-09-18),`main` 分支,58 个文件已提交;`.venv`/模型/`data/`/`captures/`/`calibration.json`/`probe_out/` 已忽略 |

### 代码规模

54 个 .py 文件、10145 行(源码 4322 行 / 19 个,测试 4386 行 / 32 个,脚本 1437 行 / 10 个;
另有 `probe_out/` 62 个文件 3570 行,不进版本库)。**401 项单测**(10 项需 `AP_UI_TESTS=1`)。

### 数据快照(截至 2026-09-18 18:15)

- 9/18 全天(09:09–18:14,监控 6h15m):有效专注 **4h50m**、分心 10.2 分钟(19 段)、
  **看不清 63.9 分钟(占监控 17%)**、离开 10.8 分钟、暂停 46.2 分钟、人脸覆盖率 **80%**、
  提醒 25 次、**0 次「判定错了」**、`camera_busy` 0 次
- `phone` 信号(9/18 中午启用):**6 段 / 2.8 分钟**,yaw 证据 22°–55°(阈值 19°)→ 未见误报
- 9/18 上午:判定 34017 tick(113 分钟)、人脸覆盖率 **93%**、提醒 2 次、有效专注 1h48m = 96%

---

## 3. 环境与机器事实(实测,不是猜的)

| 项 | 值 |
|---|---|
| 机器 | MECHREVO 极光Pro GM5AR0O(RPL),单屏(逻辑 1707x960,有 DPI 缩放) |
| Python | 3.11.4 `%LOCALAPPDATA%\Programs\Python\Python311`(venv 在 `.venv`) |
| 依赖 | mediapipe **1.0.1**、opencv-contrib-python 5.0.0.93、numpy 2.4.6、Pillow 12.3.0、pywin32 312、pystray 0.19.5 |
| 模型 | `models/pose_landmarker_lite.task`(5.5 MiB)、`face_landmarker.task`(3.6 MiB);下好后**完全离线** |
| 视频设备 | **0 = 真 RGB(唯一可用)**、1 = 红外(灰度)、2 = OBS 虚拟摄像头(**静帧占位图**) |
| 摄像头能力 | 720p 只给 **~10 fps**(USB2 未压缩带宽所限);请求 1920x1080 会让 `cap.read()` **永久卡死** |
| 硬件隐患 | WHEA `0x124` 蓝屏,9/13 与 9/17 各一次,**与本程序无关**;程序每 2 秒落盘一次以减小损失 |
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
  baseline.py      ★滚动基线(中位数/MAD/p10/p90 + 可信样本闸门 + 样本不够就弃权)。
                   理由: 固定的校准基线是坏的(眨眼测出 3.0/分、bow 与 pitch 只有 r=-0.66),
                   这类被相机几何污染的量只能做**相对判断**。眨眼基线与 Pose 直立基准共用它。
  pose_head.py     ★Pose 头部关键点 → 低精度头姿(低头/转头/没动)。**只做弱证据, 永不报警**。
                   尺度闸门(肩宽 vs 慢变基准)+ 短窗中位数 + 分位数范围判据 +
                   直立基准只吃"人脸确认头没低"的帧 + 拿不准就弃权。纯逻辑。
  signals.py       ★核心状态机(纯逻辑):Observation → 四个信号 → 分集 → 分级提醒。
                   含 _update_away、_update_signal、suspend、snapshot。
                   抑制类逻辑(冷却/静默/宽限期)全部只抑制"提醒", 不抑制"记账"。
                   SignalKind.POSE 是**只记录**的信号: 上限硬 0 + 结构上强制 record_only。
  store.py         SQLite:event(事件流)+ coverage_minute(每分钟覆盖率)。
                   **每线程一份连接**(thread-local)+ WAL + close() 关全部。
  report.py        日报 markdown:比值分母 = 实际监控时长;含"未监控时段"警告。
  capture.py       屏幕+摄像头拼接图(JPEG q70, 宽 960)+ 7 天清理。
  notifier.py      分级提示音(winsound, 三级模式)。
  ui.py            ★单 Tk 线程:带证据弹窗 / 暂停理由输入 / 日报查看器(预览渲染)。
                   request 队列进、event 队列出;`root.after` 必须在 finally;
                   控件构造函数不能收元组 padding;`<Destroy>` 复位状态。
  mdview.py        日报 markdown → **带样式的预览**(块解析纯逻辑 + Tk 渲染)。
                   表格靠 `Font.measure()` 算出的**像素 tab 停靠位**对齐(见 §5 的 14i)。
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
  eye_closed_ratio, idle_seconds, title, pose_head)` — `ts` 是**单调秒**,`at` 是墙上时间。
  `pose_head` 是 `PoseHead | None`(None = Pose 弃权),只喂给 `SignalKind.POSE`。
- `Policy(judging, allow_phone, quiet, block_name, enabled)` — 由 runtime 按时间表填。
- 动作:`Nudge(signal, level, evidence, sound, at, ts, block_name)` /
  `EpisodeStart` / `EpisodeEnd` / `AwayChange(away, at, ts, duration)`。
- `SignalKind`:SCREEN / PHONE / **AWAY**(away 不受 `enabled_signals` 控制,
  由 `away_reminder_seconds` 控制)/ **POSE**(Pose 弱证据,**只记录不报警**:
  `max_level_for()` 硬返回 0,且 `_update_signal` 结构上强制 `record_only` ——
  两道独立闸门,别去掉任何一道)。

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
4b. **中文 Windows 控制台是 GBK, 而提醒文案里有 emoji(🔔 ⏹ 💤 👋)**。
   `print` 会抛 `UnicodeEncodeError`;而 `dispatch` 的顺序是
   `记账 → print → 响铃 → 弹窗`, print 一抛**响铃和弹窗被整段跳过** ——
   库里记了一笔"提醒", 你却既没听见也没看见(2026-09-18 实测抓到)。
   修法:所有输出走 `logbook.safe_print()`(编不出来的字符降级成 `?`, 绝不抛异常),
   runtime 内用 `self._say()`;回归测试 `tests/test_encoding.py`(7 项)。
   **别再往提醒文案里塞裸 emoji 直接 print。**

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
14b. **sqlite 的写失败必须 `rollback()`,读之前必须了结挂着的写事务**。pysqlite 在执行
    DML 前会**隐式 BEGIN**;那条语句失败时(最常见 `database is locked`)事务**不会自动结束**,
    连接就留在里面了 —— 之后它的每个 SELECT 都钉在失败那一刻的快照上, 写也永远撞同一个锁,
    **而且一声不响**。2026-09-20 实测: 托盘线程 13:37:54 写事件撞锁(因为当时 30 秒才
    commit 一次, 写锁一直攥在监控线程手里)→ 悬停提示轮询读了第一次, 快照冻结 →
    菜单里的日报**永远**显示 13:37 的 17 分钟, 而监控线程自己的进度早就是 0.97 h。
    现在: `Store._write()` 失败必回滚, `Store._fresh_read()` 读前落定事务,
    落盘间隔 30 秒 → 2 秒(`runtime.FLUSH_EVERY_SECONDS`, 必须远小于
    `store.BUSY_TIMEOUT_SECONDS`)。`tests/test_store_lock.py` 守这条, 并做过变异测试。

14c. **`from datetime import time` 会把 `import time` 这个模块整个盖掉**。runtime 里到处是
    `time.monotonic()`, 加一行 `from datetime import time` 之后全线 `AttributeError`
    (单测当场抓住)。要拿"当天零点"用 `datetime.min.time()`。
14d. **`coverage_minute.seconds` 这类新列必须逐行判断可用性, 不能整体判断**。中午重启一次,
    当天就变成"上午的分钟只有 tick 数、下午的分钟有秒数"的混合状态; 整体写
    `SUM(seconds) > 0 ? 用秒数 : 用 tick` 会把上午全算成 0 —— 当天监控时长直接腰斩。
14e. **时间段事件(episode_end / away_end / pause_end)跨午夜必须按天拆开**(`_log_span`)。
    不拆的后果是双向的: 起始日留下一个**假的**"没等到收尾"(日报会谎报"程序崩溃"),
    次日凭空多出时长把有效专注夹成 0。
14f. **`away_start` 的 ts 不是离开区间的起点** —— 它是在"离开够久、决定记一笔"那一刻写的,
    比真实起点晚整整 `away_seconds`(默认 90 秒)。区间的权威定义是
    `away_end.ts - duration`, 拿 `away_start.ts` 去减会少扣 90 秒。
14g. **测试里写死日期 = 定时炸弹**。runtime 给 `wrong` / `ui_error` / `title_shadowed` 打的
    时间戳是**真实的 `datetime.now()`**, 而分集的时间戳来自测试伪造的 `Observation.at` ——
    两边一旦跨日, `day_stats(DAY)` 就只看得见一半事件。实测 2026-09-25: 5 个测试同时变红,
    和代码改动毫无关系。**测试里的 DAY / t0 必须从 `datetime.now()` 派生**。
14h. **"各时段专注"这张表必须逐项可加**。`store.block_stats` 和 `day_stats` 用同一批原始
    数据、同一套口径, 只是按时间切开, 所以每一列(监控/分心/看不清/离开)相加都必须**精确**
    等于全天总数(`tests/test_block_report.py` 里守着)。唯一的例外是 `focus_seconds` 逐段
    按 0 兜底: 某段的离开时长可能超过它自己的监控时长(离开的尾巴跨过时段结尾)。差额够
    1 分钟就会在日报里写一行 ⚠️, **不要把这个警告删掉**。
14i. **中英混排的表格不许用空格对齐**。一个汉字不是一个西文字符宽(还受字体回退影响),
    拿空格凑必然错位。日报预览用 `Font.measure()` 实测每列像素宽, 再把这些宽度设成
    `Text` 的 **tab 停靠位**(`tabs=(...)`, 裸整数 = 像素) —— 这样汉字、全角标点、
    破折号都不会错位, 而且**同一列每个单元格的 x 坐标完全相同**(实测 8 列 11 行全对齐,
    见 `probe_out/verify_preview.py`)。
14j. **Tk 的标签优先级 = 创建顺序, 后创建的赢**。预览渲染里必须先配块级标签(标题/段落),
    再配 `bold`/`code` 这些行内强调, 否则 `**粗体**` 会被段落字体盖掉(反之则标题里的
    粗体会把字号带跑)。表格还要用**自己的一对标签**(`tblN` / `tblNb`), 否则表内的粗体
    会用正文 10pt 的字号, 把那一行撑高。

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

### 摄像头类

21. **Windows 不会告诉你"别的程序想要摄像头"** —— 这条是 2026-09-18 实测出来的,
    直接决定了一个功能的设计:

    | 第二个句柄的后端 | 能开吗 | **第一个还读得到吗** |
    |---|---|---|
    | `CAP_DSHOW` | 能开、能读 | **照样能读** —— 设备被共享, 第一个人毫无察觉 |
    | `CAP_ANY`(MSMF) | 能开, 读失败 | **全部失败** —— MSMF 会把 DSHOW 的流踢掉 |

    所以**不能靠 `cap.read()` 失败来发现"有人在等摄像头"**: 别人抢一下失败了,
    我们的读一直成功。2026-09-18 当天日志里 `camera_read_fail`/`camera_busy` **一条都没有**,
    而用户明确报告"别的软件打开时它不让出摄像头"。
    **解法**(`[camera_yield]` 配置 + 托盘手动让出): 只能主动看**前台窗口标题**。
    ⚠️ 关键词必须特定到"这个程序正在用摄像头", **不能只是"这个程序开着"** ——
    尤其不许把"微信"/"QQ"放进去(它们在黑名单里, 那等于开免监控后门, 正好在它该抓你时失效)。
    `tests/test_camera_yield.py` 里有专门的测试守着这条。
22. **摄像头打不开时的重试要逐步退让**。固定 30 秒重试等于每 30 秒就把摄像头抢回来一次,
    会跟正在用它的程序反复拉锯(对方可能因此一直拿不到)。现在 30→60→120→240→300 秒封顶,
    成功一次即清零。

### 已知偶发(不是 bug, 别慌)

- ~~单测偶尔报 `FAILED (errors=12, skipped=7)`~~ → **已定位, 不是 flake**:
  那是 **GBK 控制台 + 提醒文案里的 emoji** 让 `print` 抛 `UnicodeEncodeError`
  (见 §5 第 4b 条)。当时"连跑 11 次全绿"是因为那些命令都设了
  `PYTHONIOENCODING=utf-8`, 而失败的那次没设 —— 它是**确定性**的, 不是偶发。
  现已修复:UTF-8 / GBK / 不设编码 三种情况各 214 项全绿。
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
| 17 | **别的程序要用摄像头时不让出**(用户报) | 只看人脸覆盖率,不看前台窗口标题 | `test_camera_yield`(17 项) |
| 18 | 让出摄像头时日志写了个假原因 | `_release_camera()` 的释放原因硬编码 | `test_release_log_does_not_lie_about_the_reason` |
| 19 | **日报/悬停提示卡在 17 分钟不动**(用户报) | 写失败把 sqlite 连接留在事务里 → 读永远停在旧快照 | `test_store_lock`(7 项, 已变异验证) |
| 20 | **分心时被杀掉, 那段分心凭空消失**(用户报 bug 顺带发现) | `shutdown()` 没收口进行中的分集 → 日报"提醒 2 次 / 分心 0 分钟" | `test_runtime_contract.TestShutdownClosesOpenEpisodes`(3 项)、`test_store.TestUnfinishedEpisodes`(5 项) |
| 21 | **分心/离开在"停止判定"那一刻静默消失** | `_tick` 非判定分支丢掉了 `sm.update()` 的返回值(suspend 的收口动作没 dispatch) | `test_suspend_accounting`(7 项, 已变异验证) |
| 22 | 进行中/被打断的暂停整段消失, 理由也没了 | `shutdown()` 和生成日报时都不写 `pause_end`, 而统计只认它 | `test_suspend_accounting` |
| 23 | `_resume` 记账失败时用户零提示 | 它是唯一没护栏的记账动作 | `test_suspend_accounting` |
| 24 | **「离开」被从有效专注里扣了两次** | `blind_seconds` 不排除 away, 而 `focus = monitored - distract - blind - away` | `test_time_accounting`(10 项, 已变异验证) |
| 25 | 改 `tick_hz` 会**回溯性改写**全天统计 | `monitored_seconds` 用标称 tick_hz 折算, 不是实测秒数 | `test_time_accounting` |
| 26 | 跨午夜: 时长记到次日 + 起始日假报"程序崩溃" | 事件按 `at.date()` 分日, 时间段不拆 | `test_time_accounting` |
| 27 | 背单词时段用手机被当成摸鱼扣专注 | `episode_end` 不落 `record_only` | `test_accounting_flags`(11 项, 已变异验证) |
| 28 | 点了「判定错了」照样扣那段的分心时长 | `report_wrong` 只设静默, 没真的结束分集 | `test_accounting_flags` |
| 29 | UI 报错被记成 `alert_dismissed`(像"用户点了我回来了") | `_drain_ui` 的 else 分支吞掉一切未知类型 | `test_accounting_flags` |
| 30 | 清空截图漏掉删不掉的文件却报"已清空" | `clear_all` 只数成功的, `except OSError: continue` | `test_accounting_flags` |
| 31 | 白名单通用词让黑名单站点"隐身"且不留痕 | 白名单优先(刻意) × 通用名词 | `test_shadow_and_disable`(8 项) |
| 32 | 热重载关掉信号 → 分集收不了口, 时长拉到块结束 | 未启用的信号被 `continue` 跳过, 没人收口 | `test_shadow_and_disable` |
| 33 | Tk 起不来时提醒全哑, 托盘兜底走不到 | `tk.Tk()` 在 try 之外 + `show_report` 从不抛异常 | `test_ui_lifecycle`(3 项, 需 `AP_UI_TESTS=1`) |
| 34 | 暂停框被顶掉/点 X 时静默取消 | 只有 算了/Esc 发 `reason_cancelled` | `test_ui_lifecycle` |
| 35 | 被压掉的提醒声不留痕 / 分辨率不校验 / `start==end`=24 小时 / `GetTickCount` 有符号 / 死代码 | 见 PROGRESS 七之十七 | `test_low_severity`(6 项) |
| 36 | 5 个测试写死了 `DAY = "2026-09-20"`, 跨日之后整批变红(和代码无关) | runtime 打的时间戳是真实 `now`, 测试伪造的 `Observation.at` 是写死的日期 | `test_accounting_flags` / `test_shadow_and_disable`(改成从 `datetime.now()` 派生) |
| 37 | 日报只有全天一个"有效专注", 看不出哪一段塌了 | 没有按作息表切分(event 里只有 `nudge` 带 `block`) | `test_block_report`(26 项, 含"逐项可加"的不变式) |
| 38 | 日报窗口里八列的表格挤成 `\| a \| b \|` 一行, 对不齐(用户报) | 查看器只把 markdown 记号删掉后原样显示 | `test_mdview`(23 项)、`test_ui_lifecycle.TestReportPreviewRenders`(5 项, 需 `AP_UI_TESTS=1`) |

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

固定四行(有效专注 / 实际监控 / 计划 / 分心 / 暂停 / 离开 / 看不清)+ 未监控时段警告 +
**各时段专注情况(按作息表)** + 最常分心时段 + 分心前窗口 top5 + 暂停理由 +
数据可信度(人脸覆盖率、摄像头占用漏检)。

**各时段专注情况**(2026-09-25 用户要求):按 `[schedule]` 里**你自己的作息**把一天切开,
逐段给出 计划 / 实际监控 / 有效专注 / 专注率 / 分心 / 看不清 / 离开,外加一行合计和一行
"时间表外"(手动学习、提前开机、拖堂)。三个口径上的讲究:

- 分母仍是**实际监控时长**(和标题一致);"还没到"的时段写 **未到**、已过去却一分钟都没
  监控到的写 **无监控数据**, 都不写 0% —— 中午打开日报时, 下午那个 0% 是假的;
- 不足一分钟的时长写"20 秒"而不是"0 分钟"(否则 `0 分钟(2 次)` 看着像记账出错);
- 各段之和与合计对不上(够 1 分钟)时会自己写一行 ⚠️, 见 §5 的 14h。

**在窗口里看的是渲染后的预览**(2026-09-25 用户要求):`ui.show_report` 把 markdown 交给
`mdview.render()`, 标题/引用/列表/粗体各有样式, 表格用像素 tab 停靠位对齐;窗口右下角有
「看纯文本 / 看预览」可以切回原始文本。渲染失败时**退回纯文本并记一条 `ui_error`**,
不会留下一个空窗口。磁盘上的 `.md` 文件本身不变(还是给编辑器和 git 看的)。


### 诊断脚本(都在 `probe_out/`, 不进版本库但**很有用**)

| 脚本 | 用途 |
|---|---|
| `analyze_log.py` | 一键体检:错误行、派发记录、状态机快照、分集/提醒对照、**按信号的分集统计**、低覆盖率分钟 |
| `posenoise.py` | 离线量 Pose 头部关键点的几何与噪声(跑 `captures/*.jpg` 的摄像头那一半, **不需要摄像头**) |
| `poselabel.py` | **M4 真人标定**(需要摄像头空闲):按屏幕提示摆 5 个姿势约 2.5 分钟,**每段用提示音报序号**(响 N 声 = 第 N 个姿势),并直接给出"低头/转头判据分得开/分不开"与建议阈值。开头 2.5 秒样本不计入(换姿势的延迟) |
| `replay_poselabel.py` | 用标定的**真实逐帧数据**回放估计器, 对比修复前后的检出率/误报率(HANDOFF §11 第 2 条那套做法) |
| `verify_wiring.py` | **接线验证**(不需要摄像头):用 `captures/` 的一张真实画面跑**完整生产管线**, 确认 `analyze() -> pose_head` 不抛异常、且 POSE 只记录不报警。改完 perception/signals 接线后跑一次 |
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

### M4(基本完成):Pose 弱证据通道已建好、接线、真机验证、真人标定;**发呆已砍掉**

**问题**:发呆信号要求"低头姿态 + 头几乎不动 + 眨眼率下降",而
**低头写题时人脸覆盖率只有 12–15%** —— 它依赖人脸关键点,在最需要它的场景下天然不可用。
另外基线眨眼率测得 3.0 次/分(正常 15–20),说明眨眼检测欠计数,这个判据不可靠。

**这个洞有多大(2026-09-18 实测)**:当天 09:09–18:14 监控 6 小时 15 分,其中
**「看不清」63.9 分钟 = 监控时长的 17%**;17:33–17:51 那段人脸覆盖率长期在 **0–10%**。
也就是说日报有六分之一的时间是瞎的 —— 这正是 M4 要修的东西。

#### 已完成的调研(2026-09-18 下午, **别再重做**)

数据来源:`data/events.sqlite3` 的 `coverage_minute` +
`probe_out/posenoise.md`(离线跑 20 张 `captures/*.jpg` 的摄像头那一半,
与生产管线同样的 640px Pose 输入;脚本 `probe_out/posenoise.py` 可重跑)。

| 结论 | 证据 |
|---|---|
| **Pose 兜底通道确实存在** | 人脸覆盖率 <60% 的 54 个分钟里,Pose 仍有 **80.6%** 的帧在;21:49(疑似低头写题)人脸只有 33%,Pose 是 **100%** |
| **Pose 关键点的 `visibility` 恒为 1.0**,哪怕头转了 41° | 所以**不能拿它当质量闸门**(与直觉相反),只能用几何 |
| 质量闸门必须用几何 | 实测抓到一帧肩宽 **96px vs 常态 204px**(部分出画),那一帧所有比值都是垃圾 |
| `side`(鼻相对肩中线 / 肩宽)可判「转头」 | yaw≈0 时 \|side\|<0.15;yaw −34°/−41° 时 −0.45/−0.62 |
| **`bow` 只能相对判断** | 与真人脸 pitch 只有 **r = −0.66**;直立帧的 bow 本身就在 0.35–0.78 铺开 |

**⚠️ 最关键的一条(它推翻了一版设计,别再改回去)**:按"中位数 ± k·MAD"算出来的死区是
**0.39**,**比低头造成的位移(0.35)还大** → 真低头一帧都判不出来。所以判据改成了
**分位数范围**(直立基准的 p10/p90):低头只要"掉出你平时直立的范围"就算。
`tests/test_pose_head.py::test_median_band_would_have_failed` 是一条**反向测试**,专门钉住这个决策。

#### 已完成(代码 + **285 项单测全绿**)

- `src/attention_please/baseline.py` —— 滚动基线(中位数/MAD/p10/p90、可信样本闸门、
  样本不够就弃权、抽稀、带下限的死区)。眨眼基线与 Pose 直立基准共用它。
- `src/attention_please/pose_head.py` —— Pose 粗头姿: 尺度闸门 + 短窗中位数 +
  **直立基准只吃"人脸确认头没低"的帧**(所以长时间低头不会污染基准)+ 拿不准就弃权。
- 接线:`config`(新键 `pose_*`)/ `perception`(`FrameFeatures.pose_head`)/
  `signals`(新 `SignalKind.POSE`,**两道独立闸门**保证永不报警: 上限硬 0 +
  结构上强制 `record_only`)/ `store`(pose 不算分心,也不算进"最常分心时段")/
  `report`(把「看不清」拆成"疑似纸笔模式" vs "什么都没看到")/ `runtime` / `config.toml`。
- 顺手修掉两个真 bug:`Policy.enabled` 默认值写死 `{screen,phone,daze}`(后果是**新信号在
  单测里永不触发、生产里却会触发**);`busiest_distraction_hours` 把 `away` 分集也算成了分心。
- 诊断工具:`probe_out/analyze_log.py` 修好了它自己的 GBK 崩溃(§5 第 4b 条那个坑漏在了这里),
  并新增"按信号看分集"与 Pose 相关统计。
- **新增 `tests/test_baseline.py`(20 项)/ `test_pose_head.py`(35 项)/
  `test_pose_evidence.py`(19 项)`test_runtime_contract.py` +2 项** —— 214 → 290 全绿。

#### 真人标定:**已做完**(2026-09-18 18:48, 5 段 / 3740 帧)

脚本 `probe_out/poselabel.py`(屏幕上有姿势提示 + **提示音**;用"响几声"编码阶段序号,
因为低头写题/转头时看不了屏幕),结果 `probe_out/poselabel.md`。

| 判据 | 原始数据 | 结论 |
|---|---|---|
| **低头** | 直立 bow p10 **0.491**(最小 0.459) vs 低头 bow p90 **0.343**(最大 0.376) | **分得开**,间隔 **0.149** → `min_margin = 0.08` 落在两者正中间 |
| **转头** | 直立 side −0.071..+0.084 vs 转头 side **−0.666..−0.401** | **分得开**,间隔 **0.482** |
| 侧身/后靠 | 肩宽 205px vs 直立 329px(比值 0.62) | **100% 弃权** —— 尺度闸门正确拒绝,没瞎猜 |

**回放验证**(`probe_out/replay_poselabel.py`, 用真实逐帧数据喂修好的估计器):

| 阶段 | 修复前 | 修复后 |
|---|---|---|
| 看屏幕 误报 | 0% | **0.0%** |
| 低头写题 检出 | 93% | **98%** |
| 转头看手机 检出 | **14%** | **98%** |
| 回到看屏幕 误报 | —— | 0.6%(3 帧,全在换姿势后 2.5s 内、原始 side 是 −0.01,是 3 秒平滑窗的**正常滞后**) |

**⚠️ 标定暴露了第二个"基准污染"通道(已修, `7baf238`)**:直立基准的可信闸门原来**只检查
pitch**,但"头没低"**不等于**"头朝前" —— 看手机时头是抬着的,于是整整 25 秒的转头帧被当成
直立样本喂进基准,占据了窗口里最小的那些排名,把基准的 side p10 从 −0.03 **拖到 −0.5**,
阈值被推到 −0.58,只剩最偏的 14% 转头帧还能判出来。**修法**:可信样本要求
`|face_yaw| < phone_yaw_deg`(本机 19°)。修完基准回到 p10 −0.031 / p90 +0.023。

> 教训:相对判据的**锚**(基准)比判据本身更容易错,而且错了以后表现是"检测率莫名很低",
> 不像崩溃那么显眼。任何"用 X 判断可信样本"的闸门,都要问一句"X 成立就等于它可信吗?"

#### 还没做(下一步)

| 项 | 现状 | 怎么做 |
|---|---|---|
| ~~发呆的处置~~ | ✅ **已砍掉**(2026-09-18 用户拍板):删了 `SignalKind.DAZE`、4 个 `daze_*` 配置键与 4 项发呆测试 | 无需动作。`yaw_std`/`pitch_std`/`blink_rate`/`eye_closed_ratio` 四个观测字段**保留但已无信号消费**(代码里已注释) |
| ~~M4-E 眨眼基线自适应~~ | ✅ **不做** —— 眨眼只被发呆用到, 发呆砍了它就是白做 | 若将来复活发呆, 先推翻下面那三条证据 |
| ~~重启托盘~~ | ✅ 已于 18:56 重启,当前实例含 pose 通道 + 全部修复 | —— |

**发呆为什么砍掉**(三条独立证据, 用户已拍板):
1. 它依赖人脸关键点,而低头写题时人脸覆盖率只有 12–15%,**在最需要它的场景下天然不可用**;
2. 它的眨眼闸门用校准基线 3.0/分,`blink_rate < 1.5` 几乎永不成立 → **它现在既不误报也不生效**;
3. **改用 Pose 来报警也不行**:Pose 通道判"低头"确实准(98%),但"低头 + 头不动 4 分钟"
   正是**认真写题**的样子 —— 拿它报警就是最大的误报源。所以 Pose 通道**必须**保持"只记录"。

结论:Pose 通道已经拿到了 M4 想要的价值(把日报的「看不清」拆开),而发呆作为一个**报警**
信号没有可行的证据链 —— **已按预案砍掉**,现在只剩 screen + phone + pose(记录)。
⚠️ 复活发呆前必须先回答上面三条;否则它会以"低头写题被当成发呆"的形式回来。

### 交接时"未验证"清单(**下一轮要补的实测**)

| 项 | 现状 | 怎么验 |
|---|---|---|
| **离座 15 分钟提醒** | 实例**已含该代码**(13:30 重启过),但当天唯一一次离开只有 **10.8 分钟**(15:11:30–15:20:49),**没够到 15 分钟门槛 → 仍未响过** | 离开座位 15 分钟以上, 听是否响一声 + 回来看见常驻弹窗 |
| 托盘"蓝色离开"状态 | 单测通过, 未真人看过 | 离开座位 90 秒后看托盘图标是否变蓝 |
| 日报新口径 | 已用 9/18 真实数据生成过, 未看过自动生成的版本 | 重启后等 21:30 自动生成, 看比值分母是不是"实际监控时长" |
| 看手机信号的真实误报 | ✅ **有数据了(9/18 全天)**: `phone` 共 **6 段 / 2.8 分钟**, yaw 证据 **22°–55°**(阈值 19°), 没有擦边触发的段。**未见误报** | 继续看; 真误报就把 `phone_continuous_seconds` 从 15 调回 20-25 |
| 「判定错了」反馈 | 9/18 全天 **0 次**(`wrong` 事件 0 条) | —— |
| 摄像头占用漏检 | 9/18 全天 `camera_busy` **0 条** | —— |
| 开机自启 | 快捷方式已装, 但**没经历过一次真实登录** | 下次登录后确认托盘自动出现 |
| **Pose 通道本身** | ✅ **已真机验证**(18:26 那次):抓到了 `EpisodeStart pose record_only=True` / `EpisodeEnd pose duration=32.9`, **一声没响**;20:14 已重启带全部修复的版本 | 看今晚 21:30 日报的「看不清」拆分(9/18 全天有 63.9 分钟「看不清」= 监控时长的 17%) |
| **摄像头让出** | ✅ **已真机验证**(2026-09-18 20:17 用 OBS):`📷 已让出摄像头(前台是「OBS」)` → 1 秒后释放 → OBS 关掉后 4 秒收回 → 2 秒后判定恢复(4.5 Hz / 人脸 90%)。事件库里 `camera_yield`/`camera_yield_end` 齐全。2026-09-20 13:37 又自然复现过一次(手动让出 52 秒) | 下次用**会议软件**(腾讯会议/钉钉)再验一次; 认不出来的软件用托盘「让出摄像头(30 分钟)」并把窗口标题关键词加进 `[camera_yield] apps` |
| **日报数字冻结**(19 号 bug) | ✅ 已定位+修复+变异验证, 但**运行中的实例还是旧的**(见 §9 下面的提醒) | 重启托盘后点「打开今日日报」, 数字应随时间增长而不是卡在 17 分钟 |

- ~~**离座提醒需要重启实例才生效**~~ → 已重启(13:30),代码在跑;缺的只是一次 >15 分钟的离开。
- ~~**日报口径修复也需要重启**~~ → 同上。
- ~~**`git init`**~~ → **已完成**(2026-09-18):`main` 分支,首次提交 `6016a2c`(57 文件/9719 行)
  + `.gitattributes`(行尾统一 LF)。改代码前建议先开分支,改完 `git diff` 自查。
  ⚠️ **`core.autocrlf=true` 与 `.gitattributes` 的 `eol=lf` 并存**:编辑工具会把个别文件
  整篇写成 CRLF(config.toml / runtime.py 出现过)。提交时 git 会归一化成 LF,所以历史是干净的,
  但工作区会不一致 —— 要修就用 `os.replace()` 把 `\r\n` 换成 `\n`(见 §5 第 4 条)。
- **`probe_out/analyze_log.py` 的"可疑分集"标记不考虑冷却**:一段 64 秒、最高 L0 的分集
  如果落在 180 秒冷却窗里,是**设计如此**(不是饿死),但脚本会标它"可疑"。看到可疑标记先
  对照 `nudge` 事件时间再下结论。
- ~~⚠️ **分集被"程序被杀"截断时不会写 `episode_end`**~~ → **已修(2026-09-20, 见 §6 第 20 条)**:
  `shutdown()` 现在会收口进行中的分集; 硬死(被杀/崩溃/蓝屏)的情况无法补救, 于是
  `DayStats.unfinished_episodes` 把"有几段没等到收尾"报出来(日报一行 + 启动横幅一句),
  **但绝不编时长** —— 不知道就是不知道, 宁可缺不可假。
- ⚠️ **`test_store_lock.py` 修的是"连接被写失败污染"**,而**已经跑着的实例里那个连接是回不来的** ——
  必须重启托盘才会恢复(监控线程本身一直是好的, 数据没丢)。
- ✅ **全面体检已完成(2026-09-20)**: 21 个隐性 bug 全部修完并做了变异测试,
  详见 PROGRESS「七之十七」和 §6 第 21–35 条。
- ✅ **两个产品决策已由用户拍板(2026-09-20), 不要再动**:
  - **白名单优先级 = 白名单赢**(M5)。原话:「考研数学基础班 - 知乎 应该归为专注,
    因为这是我在利用平台查找相关考研资料, 还有类似的我会在 b 站上看题目解析,
    应该是白名单的优先级高于黑名单」。所以「数学/英语/考研/真题」这类学习词
    **故意**赢过「知乎/哔哩哔哩/微博」这类站点名 —— **绝对不要改成站点优先**。
    `title_shadowed` 事件保留, 但它是**诊断数据不是警告**; 日报里那句也改成中性措辞
    (`test_whitelist_beats_blacklist_is_locked_in` 钉着这条)。
  - **`tick_hz` 保持 5**, 不提到 10。
- 可选:周报、打包 exe、多显示器支持、把"离开提醒通道"做成独立配置(现在跟随安静时段)。

---

## 10. 已知限制与风险

1. **人脸可见性是一切推断类信号的前提**。覆盖率低(低头/靠后/光线差)时 phone 也是瞎的,
   此时只有"窗口标题"信号有效。覆盖率会如实写进日报,不会假装你在专注。
2. **`有效专注` 依赖覆盖率记账**:`focus = 监控时长 − 分心 − 看不清 − 离开`。
   如果覆盖率长期偏低,"看不清"会吃掉大量时间 —— 这是诚实的设计,但用户可能误读为"我没学"。
3. **校准会漂移**:坐姿/笔记本高度一变,阈值就不准 → 建议大改桌面布置后重跑 `calibrate.py`。
4. **机器会蓝屏(0x124 硬件错误)**:程序每 2 秒落盘,但一次蓝屏仍会丢最多 2 秒数据;
   重启后 `coverage_minute` 会出现空洞(日报会体现为覆盖率下降)。
5. **托盘版依赖 `pythonw`**:崩溃时没有控制台,只有 `data/logs/` 里有线索。

---

## 11. 给下一个 agent 的工作约定

1. **先跑测试再改代码**(429 项,约 9 秒,不需要摄像头)。改完再跑一次。
   UI 那 15 项默认跳过,要 `$env:AP_UI_TESTS=1` 才会真弹窗(约 25 秒)。
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
11. **改完要同步到两个远端仓库**(用户 2026-09-20 要求)。已经配好: `origin` 的 fetch 指向
    GitHub, 而 **push 有两个 URL**(GitHub + Gitee), 所以一条 `git push` 就同时推两边:

    ```
    origin  https://github.com/WuHuanHere/Attention-Please.git   (fetch + push)
    origin  https://gitee.com/WuHuanHere/attention-please.git    (push)
    ```

    ⚠️ **两个仓库都是公开的**。推之前务必确认没有个人数据: `data/`、`captures/`、
    `calibration.json`、`probe_out/` 已在 `.gitignore` 里; 文档里**不要写 Windows 用户名**
    (用 `%LOCALAPPDATA%` / `%APPDATA%` 代替)。
    ⚠️ `git push` 需要 Git Credential Manager, 而**沙箱会拦住它**(msys `sh.exe` 报
    "couldn't create signal pipe, Win32 error 5") —— 需要用 `danger-full-access` 跑一次。

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
