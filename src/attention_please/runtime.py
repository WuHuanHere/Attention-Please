"""运行时: 把摄像头/感知/状态机/提醒/存储接起来跑。

设计上刻意保持"薄": 所有判定逻辑都在 signals.py(纯逻辑、有单测),
这里只做编排 —— 读帧、填策略、派发动作、记账。

几个实测逼出来的决定:
  - **摄像头开着就别关**: 每帧重开摄像头会闪指示灯、触发驱动问题, 而且慢。
    只有"长时间不判定"(默认 120 秒)才释放, 免得你看视频时摄像头灯一直亮着。
  - **摄像头被占用要留痕**: 打不开就每 30 秒重试, 并把这段时间记成 camera_busy,
    日报里会明确写出"今天因摄像头占用漏检 X 分钟"。漏报必须可见。
  - **人脸覆盖率逐分钟入库**: 覆盖率低的时候"看手机"其实是瞎的,
    这段时间要记成"看不清", 既不算专注也不当分心。
  - **"摄像头瞎了"不等于"你走了"**: 采集流死掉时 `cap.read()` 会返回 `ok=True` 的
    **纯黑帧**(2026-09-27 实测), 于是人脸/Pose 全 0% —— 以前这就被算成"离开座位",
    一错 36 分钟。现在先用 framehealth 判"这张图里有没有信息", 没有画面就记成
    `camera_blank` 并自动重开摄像头, **绝不当成离开**。
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import capture, foreground, framehealth, input_activity
from .camera import CameraInfo, grab_frame, open_camera, set_buffer_size
from .config import Calibration, Config
from .logbook import Logbook, install_streams, safe_print
from .notifier import LEVEL_LABEL, Notifier
from .perception import FrameAnalyzer
from .report import write_report
from .signals import (
    SIGNAL_LABEL,
    AwayChange,
    EpisodeEnd,
    EpisodeStart,
    FocusStateMachine,
    Nudge,
    Observation,
    Policy,
    SignalKind,
)
from .store import Store
from .ui import AlertRequest, AlertUI
from .wordlist import classify

LOCK_PORT = 47731          # 单实例: 这个端口被占用说明已经有一个在跑
CAMERA_RETRY_SECONDS = 30
# 摄像头打不开时的重试上限。**逐步退让**而不是固定 30 秒: 固定间隔等于每 30 秒就把
# 摄像头抢回来一次, 会跟正在用它的程序(会议软件等)反复拉锯。
CAMERA_RETRY_MAX_SECONDS = 300
CAMERA_RELEASE_AFTER = 120
# "没有画面"时自动重开摄像头的间隔: 第一次等 5 秒, 之后翻倍, 上限 2 分钟。
# 实测这是**唯一有效的解药** —— 2026-09-27 09:27 那次采集流死了 36 分钟, 只有
# "让出 + 收回摄像头"(release + 重新 open)才把它救回来; cap.read() 自己永远好不了。
# 退避的理由和 CAMERA_RETRY_MAX_SECONDS 一样: 别跟正在用摄像头的程序反复拉锯。
BLANK_REOPEN_SECONDS = 5
BLANK_REOPEN_MAX_SECONDS = 120
CONFIG_RELOAD_SECONDS = 60
SUMMARY_EVERY_SECONDS = 300
# 落盘间隔。**不是随手定的数字**: 每帧的 coverage_tick 会隐式开启一个写事务, 在 commit
# 之前整个库的写锁都在监控线程手里。原来写的是 30 秒 —— 托盘线程(暂停/让出/日报)的
# 写操作会在 10 秒 busy timeout 之后失败, 而失败的写会把那个连接永久留在事务里, 于是
# 托盘读到的永远是失败那一刻的快照(2026-09-20 实测: 日报卡在 17 分钟不动)。
# 2 秒既远小于 busy timeout(托盘写基本不会撞锁), 又比原来多 15 倍落盘频率:
# 蓝屏最多丢 2 秒, 而不是 30 秒。
FLUSH_EVERY_SECONDS = 2.0


def acquire_single_instance() -> socket.socket | None:
    """占一个本地端口当作互斥锁。返回 None 表示已经有一个实例在跑。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def describe_action(act) -> str:
    """把一个动作写成一行, 用于文件日志。

    这行日志的价值在于定位"该提醒却没提醒"到底丢在哪一环:
    状态机发出了吗(moment of truth)、派发了吗、记账了吗、提醒通道报错了吗。
    """
    bits: list[str] = [type(act).__name__]
    signal = getattr(act, "signal", None)
    if signal is not None:
        bits.append(str(getattr(signal, "value", signal)))
    for field in ("level", "duration", "record_only"):
        value = getattr(act, field, None)
        if value is None:
            continue
        bits.append(f"{field}={round(value, 1) if isinstance(value, float) else value}")
    evidence = getattr(act, "evidence", "")
    if evidence:
        bits.append(f"「{evidence}」")
    return " ".join(bits)


