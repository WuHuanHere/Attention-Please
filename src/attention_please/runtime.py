"""运行时: 把摄像头/感知/状态机/提醒/存储接起来跑。

设计上刻意保持"薄": 所有判定逻辑都在 signals.py(纯逻辑、有单测),
这里只做编排 —— 读帧、填策略、派发动作、记账。

几个实测逼出来的决定:
  - **摄像头开着就别关**: 每帧重开摄像头会闪指示灯、触发驱动问题, 而且慢。
    只有"长时间不判定"(默认 120 秒)才释放, 免得你看视频时摄像头灯一直亮着。
  - **摄像头被占用要留痕**: 打不开就每 30 秒重试, 并把这段时间记成 camera_busy,
    日报里会明确写出"今天因摄像头占用漏检 X 分钟"。漏报必须可见。
  - **人脸覆盖率逐分钟入库**: 覆盖率低的时候"看手机/发呆"其实是瞎的,
    这段时间要记成"看不清", 既不算专注也不当分心。
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import capture, foreground, input_activity
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

LOCK_PORT = 47731          # 单实例: 这个端口被占用说明已经有一个在跑
CAMERA_RETRY_SECONDS = 30
CAMERA_RELEASE_AFTER = 120
CONFIG_RELOAD_SECONDS = 60
SUMMARY_EVERY_SECONDS = 300


@dataclass
class Counters:
    judging_started: datetime | None = None
    distract_seconds: float = 0.0
    pause_seconds: float = 0.0
    away_seconds: float = 0.0
    nudges: int = 0
    episodes: int = 0


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
        self.counters = Counters()
        self.analyzer: FrameAnalyzer | None = None
        self.cap = None
        self.cam_info: CameraInfo | None = None
        self.cam_fail_since: datetime | None = None
        self.cam_next_retry: float = 0.0
        self.idle_since: float = time.monotonic()
        self.running = True
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        # --- 暂停 / 手动学习 ---
        self.paused = False
        self.pause_started: datetime | None = None
        self.pause_reason = ""
        self.manual_until: datetime | None = None
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

    # ------------------------------------------------------------------
    # 策略: 时间表 + 安静时段 + 启用信号
    # ------------------------------------------------------------------
    def _policy(self, at: datetime) -> Policy:
        enabled = frozenset(self.cfg.detection.enabled_signals)
        with self._lock:
            paused = self.paused
            manual_until = self.manual_until
        if paused:
            return Policy(judging=False, enabled=enabled, block_name="已暂停")
        # 作息表优先于"手动学习": 否则你在"英语单词"块里点手动学习,
        # 会把这个块的 allow_phone=true 冲掉, 背单词反而被当成玩手机。
        block = self.cfg.schedule.block_at(at)
        if block is None and manual_until is not None and at < manual_until:
            return Policy(judging=True, allow_phone=False,
                          quiet=self.cfg.quiet_hours.is_quiet(at),
                          block_name="手动学习", enabled=enabled)
        if block is None:
            return Policy(judging=False, enabled=enabled, block_name="")
        return Policy(
            judging=True,
            allow_phone=block.allow_phone,
            quiet=self.cfg.quiet_hours.is_quiet(at),
            block_name=block.name,
            enabled=enabled,
        )

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
            duration = (at - self.pause_started).total_seconds() if self.pause_started \
                else 0.0
            reason = self.pause_reason
            self.paused = False
            self.pause_started = None
            self.pause_reason = ""
        self.counters.pause_seconds += duration
        self._say(f"[{at:%H:%M:%S}] ▶ 恢复监控(暂停 {duration / 60:.1f} 分钟:{reason})")
        self._log("pause_end", at=at, duration=duration, detail=reason)

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
            self.cam_next_retry = now + CAMERA_RETRY_SECONDS
            return False
        # 只留 1 帧缓冲, 否则会读到几秒前的旧画面(必须用 CAP_PROP_BUFFERSIZE, 别写魔数)
        set_buffer_size(cap, 1)
        self.cap = cap
        self.cam_info = CameraInfo(index=self.cfg.general.camera_index, opened=True,
                                   backend=backend,
                                   width=int(cap.get(3)), height=int(cap.get(4)))
        if self.cam_fail_since is not None:
            dur = (datetime.now() - self.cam_fail_since).total_seconds()
            self._log("camera_busy_end", duration=dur)
            self._say(f"✅ 摄像头恢复(此前漏检 {dur / 60:.1f} 分钟)")
            self.cam_fail_since = None
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

    def _release_camera(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None
            self._say("(长时间不在判定区间, 已释放摄像头)")

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------
    def _log(self, kind: str, *, at: datetime | None = None, signal: str | None = None,
             level: int | None = None, duration: float | None = None,
             evidence: str | None = None, block: str | None = None,
             detail: str | None = None) -> None:
        self.store.event(kind, at=at, signal=signal, level=level, duration=duration,
                         evidence=evidence, block=block, detail=detail)

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
        policy = self._policy(now)
        enabled = frozenset(self.cfg.detection.enabled_signals)

        if not policy.judging:
            if self.cap is not None and mono - self.idle_since > CAMERA_RELEASE_AFTER:
                self._release_camera()
            # Pose 弱证据的基准是"你最近的样子"。待机/休息久了它就过期了 ——
            # 拿一小时前(甚至午休前)的姿势当基准, 等于在猜。清掉, 恢复判定后重新学。
            if self.analyzer is not None:
                self.analyzer.pose_head_estimator.reset()
            self.sm.update(Observation(ts=mono, at=now), Policy(judging=False,
                                                               enabled=enabled))
            return

        self.idle_since = mono
        if not self._ensure_camera():
            return

        ok, frame = self.cap.read()
        if not ok or frame is None:
            self.cap.release()
            self.cap = None
            self._log("camera_read_fail")
            return

        ts_ms = int((mono - self.t0) * 1000)
        feats = self.analyzer.analyze(frame, ts_ms, now=mono)
        self._last_frame = frame
        self._tick_count += 1
        self._face_count += 1 if feats.face_present else 0
        self._face_pct_recent = (self._face_count / max(1, self._tick_count)) * 100.0
        self.store.coverage_tick(now, feats.pose_present, feats.face_present,
                                 judging=True)

        obs = Observation(
            ts=mono, at=now,
            pose_present=feats.pose_present,
            yaw=feats.yaw, pitch=feats.pitch,
            yaw_std=feats.yaw_std, pitch_std=feats.pitch_std,
            blink_rate=feats.blink_rate, eye_closed_ratio=feats.eye_closed_ratio,
            idle_seconds=input_activity.idle_seconds(),
            title=foreground.foreground_title(),
            pose_head=feats.pose_head,
        )
        self.dispatch(self.sm.update(obs, policy), frame)

        # 每 30 秒落盘一次: 这台机器 4 天蓝屏两次(0x124 硬件错误), 不能把 5 分钟的数据
        # 全押在内存里 —— 一次蓝屏就等于那 5 分钟凭空消失。
        if mono - self._flush_at > 30:
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
            self._say(msg)
            self._status_at = mono
            self._tick_count = 0
            self._face_count = 0

        if mono - self._summary_at > SUMMARY_EVERY_SECONDS:
            self._summary_at = mono
            self._print_summary(now)
            self.store.flush()

    # ---- 动作派发 ----
    def dispatch(self, actions: list, frame=None) -> None:
        if actions:
            # 先把"状态机决定了什么"落到文件日志, 再执行。这样"该提醒却没提醒"能被一眼
            # 定位到是哪一环丢的: 状态机没发出 / 派发了但记账失败 / 提醒通道报错。
            self.log.write("派发: " + "; ".join(describe_action(a) for a in actions))
        for act in actions:
            if isinstance(act, Nudge):
                self.counters.nudges += 1
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
                self.counters.episodes += 1
                self.counters.distract_seconds += act.duration
                self._say(f"[{act.at:%H:%M:%S}] ⏹ 分心结束, 持续 {act.duration:.0f} 秒"
                      f"(最高 L{act.max_level})")
                self._log("episode_end", signal=act.signal.value, level=act.max_level,
                          duration=act.duration, evidence=act.evidence)
            elif isinstance(act, AwayChange):
                if act.away:
                    self._say(f"[{act.at:%H:%M:%S}] 💤 离开座位(只记录, 不提醒)")
                    self._log("away_start")
                else:
                    self.counters.away_seconds += act.duration
                    self._say(f"[{act.at:%H:%M:%S}] 👋 回到座位(离开 {act.duration / 60:.1f} 分钟)")
                    self._log("away_end", duration=act.duration)

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
                self.sm.report_wrong(sk, mono)
                minutes = self.cfg.reminder.wrong_feedback_mute_seconds // 60
                self._say(f"   判定错了 -> {SIGNAL_LABEL[sk]} 静默 {minutes} 分钟"
                      f"(已入库; 不自动调参, 攒够样本后人工调阈值)")
                self._log("wrong", signal=value)
            elif kind == "reason":
                self.pause(value, datetime.now())
            elif kind == "reason_cancelled":
                self._say("   已取消暂停(没写理由就不给暂停)")
            else:
                self._log("alert_dismissed", signal=value)

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
        try:
            path = write_report(self.cfg, self.store, day)
            self._say(f"[{now:%H:%M:%S}] 📄 今日日报已生成: {path}")
            self._log("report", detail=str(path))
            self._print_summary(now)
        except Exception as exc:  # noqa: BLE001
            self._say(f"日报生成失败(忽略): {type(exc).__name__}: {exc}")

    # ---- 摘要 ----
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
        if self.cap is not None:
            self.cap.release()
            self.cap = None
        if self.analyzer is not None:
            self.analyzer.close_async()   # 同步 close 要 40 多秒, 必须异步
        self.store.flush()
        self._say("已退出。")
        self.close()