class Runtime:
    def __init__(self, cfg: Config, cal: Calibration):
        self.cfg = cfg
        self.cal = cal
        self.store = Store(cfg.db_path)
        # 文件日志: 托盘版没有控制台, 没有它出事现场就是一片空白(2026-09-17 吃过亏)
        self.log = Logbook(cfg.data_dir / "logs")
        self.notifier = Notifier()
        self.ui = AlertUI()
        self.sm = FocusStateMachine(cfg, cal)
        self.analyzer: FrameAnalyzer | None = None
        self.cap = None
        self.cam_info: CameraInfo | None = None
        self.cam_fail_since: datetime | None = None
        self.cam_next_retry: float = 0.0
        self.cam_fail_count: int = 0
        # --- 画面有效性("摄像头瞎了" 和 "人走了" 必须分开) ---
        self.health = framehealth.FrameHealthMeter()
        self._blank_at: datetime | None = None    # 这一轮"没有画面"从什么时候开始(墙钟)
        self._blank_reopens = 0                   # 这一轮故障里自动重开了几次
        self._blank_count = 0                     # 连续的故障轮次(用来退避)
        self.idle_since: float = time.monotonic()
        self.running = True
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        # --- 暂停 / 手动学习 ---
        self.paused = False
        self.pause_started: datetime | None = None
        self.pause_reason = ""
        self.manual_until: datetime | None = None
        # --- 摄像头让出(会议/通话/直播软件) ---
        self.yield_until: datetime | None = None      # 手动让出到这个时刻
        self._yield_since: datetime | None = None     # 当前这次让出的起点(用来记时长)
        self._yield_reason = ""
        # --- 每日任务记账 ---
        self._report_day: str | None = None
        self._cleanup_day: str | None = None
        self._last_frame = None
        self._face_pct_recent = 100.0
        self.t0 = time.monotonic()
        self._cfg_mtime = 0.0
        self._cfg_checked = 0.0
        self._summary_at = 0.0
        self._status_at = 0.0
        self._flush_at = 0.0
        self._tick_count = 0
        self._face_count = 0
        # 上一帧的单调时刻, 用来算"这一帧真实经过了多久"(写进 coverage_minute.seconds)。
        # 不判定时清零 —— 否则待机/让出一小时后的第一帧会把那一小时算成监控时间。
        self._last_tick_mono = 0.0
        # 上一次见过的前台标题(只在标题变化时检查"白名单有没有盖住黑名单")
        self._last_title_seen = ""

    # ------------------------------------------------------------------
    # 策略: 时间表 + 安静时段 + 启用信号
    # ------------------------------------------------------------------
    def _policy(self, at: datetime, title: str | None = None) -> Policy:
        enabled = frozenset(self.cfg.detection.enabled_signals)
        with self._lock:
            paused = self.paused
            manual_until = self.manual_until
            yield_until = self.yield_until
        if paused:
            return Policy(judging=False, enabled=enabled, block_name="已暂停")

        # --- 先算出"本来该不该判定"(作息表 + 手动学习) ---
        # 作息表优先于"手动学习": 否则你在"英语单词"块里点手动学习,
        # 会把这个块的 allow_phone=true 冲掉, 背单词反而被当成玩手机。
        block = self.cfg.schedule.block_at(at)
        if block is not None:
            would_judge, block_name, allow_phone = True, block.name, block.allow_phone
        elif manual_until is not None and at < manual_until:
            would_judge, block_name, allow_phone = True, "手动学习", False
        else:
            # 时间表外本来就待机(摄像头也已经释放了), **没什么可让的**。
            # 这里必须直接返回, 不能走下面的让出分支 —— 否则你晚上挂着会议室,
            # 日报会把整个晚上都算成"主动让出摄像头", 那是骗人的。
            return Policy(judging=False, enabled=enabled, block_name="")

        # --- 本来该判定 -> 现在问"要不要把摄像头让出去" ---
        # 手动让出优先于自动判断 —— 手动是明确的用户意图。
        if yield_until is not None and at < yield_until:
            left = (yield_until - at).total_seconds() / 60.0
            return Policy(judging=False, enabled=enabled, camera_yield=True,
                          block_name=f"已让出摄像头(手动, 还剩 {left:.0f} 分钟)")
        hit = self._yield_app(at, title)
        if hit is not None:
            return Policy(judging=False, enabled=enabled, camera_yield=True,
                          block_name=f"已让出摄像头(前台是「{hit}」)")

        return Policy(
            judging=True,
            allow_phone=allow_phone,
            quiet=self.cfg.quiet_hours.is_quiet(at),
            block_name=block_name,
            enabled=enabled,
        )

    def _yield_app(self, at: datetime, title: str | None = None) -> str | None:
        """前台窗口是不是"要用摄像头的软件"? 是就返回命中的关键词。

        ⚠️ Windows 不会告诉我们"别的程序想要摄像头"(实测: 我们占着摄像头时, 别人的
        打开请求失败, 而我们的 cap.read() 照样成功 —— 完全察觉不到)。所以只能靠这个
        主动判断。匹配的是**窗口标题**, 关键词表在 config.toml 的 `[camera_yield]`。

        注意: 关键词必须**特定到"这个程序正在用摄像头"**, 不能只是"这个程序开着" ——
        尤其不能把"微信"/"QQ"放进去(那等于给自己开免监控后门, 详见 config.DEFAULT_YIELD_APPS)。
        """
        cy = self.cfg.camera_yield
        if not cy.enabled or not cy.apps:
            return None
        text = title if title is not None else foreground.foreground_title()
        if not text:
            return None
        for word in cy.apps:
            if word and word.lower() in text.lower():
                return word
        return None

    def yield_camera(self, minutes: int | None = None) -> None:
        """托盘点"让出摄像头": 释放摄像头 + 暂停判定一段时间(给关键词匹配不到的软件兜底)。"""
        mins = minutes if minutes is not None else self.cfg.camera_yield.manual_minutes
        with self._lock:
            self.yield_until = datetime.now() + timedelta(minutes=mins)
        self._say(f"📷 让出摄像头 {mins} 分钟(判定暂停, 这段时间会记进日报的'让出')")
        self._log("camera_yield", detail=f"手动让出 {mins} 分钟")

    def reclaim_camera(self) -> None:
        """托盘点"收回摄像头"。"""
        with self._lock:
            had = self.yield_until is not None
            self.yield_until = None
        if had:
            self._say("📷 收回摄像头(恢复判定)")
            self._log("camera_yield_end", detail="手动收回")

    # ------------------------------------------------------------------
    # 暂停 / 手动学习 / 状态(托盘与运行时之间的接口, 都可能跨线程调用)
    # ------------------------------------------------------------------
    def request_pause(self) -> None:
        """托盘点"暂停" -> 弹必填理由的输入框(结果由 _drain_ui 处理)。"""
        self.ui.ask_reason("暂停监控", "为什么要暂停?这个理由会记进今天的日报。")

    def pause(self, reason: str, at: datetime) -> None:
        reason = (reason or "").strip()
        if not reason:
            self._say("暂停被拒: 理由不能为空(这就是「暂停的代价」)")
            return
        with self._lock:
            if self.paused:
                return
            self.paused = True
            self.pause_started = at
            self.pause_reason = reason
        self._say(f"[{at:%H:%M:%S}] ⏸ 暂停监控:{reason}(暂停期间不判定, 时长单列)")
        self._log("pause_start", at=at, detail=reason)
        if self.analyzer is not None:
            self.analyzer.close_async()      # 暂停就把模型放掉, 别占着摄像头
            self.analyzer = None
        self._release_camera()

    def resume(self, at: datetime) -> None:
        with self._lock:
            if not self.paused:
                return
        duration = self._close_pause(at, reopen=False)
        with self._lock:
            reason = self.pause_reason
            self.paused = False
            self.pause_reason = ""
        self._say(f"[{at:%H:%M:%S}] ▶ 恢复监控(暂停 {duration / 60:.1f} 分钟:{reason})")

    def _close_pause(self, now: datetime, *, reopen: bool) -> float:
        """把**进行中**的暂停结账(写一条 `pause_end`), 返回这段的秒数。

        必须有人调用它, 否则那段时间和理由会一起消失 —— `day_stats` 和
        `pause_reasons` **都只认 `pause_end`**。三条路径都要走这里:

          - 用户点「恢复学习」 -> `resume()`;
          - 程序退出           -> `shutdown()`;
          - 生成日报时人还暂停着 -> `_daily_jobs()` 先结一段再重开一段(暂停继续计时)。

        实测(2026-09-18): 21:00:06 暂停「今天学太累了」, 21:30 的日报写「暂停 46 分钟」
        且理由里没有它 —— 那 30 分钟和那条必填理由都不存在, 而日报还在说
        「见上面的…暂停记录」, 指向一条根本没写下来的记录。
        """
        with self._lock:
            if not self.paused or self.pause_started is None:
                return 0.0
            duration = max(0.0, (now - self.pause_started).total_seconds())
            reason = self.pause_reason
            self.pause_started = now if reopen else None
        self._log_span("pause_end", start_at=now - timedelta(seconds=duration),
                       end_at=now, duration=duration, detail=reason)
        return duration

    def start_manual_session(self, minutes: int = 60) -> None:
        with self._lock:
            self.manual_until = datetime.now() + timedelta(minutes=minutes)
        self._say(f"手动开始学习 {minutes} 分钟(时间表外的加练也会被记录)")

    def stop(self) -> None:
        self.running = False
        self._stop_event.set()

    def state_key(self) -> str:
        with self._lock:
            paused = self.paused
        if paused:
            return "paused"
        if self.cam_fail_since is not None:
            return "camera_busy"
        # 摄像头开着、帧也在来, 但画面是黑的/被挡住的 —— 和"打不开"一样必须一眼看出来,
        # 否则托盘一直是绿的, 你会以为它在管你, 其实它什么都没看见(2026-09-27 实测)。
        # 用 `_blank_count` 而不是 `_blank_at`: 只有**判定成故障**的才配得上这个状态,
        # 刚打开摄像头时抖一两帧黑不该让图标闪一下。
        if self._blank_count > 0:
            return "no_picture"
        if self._policy(datetime.now()).camera_yield:
            return "yielded"
        if not self._policy(datetime.now()).judging:
            return "idle"
        # 人不在画面里(且已判定为离开)也要在托盘上看得出来 ——
        # 否则图标一直是绿的, 你会以为它在管你, 其实它谁也看不见。
        if self.sm.snapshot(time.monotonic()).get("is_away"):
            return "away"
        return "judging"

    def status_text(self) -> str:
        try:
            st = self.store.day_stats(datetime.now().date().isoformat(),
                                      tick_hz=self.cfg.general.tick_hz)
            state = {"judging": "判定中", "idle": "待机", "paused": "已暂停",
                     "camera_busy": "摄像头不可用",
                     "no_picture": "摄像头没有画面(全黑/被挡)",
                     "yielded": "已让出摄像头",
                     "away": "离开座位(只记录)"}.get(self.state_key(), self.state_key())
            block = self._policy(datetime.now()).block_name
            head = f"attention_please — {state}" + (f"·{block}" if block else "")
            return (f"{head}\n今日有效专注 {st.focus_seconds / 3600:.2f} h, "
                    f"分心 {st.episodes} 次, 提醒 {st.nudges} 次\n"
                    f"人脸覆盖率 {st.coverage * 100:.0f}%")
        except Exception:  # noqa: BLE001
            return "attention_please"

    # ------------------------------------------------------------------
    # 摄像头
    # ------------------------------------------------------------------
    def _ensure_camera(self) -> bool:
        if self.cap is not None:
            return True
        now = time.monotonic()
        if now < self.cam_next_retry:
            return False
        cap, backend = open_camera(self.cfg.general.camera_index,
                                   self.cfg.general.camera_width,
                                   self.cfg.general.camera_height)
        if cap is None:
            if self.cam_fail_since is None:
                self.cam_fail_since = datetime.now()
                self._log("camera_busy", detail="摄像头打不开(被会议软件/OBS 占用?)")
                self._say(f"⚠️ 摄像头打不开 —— 监控暂停, {CAMERA_RETRY_SECONDS} 秒后重试。"
                      f"这段时间会记进日报的'漏检'。")
            # **逐步退让**: 固定 30 秒重试等于每 30 秒就把摄像头抢回来一次, 会跟
            # 正在用它的程序反复拉锯(对方可能因此一直拿不到)。越失败越等得久, 上限 5 分钟。
            self.cam_fail_count += 1
            delay = min(CAMERA_RETRY_SECONDS * (2 ** (self.cam_fail_count - 1)),
                        CAMERA_RETRY_MAX_SECONDS)
            self.cam_next_retry = now + delay
            if self.cam_fail_count > 1:
                self._say(f"   (连续 {self.cam_fail_count} 次打不开, 这次等 {delay} 秒再试)")
            return False
        # 只留 1 帧缓冲, 否则会读到几秒前的旧画面(必须用 CAP_PROP_BUFFERSIZE, 别写魔数)
        set_buffer_size(cap, 1)
        self.cap = cap
        # 刚打开的几帧本来就可能是黑的, 上一帧的画面也不作数了 —— 重新开始量。
        self.health.reset()
        self.cam_info = CameraInfo(index=self.cfg.general.camera_index, opened=True,
                                   backend=backend,
                                   width=int(cap.get(3)), height=int(cap.get(4)))
        # **校验设备实际给的分辨率**。以前 cam_info 存下来就再没人读过, 于是启动横幅
        # 永远打印 config.toml 里的值 —— 而 640x480 下实测人脸检出率是 **0%**
        # ("看手机"和 Pose 通道会直接失效)。摄像头静默回落到低分辨率时, 日志必须说真话。
        want = (self.cfg.general.camera_width, self.cfg.general.camera_height)
        got = (self.cam_info.width, self.cam_info.height)
        if got != want and got[0] > 0:
            self._say(f"⚠️ 摄像头实际给的是 {got[0]}x{got[1]}, 不是配置的 "
                      f"{want[0]}x{want[1]} —— 人脸检出率可能大幅下降"
                      f"(实测 640x480 下是 0%)")
            self._log("camera_resolution_mismatch", detail=f"{got[0]}x{got[1]}")
        if self.cam_fail_since is not None:
            dur = (datetime.now() - self.cam_fail_since).total_seconds()
            self._log("camera_busy_end", duration=dur)
            self._say(f"✅ 摄像头恢复(此前漏检 {dur / 60:.1f} 分钟)")
            self.cam_fail_since = None
        self.cam_fail_count = 0        # 成功一次就把退让计数清零
        if self.analyzer is None:
            self._say("加载 MediaPipe 模型 ...")
            self.analyzer = FrameAnalyzer(self.cfg.models_dir / "pose_landmarker_lite.task",
                                         self.cfg.models_dir / "face_landmarker.task",
                                         calibration=self.cal,
                                         pose_head_kwargs=self._pose_head_kwargs())
            self._say("模型就绪。")
        return True

    def _pose_head_kwargs(self) -> dict:
        d = self.cfg.detection
        return {
            "down_k": d.pose_head_down_k,
            "turn_k": d.pose_head_turn_k,
            "min_margin": d.pose_head_min_margin,
            "book_pitch_deg": self.cal.book_pitch_deg,
            # "头朝前"的判据用"看手机"的 yaw 阈值 —— 超过它就认为头转了,
            # 那样的帧不许进直立基准(否则转头会把自己的基准拖歪)。
            "reference_max_yaw_deg": self.cal.phone_yaw_deg,
        }

    def _should_release_now(self, policy: Policy, mono: float) -> bool:
        """现在该不该释放摄像头。

        两种"不判定"要区别对待:
          - **让出**(别的程序要用): **立刻**放 —— 人家正等着, 不能等 120 秒;
          - 普通待机(作息表外/休息): 等 120 秒 —— 否则休息的几分钟里会反复开开关关。
        """
        return policy.camera_yield or (mono - self.idle_since > CAMERA_RELEASE_AFTER)

    def _track_yield(self, policy: Policy, now: datetime) -> None:
        """让出摄像头的开始/结束留痕。

        "漏报必须可见"这条铁律在这里的意思是: 让出期间**没有监控数据**, 日报必须
        写清楚是"让给别的程序了", 而不是让人以为"我明明在学却没记上"。
        """
        if policy.camera_yield:
            if self._yield_since is None:
                self._yield_since = now
                self._yield_reason = policy.block_name
                self._say(f"📷 {policy.block_name} —— 摄像头已让出, 判定暂停")
                self._log("camera_yield", at=now, detail=policy.block_name)
        elif self._yield_since is not None:
            dur = (now - self._yield_since).total_seconds()
            self._log("camera_yield_end", at=now, duration=dur, detail=self._yield_reason)
            self._say(f"📷 收回摄像头(让出 {dur / 60:.1f} 分钟)")
            self._yield_since = None
            self._yield_reason = ""

    def _release_camera(self, note: str | None = "长时间不在判定区间") -> None:
        """释放摄像头。`note` 是日志里的**原因** —— 必须说真话。

        实测踩到(2026-09-18 20:17,OBS 那次): 让出摄像头时这里仍然打
        "(长时间不在判定区间, 已释放摄像头)" —— 日志里的原因是假的, 而日志正是
        "该提醒没提醒/该让出没让出"这类问题的第一现场。让出时传 `note=None`
        (上面 `_track_yield` 已经把真正的原因写清楚了, 不必再来一条会误导的)。
        """
        if self.cap is not None:
            self.cap.release()
            self.cap = None
            if note:
                self._say(f"({note}, 已释放摄像头)")

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------
    def _log(self, kind: str, *, at: datetime | None = None, signal: str | None = None,
             level: int | None = None, duration: float | None = None,
             evidence: str | None = None, block: str | None = None,
             detail: str | None = None) -> None:
        self.store.event(kind, at=at, signal=signal, level=level, duration=duration,
                         evidence=evidence, block=block, detail=detail)

    def _note_shadowed_title(self, title: str | None) -> None:
        """标题**同时**命中白名单和黑名单时记一笔(诊断用)。

        判定顺序是"白名单优先", 而且这是**用户 2026-09-20 明确确认过的**:
        他用知乎/B站查考研资料、看题目解析, 所以「数学/考研/真题」这类学习词
        应该赢过「知乎/哔哩哔哩」这类站点名。**不要擅自把优先级倒过来。**

        那为什么还要记? 因为它是个**可观测的量**: 如果这个数一天几十次, 说明
        黑名单对你实际上已经基本不起作用了(那时该调的是白名单里的通用词,
        而不是优先级)。所以这里只落一条 `title_shadowed` 事件, 措辞是中性的,
        不写成"警告/可能漏判" —— 它本来就是设计如此。
        """
        if not title or title == self._last_title_seen:
            return
        self._last_title_seen = title
        try:
            m = classify(title, self.cfg.wordlists.whitelist, self.cfg.wordlists.blacklist)
        except Exception:  # noqa: BLE001 - 记一笔而已, 绝不能影响监控
            return
        if not m.shadowed:
            return
        self._say(f"标题命中白名单「{m.matched}」, 同时也有黑名单词「{m.shadowed}」"
                  f"—— 按专注算(设计如此): 「{title[:50]}」")
        self._log("title_shadowed", evidence=f"白名单「{m.matched}」盖住黑名单「{m.shadowed}」",
                  detail=title)

    def _log_span(self, kind: str, *, start_at: datetime, end_at: datetime,
                  duration: float, **fields) -> None:
        """记一段**有始有终**的时间。跨午夜要按天拆开。

        不拆的后果(2026-09-20 审查时发现, 用合成数据复现过): 一段 23:55–00:05 的分集
        整段落进第二天, 于是
          - 起始日留下一个**假的**"没等到收尾"(日报会谎报"程序在分心时被关掉/崩溃");
          - 次日凭空多出 600 秒分心, 而它那天只监控了 600 秒 -> 有效专注被夹成 0。
        拆开之后两边各自的账都自洽: 时长按比例分到两天, 而 monitored 本来就是按天算的。
        """
        if duration <= 0 or end_at.date() == start_at.date():
            self._log(kind, at=end_at, duration=duration, **fields)
            return
        cursor, remaining = start_at, duration
        while cursor.date() < end_at.date():
            # 注意: 这里**不能** `from datetime import time` —— 那会把 `import time`
            # 这个模块整个盖掉, 而 time.monotonic() 到处都是(实测: 一改就全线 AttributeError)。
            boundary = datetime.combine(cursor.date() + timedelta(days=1),
                                        datetime.min.time())
            part = (boundary - cursor).total_seconds()
            self._log(kind, at=boundary - timedelta(seconds=1),
                      duration=min(part, remaining), **fields)
            remaining -= part
            cursor = boundary
        if remaining > 0:
            self._log(kind, at=end_at, duration=remaining, **fields)

    def _say(self, message: str) -> None:
        """控制台输出: 编码不兼容时降级, 绝不抛异常。

        这条不是洁癖: 中文 Windows 控制台是 GBK, 而提醒文案里有 emoji ——
        print 抛异常会把同一段代码里后面的响铃/弹窗一起带走(实测踩到)。
        """
        safe_print(message)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run(self) -> int:
        install_streams(self.log)      # 之后所有 print 都会同时落到 data/logs/
        self.log.write("=" * 60)
        self.log.write(f"启动: 设备 {self.cfg.general.camera_index} @ "
                       f"{self.cfg.general.camera_width}x{self.cfg.general.camera_height}, "
                       f"{self.cfg.general.tick_hz} Hz, "
                       f"启用信号 {self.cfg.detection.enabled_signals}")
        self._say("=" * 72)
        self._say("attention_please 监控中。Ctrl+C 退出。")
        self._say(f"  时间表内自动判定, 当前启用信号: {self.cfg.detection.enabled_signals}")
        self._say(f"  摄像头: 设备 {self.cfg.general.camera_index} @ "
              f"{self.cfg.general.camera_width}x{self.cfg.general.camera_height}, "
              f"检测频率 {self.cfg.general.tick_hz} Hz")
        self._say("=" * 72)
        self._warn_unfinished_episodes()
        interval = 1.0 / max(0.5, self.cfg.general.tick_hz)
        next_tick = time.monotonic()
        self._summary_at = next_tick
        self._status_at = next_tick
        self._flush_at = next_tick
        try:
            while self.running:
                try:
                    self._tick()
                except KeyboardInterrupt:
                    break
                except Exception as exc:  # noqa: BLE001 - 单帧异常绝不能拖垮监控
                    self.log.exception("tick", exc)
                    self._say(f"⚠️ 这一帧出错(已跳过): {type(exc).__name__}: {exc}")
                next_tick += interval
                slack = next_tick - time.monotonic()
                if slack > 0:
                    time.sleep(slack)
                else:
                    next_tick = time.monotonic()
        finally:
            self.shutdown()
        return 0

    def _tick(self) -> None:
        now = datetime.now()
        mono = time.monotonic()
        self._maybe_reload_config(mono, now)
        # 每日任务(清理截图/生成日报)必须放在判定之前: 21:30 生成日报时
        # 通常已经不在学习时段了, 挂在判定分支里会导致日报永远不触发。
        self._daily_jobs(now)
        # UI 事件(暂停理由/判定错了/关闭弹窗)必须在**任何状态下**都处理:
        # 漏掉这一步的后果是"弹窗弹了、理由也输了, 但什么都没发生"。
        self._drain_ui(mono)
        # 前台标题只取一次, 既给策略判断(要不要让出摄像头)也给这一帧的 Observation。
        title = foreground.foreground_title()
        self._note_shadowed_title(title)
        policy = self._policy(now, title=title)
        enabled = frozenset(self.cfg.detection.enabled_signals)
        self._track_yield(policy, now)

        if not policy.judging:
            if self.cap is not None and self._should_release_now(policy, mono):
                # 让出时不出声: 上面 _track_yield 已经写了真正的原因
                self._release_camera(None if policy.camera_yield else "长时间不在判定区间")
            # Pose 弱证据的基准是"你最近的样子"。待机/休息久了它就过期了 ——
            # 拿一小时前(甚至午休前)的姿势当基准, 等于在猜。清掉, 恢复判定后重新学。
            if self.analyzer is not None:
                self.analyzer.pose_head_estimator.reset()
            # **必须 dispatch**: suspend() 会把进行中的分集和"离开"收口, 返回
            # EpisodeEnd / AwayChange(False)。丢掉返回值 = 那段分心和离开时长凭空消失,
            # 还会在库里留下"有开头没结尾"的孤儿(实测 2026-09-17: 20:21 的分集孤儿、
            # 21:29 的 away 孤儿 —— 那天日报写着"离开座位 0 分钟")。
            # suspend() 不会产出 Nudge / EpisodeStart, 所以这里不发声、不弹窗、不截图。
            self.dispatch(self.sm.update(Observation(ts=mono, at=now),
                                         Policy(judging=False, enabled=enabled)))
            self._last_tick_mono = 0.0     # 恢复判定后重新开始量真实间隔
            return

        self.idle_since = mono
        if not self._ensure_camera():
            self._last_tick_mono = 0.0
            return

        ok, frame = self.cap.read()
        if not ok or frame is None:
            self.cap.release()
            self.cap = None
            self._log("camera_read_fail")
            self._last_tick_mono = 0.0
            return

        # 有帧、但画面里有没有信息? —— 这一问是 2026-09-27 那个 bug 的分界线。
        # 采集流死掉时 read() 返回的是 `ok=True` 的纯黑帧, 下面那句 `if not ok` 永远
        # 抓不到; 而纯黑帧喂给 Face/Pose 只会得到 0%, 于是"看不到"被记成"你走了"。
        reading = self.health.reading(frame)
        verdict = self.health.watch.observe(
            reading, mono,
            blank_seconds=self.cfg.detection.blank_seconds,
            blank_std=self.cfg.detection.blank_std)
        if verdict == framehealth.BLANK:
            # 没有画面: **不喂模型、不记监控、不判定**。既不算专注, 也不算分心,
            # 更不算离开 —— 这张图里根本没有"你在不在"这个信息。
            if self._blank_at is None:
                self._blank_at = now - timedelta(
                    seconds=self.health.watch.blank_for(mono))
            self._blank_tick(mono, now, reading, policy)
            return
        if verdict == framehealth.TRIP:
            self._no_picture(mono, now, reading, frame, policy, enabled)
            return
        if self._blank_at is not None:
            self._picture_back(mono, now)

        ts_ms = int((mono - self.t0) * 1000)
        feats = self.analyzer.analyze(frame, ts_ms, now=mono)
        self._last_frame = frame
        self._tick_count += 1
        self._face_count += 1 if feats.face_present else 0
        self._face_pct_recent = (self._face_count / max(1, self._tick_count)) * 100.0
        # 把**真实经过的秒数**一起记下来 —— "实际监控时长"要用它, 而不是
        # "tick 数 ÷ 配置里的 tick_hz"(那样改一次 tick_hz 就会回溯性改写全天统计,
        # 而且循环掉速时会多扣有效专注)。首帧没有上一帧, 用标称间隔兜底。
        dt = mono - self._last_tick_mono if self._last_tick_mono else 1.0 / max(0.5, self.cfg.general.tick_hz)
        self._last_tick_mono = mono
        self.store.coverage_tick(now, feats.pose_present, feats.face_present,
                                 judging=True, dt=min(dt, 5.0))

        obs = Observation(
            ts=mono, at=now,
            pose_present=feats.pose_present,
            yaw=feats.yaw, pitch=feats.pitch,
            yaw_std=feats.yaw_std, pitch_std=feats.pitch_std,
            blink_rate=feats.blink_rate, eye_closed_ratio=feats.eye_closed_ratio,
            idle_seconds=input_activity.idle_seconds(),
            title=title,
            pose_head=feats.pose_head,
        )
        self.dispatch(self.sm.update(obs, policy), frame)

        # 每 FLUSH_EVERY_SECONDS 落盘一次: 这台机器 4 天蓝屏两次(0x124 硬件错误),
        # 不能把数据全押在内存里 —— 一次蓝屏就等于那段时间凭空消失。
        # 间隔不能拉长: 它同时也是"写锁被监控线程攥住"的时长, 见 FLUSH_EVERY_SECONDS。
        if mono - self._flush_at > FLUSH_EVERY_SECONDS:
            self._flush_at = mono
            self.store.flush()

        # 每分钟打一行状态, 试跑时能立刻看出"它到底在不在工作"
        if mono - self._status_at > 60:
            rate = self._tick_count / max(1e-6, mono - self._status_at)
            face_pct = self._face_count / max(1, self._tick_count) * 100
            msg = (f"[{now:%H:%M:%S}] 判定中·{policy.block_name} | "
                   f"{rate:.1f} Hz | 人脸 {face_pct:.0f}%"
                   + ("  ⚠️ 看不清" if face_pct < 50 else ""))
            # Pose 弱证据单独写, 不混进下面的"分集中"里 —— 它只记录、不算分心,
            # 写成"分集中"会让人以为它在报警。看不清的时候它才是主角。
            if feats.pose_head is not None:
                msg += f" | Pose:{feats.pose_head.posture}"
            snap = self.sm.snapshot(mono)
            parts = []
            for key, val in snap.items():
                if not isinstance(val, dict) or key == SignalKind.POSE.value:
                    continue
                bits = []
                if val.get("in_episode"):
                    bits.append(f"分集中{val.get('elapsed', 0):.0f}s/L{val.get('level', 0)}")
                if val.get("muted_for", 0) > 0:
                    bits.append(f"静默剩{val['muted_for']:.0f}s")
                if bits:
                    parts.append(f"{key}:{'/'.join(bits)}")
            if parts:
                msg += " | " + " ".join(parts)
            # 掉速也要说出来: 采集流快死的时候最先露头的现象就是帧率塌下来
            # (2026-09-27 实测 5.0 -> 3.9 -> 2.9 -> 0.95 Hz, 然后才是黑帧)。
            if rate < self.cfg.general.tick_hz * 0.6 and self._tick_count >= 5:
                msg += "  ⚠️ 掉速"
            self._say(msg)
            self._status_reset(mono)

        if mono - self._summary_at > SUMMARY_EVERY_SECONDS:
            self._summary_at = mono
            self._print_summary(now)
            self.store.flush()

    def _status_reset(self, mono: float) -> None:
        self._status_at = mono
        self._tick_count = 0
        self._face_count = 0

    # ---- "摄像头没有画面"(和"你离开座位"是两件事) ----
    def _blank_tick(self, mono: float, now: datetime, reading, policy: Policy) -> None:
        """没有画面时的一帧: 只数帧率和每分钟报一次, **不产生任何判定**。

        帧率照实累计(它本身就是最重要的证据: 黑帧那次是 1/1.05s), 但人脸次数不加 ——
        分母里混进"根本没画"的帧只会让覆盖率这个数字失去意义。
        """
        self._tick_count += 1
        self._last_tick_mono = 0.0
        if mono - self._status_at <= 60:
            return
        rate = self._tick_count / max(1e-6, mono - self._status_at)
        self._say(f"[{now:%H:%M:%S}] ⚠️ 没有画面·{policy.block_name} | {rate:.1f} Hz | "
                  f"{reading.describe()} | 已 {self.health.watch.blank_for(mono):.0f} 秒")
        self._status_reset(mono)

    def _no_picture(self, mono: float, now: datetime, reading, frame,
                    policy: Policy, enabled: frozenset) -> None:
        """连续 `blank_seconds` 秒没有画面 -> 按**设备故障**处理, 并自动重开摄像头。

        三件事必须同时做到, 少一件就是踩铁律:
          1. **不许算成离开座位**: 这里走 suspend(), 把进行中的分集/离开收口。
             (dispatch 的返回值不能丢 —— 丢了就留下"有开头没结尾"的孤儿, 见上面注释。)
          2. **必须可见**: `camera_blank` 事件 + 当场存一张证据图 + 托盘变色/气泡。
             没有证据图的话, 下一次只能靠猜"是摄像头坏了还是我真的走了"。
          3. **必须自救**: 释放并重开采集流。实测这是唯一有效的解药 ——
             read() 自己永远不会好, 而让用户手动"让出+收回"就是把 bug 转嫁给人。
        """
        span = self.health.watch.blank_for(mono)
        rate = self._tick_count / max(1e-6, mono - self._status_at)
        detail = f"{reading.describe()}; 帧率 {rate:.1f} Hz"
        first = self._blank_count == 0
        self._blank_count += 1
        if first:
            self._log("camera_blank", at=now, detail=detail)
            self._say(f"⚠️ 摄像头**没有画面**({detail}) —— 这段时间**不算你离开座位**, "
                      f"正在自动重开摄像头 ...")
            self._save_no_picture(frame, now, reading)
        # 复位 Pose 弱证据的直立基准: 基准是"你最近的样子", 而这段时间我们什么都没看见。
        if self.analyzer is not None:
            self.analyzer.pose_head_estimator.reset()
        self.dispatch(self.sm.update(Observation(ts=mono, at=now),
                                     Policy(judging=False, enabled=enabled)))
        self._release_camera(None)          # 让出时的原因由上面那行说清楚了
        self._blank_reopens += 1
        delay = min(BLANK_REOPEN_SECONDS * (2 ** (self._blank_count - 1)),
                    BLANK_REOPEN_MAX_SECONDS)
        self.cam_next_retry = mono + delay
        self.health.reset()                 # 重开之后重新计时(刚打开的几帧本来就可能是黑的)
        self._last_tick_mono = 0.0
        self._log("camera_reopen", at=now, duration=delay,
                  detail=f"没有画面 {span:.0f} 秒, 第 {self._blank_count} 次自动重开")
        self._say(f"   (已经 {span:.0f} 秒没有画面; 自动重开摄像头, {delay:.0f} 秒后重试)")

    def _picture_back(self, mono: float, now: datetime) -> None:
        """画面回来了 —— 结账, 并说清楚这段空白有多长(它是日报里那一行的来源)。

        只有**真的判定成故障过**(`_blank_count > 0`)才记账: 摄像头刚打开时抖一两帧黑
        是正常的, 给它记一笔 `camera_blank_end` 只会往日报里塞一个没有意义的 0 分钟。
        """
        span = (now - self._blank_at).total_seconds() if self._blank_at else 0.0
        tripped = self._blank_count > 0
        reopens = self._blank_reopens
        self._blank_at = None
        self._blank_reopens = 0
        self._blank_count = 0
        if not tripped:
            return
        self._log("camera_blank_end", at=now, duration=span,
                  detail=f"自动重开 {reopens} 次")
        if span >= 60:
            self._say(f"✅ 摄像头画面恢复(此前 {span / 60:.1f} 分钟没有画面"
                      f" —— 那段时间不算你离开座位; 自动重开 {reopens} 次)")

    def _save_no_picture(self, frame, now: datetime, reading) -> None:
        """存一张"当时到底看到了什么"的证据图(黑帧那次存下来就是纯黑的下半张)。

        这是"漏报必须可见"的最后一道保险: 没有它, 事后无法区分
        "摄像头坏了"和"程序判定错了"。按 privacy 配置走, 失败绝不影响监控。
        """
        pv = self.cfg.privacy
        if not pv.save_captures:
            return
        try:
            path = capture.save_composite(
                self.cfg.capture_dir, at=now, frame_bgr=frame,
                width=pv.capture_width, screen=True, camera=frame is not None,
                note="NO PICTURE")
        except Exception as exc:  # noqa: BLE001 - 存图失败绝不能拖垮监控
            self.log.exception("no_picture.capture", exc)
            return
        if path is not None:
            self._log("capture", at=now, detail=str(path))
            self._say(f"   已存证据图: {path.name}({reading.describe()})")

    # ---- 动作派发 ----
    def dispatch(self, actions: list, frame=None) -> None:
        if actions:
            # 先把"状态机决定了什么"落到文件日志, 再执行。这样"该提醒却没提醒"能被一眼
            # 定位到是哪一环丢的: 状态机没发出 / 派发了但记账失败 / 提醒通道报错。
            self.log.write("派发: " + "; ".join(describe_action(a) for a in actions))
        for act in actions:
            if isinstance(act, Nudge):
                title = foreground.foreground_title() or ""
                # **先记账, 再提醒**: 提醒通道(声音/弹窗)出任何问题都不能丢掉这条记录。
                # 2026-09-17 那 3 次"该响却没响"的 L3 之所以无从查证, 就是因为顺序反了 ——
                # 记录写在提醒之后, 提醒一抛异常, 连"它本来想提醒"这件事都没留下。
                self._log("nudge", signal=act.signal.value, level=act.level,
                          evidence=act.evidence, block=act.block_name, detail=title)
                self._say(f"[{act.at:%H:%M:%S}] 🔔 提醒 L{act.level}"
                      f"({LEVEL_LABEL.get(act.level, '')}) "
                      f"{SIGNAL_LABEL[act.signal]}: {act.evidence}"
                      + (f" | 当前窗口: {title[:60]}" if title else ""))
                try:
                    self.notifier.buzz(act.level, sound=act.sound,
                                       label=f"{SIGNAL_LABEL[act.signal]} {act.evidence}")
                except Exception as exc:  # noqa: BLE001
                    self.log.exception("notifier.buzz", exc)
                if self._should_popup(act):
                    try:
                        self.ui.show(AlertRequest(
                            title=f"第 {act.level} 级提醒 · {SIGNAL_LABEL[act.signal]}",
                            body=act.evidence + (f"\n\n当前窗口: {title[:90]}" if title else ""),
                            signal=act.signal.value,
                            level=act.level,
                            # "离开太久"的弹窗不自动关闭: 你不在座位上, 得等你回来看到它
                            auto_close_seconds=(0.0 if act.signal is SignalKind.AWAY
                                                else 30.0),
                            sound_hint="" if act.sound else "安静时段: 不发声, 仅弹窗",
                        ))
                    except Exception as exc:  # noqa: BLE001
                        self.log.exception("ui.show", exc)
            elif isinstance(act, EpisodeStart):
                self._say(f"[{act.at:%H:%M:%S}] ▶ 开始分心"
                      f"{'(只记录)' if act.record_only else ''}: {act.evidence}")
                self._log("episode_start", signal=act.signal.value,
                          evidence=act.evidence, detail="record_only" if act.record_only else None)
                # **只记录的分集不截图**。截图的用途是"分心现场证据"(事后调参时看当时
                # 在看什么/什么姿态)。而 POSE 分集只是"看不到脸时 Pose 觉得你在低头",
                # 背单词时段的 phone 也走只记录 —— 给它们存图既是隐私问题(低头写题会被
                # 拍一堆), 也会把 captures/ 塞满, 还会让人误以为"它又报警了"。
                # (2026-09-18 真机验证时抓到的: 一段 33 秒的 pose 分集就存了一张图。)
                if not act.record_only:
                    self._capture(act, frame)
            elif isinstance(act, EpisodeEnd):
                self._say(f"[{act.at:%H:%M:%S}] ⏹ 分心结束, 持续 {act.duration:.0f} 秒"
                      f"(最高 L{act.max_level})")
                flags = [f for f, on in (("record_only", act.record_only),
                                         ("wrong", act.wrong)) if on]
                self._log_span("episode_end", signal=act.signal.value,
                               level=act.max_level, evidence=act.evidence,
                               detail=",".join(flags) or None,
                               start_at=act.at - timedelta(seconds=act.duration),
                               end_at=act.at, duration=act.duration)
            elif isinstance(act, AwayChange):
                if act.away:
                    self._say(f"[{act.at:%H:%M:%S}] 💤 离开座位(只记录, 不提醒)")
                    self._log("away_start")
                else:
                    self._say(f"[{act.at:%H:%M:%S}] 👋 回到座位(离开 {act.duration / 60:.1f} 分钟)")
                    self._log_span("away_end",
                                   start_at=act.at - timedelta(seconds=act.duration),
                                   end_at=act.at, duration=act.duration)

    def _capture(self, act: EpisodeStart, frame) -> None:
        """分心开始时存一张"屏幕+摄像头"拼接图(按 privacy 配置, 7 天自动删)。

        截图失败绝不能影响监控, 所以整段包在 try 里, 而且失败只打一行提示。
        """
        pv = self.cfg.privacy
        if not pv.save_captures:
            return
        path = capture.save_composite(
            self.cfg.capture_dir, at=act.at, frame_bgr=frame,
            width=pv.capture_width, screen=True, camera=frame is not None,
            note=act.signal.value)
        if path is not None:
            self._log("capture", signal=act.signal.value, detail=str(path))
            self._say(f"   已存证据图: {path.name}")

    # ---- 弹窗策略 ----
    def _should_popup(self, act: Nudge) -> bool:
        """什么时候弹窗。

        L1 级(高可信信号刚触发)只是一声轻响, 不该弹窗打断你;
        但**安静时段弹窗是唯一的提醒通道**, 所以那时候必须弹。
        "离开太久"也一定要弹: 响的时候你不在座位上, 回来时得能看见它凭什么响过。
        """
        if not self.cfg.reminder.show_evidence:
            return False
        if act.signal is SignalKind.AWAY:
            return True
        return (not act.sound) or act.level >= 2

    def _drain_ui(self, mono: float) -> None:
        for kind, value in self.ui.drain():
            if kind == "wrong":
                try:
                    sk = SignalKind(value)
                except ValueError:
                    continue
                # report_wrong 会返回"把这段误报结掉"的动作, **必须 dispatch** ——
                # 否则它只是静默了信号, 那段误报照样计进分心时长。
                self.dispatch(self.sm.report_wrong(sk, mono, datetime.now()))
                minutes = self.cfg.reminder.wrong_feedback_mute_seconds // 60
                self._say(f"   判定错了 -> {SIGNAL_LABEL[sk]} 静默 {minutes} 分钟"
                      f"(这一段已按误报剔除, 不再扣你的专注; 不自动调参, 攒够样本后人工调阈值)")
                self._log("wrong", signal=value)
            elif kind == "reason":
                self.pause(value, datetime.now())
            elif kind == "reason_cancelled":
                self._say("   已取消暂停(没写理由就不给暂停)")
            elif kind == "ui_error":
                # 弹窗通道**自己**炸了 = 提醒可能根本没弹出来。这是"漏报", 必须一眼可见:
                # 以前它落进下面的 else 分支, 被记成 alert_dismissed(等同于"用户点了我回来了"),
                # 事件流里完全看不出 UI 已经坏了(实测: TclError 被记成 alert_dismissed)。
                self._say(f"⚠️ 提醒窗口出错(提醒可能没弹出来): {value}")
                self._log("ui_error", detail=value)
            elif kind == "dismissed":
                self._log("alert_dismissed", signal=value)
            else:
                # 未知类型也如实记成它自己 —— 绝不能一律当成"用户点了我回来了"
                self._log(kind or "ui_event", signal=value)

    # ---- 配置热重载 ----
    def _maybe_reload_config(self, mono: float, now: datetime) -> None:
        if mono - self._cfg_checked < CONFIG_RELOAD_SECONDS:
            return
        self._cfg_checked = mono
        try:
            mtime = self.cfg.path.stat().st_mtime
        except OSError:
            return
        if mtime == self._cfg_mtime:
            return
        first = self._cfg_mtime == 0.0
        self._cfg_mtime = mtime
        if first:
            return
        try:
            new_cfg = Config.load(self.cfg.path)
            new_cal = Calibration.load()
        except Exception as exc:  # noqa: BLE001
            self._say(f"⚠️ 配置重载失败(继续用旧的): {exc}")
            return
        # 分辨率/设备改变需要重开摄像头
        if (new_cfg.general.camera_index != self.cfg.general.camera_index
                or new_cfg.general.camera_width != self.cfg.general.camera_width):
            self._release_camera()
        self.cfg = new_cfg
        self.cal = new_cal
        self.sm.cfg = new_cfg
        self.sm.cal = new_cal
        if self.analyzer is not None:
            self.analyzer.cal = new_cal
            # Pose 弱证据的阈值也要跟着热重载, 否则改了 config.toml 却只有重启才生效
            est = self.analyzer.pose_head_estimator
            for key, val in self._pose_head_kwargs().items():
                setattr(est, key, val)
        self._say(f"[{now:%H:%M:%S}] 配置已热重载(启用信号: {new_cfg.detection.enabled_signals})")

    # ---- 每日任务 ----
    def _daily_jobs(self, now: datetime) -> None:
        day = now.date().isoformat()
        if self._cleanup_day != day:
            self._cleanup_day = day
            try:
                removed = capture.cleanup(self.cfg.capture_dir,
                                          self.cfg.privacy.capture_retention_days)
                if removed:
                    self._say(f"清理了 {removed} 张超过 "
                          f"{self.cfg.privacy.capture_retention_days} 天的截图")
            except Exception as exc:  # noqa: BLE001
                self._say(f"截图清理失败(忽略): {exc}")

        if not self.cfg.report.daily_report or self._report_day == day:
            return
        if now.time() < self.cfg.report.report_time:
            return
        self._report_day = day
        # 生成日报时如果人还暂停着, 先把**已经过去的那段**结账再重开一段 ——
        # 否则 21:00 暂停、21:30 出日报, 那 30 分钟和理由都不在库里(实测踩到)。
        # reopen=True 让暂停继续计时, 后面真的恢复时再结第二段。
        try:
            self._close_pause(now, reopen=True)
        except Exception as exc:  # noqa: BLE001 - 结账失败不能挡住日报
            self._say(f"暂停结账失败(忽略): {type(exc).__name__}: {exc}")
        try:
            path = write_report(self.cfg, self.store, day)
            self._say(f"[{now:%H:%M:%S}] 📄 今日日报已生成: {path}")
            self._log("report", detail=str(path))
            self._print_summary(now)
        except Exception as exc:  # noqa: BLE001
            self._say(f"日报生成失败(忽略): {type(exc).__name__}: {exc}")

    # ---- 摘要 ----
    def _warn_unfinished_episodes(self) -> None:
        """上一轮是不是"在分心过程中被杀掉/崩溃"的? 启动时必须说出来。

        那段分心没有 episode_end, 于是既不计时长也不计次数, 而提醒已经响过了 ——
        不说的话就是"漏报且不可见", 正好踩中第二条铁律。日报里也会写, 但那要等到
        21:30; 启动时先说一句, 你当场就知道上次不是正常退出。
        """
        try:
            st = self.store.day_stats(datetime.now().date().isoformat(),
                                      tick_hz=self.cfg.general.tick_hz)
        except Exception:  # noqa: BLE001 - 启动提示绝不能挡住监控
            return
        if st.unfinished_episodes:
            self._say(f"⚠️ 今天有 {st.unfinished_episodes} 段分心没有收尾"
                      f"(上次运行时在分心过程中被关掉/崩溃) —— 时长不明, "
                      f"不会计入日报的分心时长")

    def _print_summary(self, now: datetime) -> None:
        st = self.store.day_stats(now.date().isoformat(),
                                  tick_hz=self.cfg.general.tick_hz)
        self._say(f"[{now:%H:%M:%S}] —— 今日进度 ——")
        self._say(f"    有效专注 {st.focus_seconds / 3600:.2f} h | 分心 {st.distract_seconds / 60:.1f} min "
              f"({st.episodes} 次) | 提醒 {st.nudges} 次")
        self._say(f"    离开 {st.away_seconds / 60:.1f} min | 看不清 {st.blind_seconds / 60:.1f} min "
              f"| 人脸覆盖率 {st.coverage * 100:.0f}%")
        if st.camera_busy_seconds:
            self._say(f"    ⚠️ 因摄像头占用漏检 {st.camera_busy_seconds / 60:.1f} min")
        if st.camera_blank_seconds:
            # 必须和"离开座位"并排看: 它以前就是被错算成离开的那一段。
            self._say(f"    ⚠️ 摄像头没有画面 {st.camera_blank_seconds / 60:.1f} min"
                      f"({st.camera_blank_count} 次, 自动重开 {st.camera_reopen_count} 次)"
                      f" —— 不算离开座位")
        if st.unfinished_episodes:
            # 和上面那行"分心 N 次"并排看才会发现问题, 所以必须挨着写出来
            self._say(f"    ⚠️ 另有 {st.unfinished_episodes} 段分心没有收尾(时长不明, 未计入)")

    def close(self) -> None:
        """释放资源。正常退出和测试都走这里, 别只关一半。

        (踩过两次: SQLite 连接和日志文件句柄各自漏关, 都会让临时目录删不掉、
        退出时文件残留 —— 所以统一收口。)
        """
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.log.close()
        except Exception:  # noqa: BLE001
            pass

    def shutdown(self) -> None:
        self._say("\n正在退出 ...")
        # 退出前把**暂停**也结账: 否则"暂停中直接关掉托盘"会让整段暂停时长和理由消失
        # (day_stats / pause_reasons 只认 pause_end)。实测 2026-09-18 21:00 那次就是。
        try:
            if self._close_pause(datetime.now(), reopen=False):
                self.paused = False
        except Exception as exc:  # noqa: BLE001 - 退出路径上绝不能再往外抛
            self.log.exception("shutdown.pause", exc)
        # **退出前必须把进行中的分集收口**。否则这段分心既不计时长也不计次数, 而提醒
        # 已经响过了 —— 日报就会出现"提醒 2 次 / 分心 0 分钟"这种自相矛盾的行
        # (2026-09-20 实测: 10:49:31 那次分心刚开 16 秒, 程序就被关掉了)。
        # suspend() 只会产出 EpisodeEnd / AwayChange(False), 不会有 Nudge 或
        # EpisodeStart, 所以走 dispatch 是安全的: 不发声、不弹窗、不截图。
        try:
            self.dispatch(self.sm.suspend(time.monotonic(), datetime.now(), "shutdown"))
        except Exception as exc:  # noqa: BLE001 - 退出路径上绝不能再往外抛
            self.log.exception("shutdown.suspend", exc)
        # 退出时也要把"没有画面"这一段收口 —— 否则那段时间的长度永远停在"未知",
        # 而它恰恰是"我明明在学却没记上"的那种时间, 必须能进日报。
        try:
            if self._blank_at is not None:
                self._picture_back(time.monotonic(), datetime.now())
        except Exception as exc:  # noqa: BLE001
            self.log.exception("shutdown.blank", exc)
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        if self.analyzer is not None:
            self.analyzer.close_async()   # 同步 close 要 40 多秒, 必须异步
        self.store.flush()
        self._say("已退出。")
        self.close()
