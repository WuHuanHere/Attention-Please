"""观测特征 -> 信号 -> 分集(episode) -> 分级提醒。

这是整个程序的心脏, 而且是**纯逻辑**: 输入是一帧观测(数字), 输出是一串动作事件。
不碰摄像头、不碰 MediaPipe、不碰声音, 所以可以完全用假数据单测。

已确认的判定口径:
  切到娱乐窗口(screen)  高可信  连续 screen_continuous_seconds 秒    可升到 max_level_high
  看手机(phone)          中可信  连续 phone_continuous_seconds 秒     可升到 max_level_mid
  发呆(daze)             低可信  连续 daze_continuous_seconds 秒      可升到 max_level_low
  离开(away)             ——      Pose 丢失 away_seconds 秒           只记录不提醒

三条硬约束:
  1. 误报最烦 -> 门槛(连续时长) + 迟滞(release) + 冷却(cooldown) 三重抑制;
  2. 升级力度按可信度封顶 -> 推断出来的信号不许循环播报;
  3. 点了"判定错了" -> 该信号静默 wrong_feedback_mute_seconds 秒, 且不自动调参。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .config import Calibration, Config
from .wordlist import TitleVerdict, classify


class SignalKind(str, Enum):
    SCREEN = "screen"
    PHONE = "phone"
    DAZE = "daze"
    # 离开座位太久。它**不走 enabled_signals 开关**, 由 away_reminder_seconds 控制
    # (0 = 关闭), 因为"人不在"和"人在但分心"是两件不同的事。
    AWAY = "away"


CONFIDENCE: dict[SignalKind, str] = {
    SignalKind.SCREEN: "high",
    SignalKind.PHONE: "mid",
    SignalKind.DAZE: "low",
    SignalKind.AWAY: "low",      # 上限 1 声 -> 一次离开只会响一次, 不会连环叫
}

SIGNAL_LABEL: dict[SignalKind, str] = {
    SignalKind.SCREEN: "切到了娱乐窗口",
    SignalKind.PHONE: "在看手机",
    SignalKind.DAZE: "疑似发呆",
    SignalKind.AWAY: "离开座位太久",
}


# --------------------------------------------------------------------------
# 输入 / 输出
# --------------------------------------------------------------------------
@dataclass
class Observation:
    """一帧观测。ts 用单调秒(时间差计算), at 用墙上时间(写库/展示)。"""

    ts: float
    at: datetime
    pose_present: bool = False
    yaw: float | None = None          # 度, 向右为正
    pitch: float | None = None        # 度, 低头为正
    yaw_std: float | None = None      # 发呆窗口内的波动
    pitch_std: float | None = None
    blink_rate: float | None = None   # 次/分
    eye_closed_ratio: float | None = None
    idle_seconds: float = 0.0         # 键鼠空闲时长
    title: str | None = None          # 前台窗口标题


@dataclass
class Policy:
    """当前上下文策略, 由 runtime 按时间表和设置填。"""

    judging: bool = True          # 是否处于判定状态(时间表外/暂停时为 False)
    allow_phone: bool = False     # 该时段"看手机"只记录不报警
    quiet: bool = False           # 安静时段: 不发声, 只弹窗
    block_name: str = ""
    # 只启用这些信号。M3 试跑先只开 screen(最可信、最不容易误报), 试跑满意再开其它。
    enabled: frozenset[str] = frozenset({"screen", "phone", "daze"})


@dataclass
class SignalEval:
    active: bool
    evidence: str = ""


@dataclass
class Nudge:
    signal: SignalKind
    level: int
    evidence: str
    sound: bool
    at: datetime
    ts: float
    block_name: str = ""


@dataclass
class EpisodeStart:
    signal: SignalKind
    evidence: str
    at: datetime
    ts: float
    record_only: bool = False


@dataclass
class EpisodeEnd:
    signal: SignalKind
    at: datetime
    ts: float
    duration: float
    max_level: int
    record_only: bool
    evidence: str = ""


@dataclass
class AwayChange:
    away: bool
    at: datetime
    ts: float
    duration: float = 0.0


Action = Nudge | EpisodeStart | EpisodeEnd | AwayChange


# --------------------------------------------------------------------------
# 信号判定(纯函数, 好测)
# --------------------------------------------------------------------------
def evaluate(obs: Observation, cfg: Config, cal: Calibration) -> dict[SignalKind, SignalEval]:
    det = cfg.detection
    out: dict[SignalKind, SignalEval] = {}

    # --- 切窗口: 只看窗口标题, 但**必须在"你确实看着屏幕"时才判** ---
    #
    # 2026-09-17 的实测教训(误报): 你低头写题、把 QQ / B 站晾在前台, 摄像头看不到脸 ->
    # 旧逻辑把 `obs.pitch is None`(拿不到头姿)当成了"你在看屏幕" -> 一路误报。
    # 现在的口径回到你最初的设计(**低头 = 纸笔模式, 不判窗口**):
    #   - 必须能看到脸(拿得到头姿)  -> 看不到你就不知道你在看什么, 不猜;
    #   - 头没有低下去(pitch < 阈值) -> 否则你在看纸/写字, 窗口是谁在前台与你无关。
    # 代价是"靠回椅背、脸出画面地看视频"会漏判 —— 漏报比误报好, 而且覆盖率会把这段时间
    # 记成"看不清"写进日报, 不会假装你在专注。
    face_visible = obs.pitch is not None
    head_up = face_visible and obs.pitch < cal.book_pitch_deg
    match = classify(obs.title, cfg.wordlists.whitelist, cfg.wordlists.blacklist)
    screen_active = (obs.pose_present and head_up and match.verdict is TitleVerdict.DISTRACT)
    if screen_active:
        idle_note = (f"; 已 {obs.idle_seconds:.0f} 秒没有键鼠操作"
                     if obs.idle_seconds >= 60 else "")
        evidence = f"窗口标题命中黑名单「{match.matched}」(你在看屏幕{idle_note})"
    else:
        evidence = ""
    out[SignalKind.SCREEN] = SignalEval(screen_active, evidence)

    # --- 看手机: 头向右偏(手机架在右手边, 摄像头拍不到手机本身) ---
    phone_active = (
        obs.pose_present
        and obs.yaw is not None
        and obs.yaw > cal.phone_yaw_deg
    )
    out[SignalKind.PHONE] = SignalEval(
        phone_active,
        f"头向右偏 {obs.yaw:.0f}°(阈值 {cal.phone_yaw_deg:.0f}°)" if phone_active else "",
    )

    # --- 发呆: 只在低头(看书)姿态下判定; 需要"头没动" + "眨眼变少"两个证据 ---
    head_down = obs.pitch is not None and obs.pitch >= cal.book_pitch_deg
    not_phone = obs.yaw is None or obs.yaw <= cal.phone_yaw_deg
    still = (
        obs.yaw_std is not None
        and obs.pitch_std is not None
        and obs.yaw_std < det.daze_yaw_std_deg
        and obs.pitch_std < det.daze_pitch_std_deg
    )
    if obs.blink_rate is None:
        blink_low = True  # 拿不到眨眼数据时不因此否决, 由"头没动"单独承担
        blink_txt = "眨眼数据不可用"
    else:
        blink_low = obs.blink_rate < cal.blink_rate_per_min * (1.0 - det.daze_blink_drop_ratio)
        blink_txt = f"眨眼 {obs.blink_rate:.0f}/分(基线 {cal.blink_rate_per_min:.0f})"
    daze_active = obs.pose_present and head_down and not_phone and still and blink_low
    out[SignalKind.DAZE] = SignalEval(
        daze_active,
        f"头部静止(偏航波动 {obs.yaw_std:.1f}°, 俯仰波动 {obs.pitch_std:.1f}°), {blink_txt}"
        if daze_active
        else "",
    )
    return out


# --------------------------------------------------------------------------
# 状态机
# --------------------------------------------------------------------------
RELEASE_SECONDS = 5.0  # 信号消失多少秒才算这次分心结束(迟滞, 防抖)


@dataclass
class _Track:
    active_since: float | None = None
    last_active_ts: float | None = None
    episode_start: float | None = None   # 首次判定的时刻(升级阶梯从这里算)
    signal_start: float | None = None    # 分心本体的起点(时长从这里算)
    evidence: str = ""
    level_emitted: int = 0
    max_level: int = 0
    record_only: bool = False
    last_episode_end: float | None = None
    # 冷却的锚点是"上一次真的响过的时刻"(不是上一次分心结束):
    # 否则一集接一集地切窗口会把冷却无限顺延, 变成"18 分钟一声不吭"。
    last_nudge_ts: float | None = None
    forced: bool = False                 # 这一集是否已经"强行打破静默"过
    muted_until: float = 0.0


class FocusStateMachine:
    def __init__(self, cfg: Config, cal: Calibration):
        self.cfg = cfg
        self.cal = cal
        self.tracks: dict[SignalKind, _Track] = {k: _Track() for k in SignalKind}
        self.away_since: float | None = None
        self.away_at: datetime | None = None
        self.away_reported = False
        self.resume_until: float = 0.0
        self.suspended = False

    # ---- 对外 ----
    def max_level_for(self, signal: SignalKind) -> int:
        r = self.cfg.reminder
        return {
            "high": r.max_level_high,
            "mid": r.max_level_mid,
            "low": r.max_level_low,
        }[CONFIDENCE[signal]]

    def threshold_for(self, signal: SignalKind) -> float:
        d = self.cfg.detection
        return {
            SignalKind.SCREEN: d.screen_continuous_seconds,
            SignalKind.PHONE: d.phone_continuous_seconds,
            SignalKind.DAZE: d.daze_continuous_seconds,
            SignalKind.AWAY: d.away_reminder_seconds,
        }[signal]

    def report_wrong(self, signal: SignalKind, now_ts: float) -> None:
        """用户点了"判定错了": 立刻结束分集, 该信号静默一段时间, 并留给调参用。"""
        self.tracks[signal].muted_until = now_ts + self.cfg.reminder.wrong_feedback_mute_seconds

    def suspend(self, now_ts: float, now_at: datetime, reason: str = "suspend") -> list[Action]:
        """暂停/离开时间表/摄像头被占用: 关掉所有进行中的分集, 不报警。"""
        actions: list[Action] = []
        # **离开状态必须在这里收口**。否则这条时间线会算错:
        #   11:20 离开(已报告) -> 11:30 时间表结束(不再判定) -> 午休 -> 13:00 回来
        # 回来时算出的时长是 1 小时 40 分, 其中 1.5 小时是午休 —— 而午休本来就不在
        # "监控时间"里, 日报却会把这段从未计入的时间又从"有效专注"里扣掉一次。
        # 所以: 判定一停, 就把离开按当前时刻结掉; 恢复判定后如果人还不在, 90 秒后会重新开一段。
        if self.away_since is not None and self.away_reported:
            actions.append(AwayChange(False, now_at, now_ts, now_ts - self.away_since))
        self.away_since = None
        self.away_at = None
        self.away_reported = False
        for kind, tr in self.tracks.items():
            if tr.episode_start is not None:
                start = tr.signal_start if tr.signal_start is not None else tr.episode_start
                actions.append(EpisodeEnd(kind, now_at, now_ts, now_ts - start,
                                          tr.max_level, tr.record_only, tr.evidence))
                tr.episode_start = None
                tr.signal_start = None
                tr.level_emitted = 0
            tr.active_since = None
            tr.last_active_ts = None
        self.suspended = True
        return actions

    def resume(self, now_ts: float) -> None:
        self.suspended = False
        self.resume_until = now_ts + self.cfg.detection.resume_grace_seconds

    # ---- 主循环 ----
    def update(self, obs: Observation, policy: Policy) -> list[Action]:
        actions: list[Action] = []
        if not policy.judging:
            return self.suspend(obs.ts, obs.at, policy.block_name or "outside-schedule")

        self._update_away(obs, actions)
        # ⚠️ 这里**不能**因为"坐下宽限期"就 return。
        # 曾经的写法是 `if obs.ts < self.resume_until: return actions`, 而 resume_until 会被
        # 每一个"检测到人"的 tick 推成 now+20 秒 —— 于是只要 Pose 在通/断之间抖动
        # (低头、转头、光线差时极常见), 信号判定几乎永远被跳过:
        # 不升级、不发提醒、连"回到座位"都不判; 而分集因为间隙没超过迟滞会一直挂着
        # (所以出现过 209 秒、200 秒的超长分集)。
        # 现在的做法: **记账永远照跑**, 宽限期只用来"别刚坐下就念你"(见 _update_signal 的 muted)。
        evals = evaluate(obs, self.cfg, self.cal)
        for kind in SignalKind:
            if kind is SignalKind.AWAY:
                continue                      # 单独处理(见下), 不受 enabled_signals 约束
            if kind.value not in policy.enabled:
                # 未启用的信号连记录都不做, 免得试跑数据里混进还没验证过的东西
                continue
            actions.extend(self._update_signal(kind, evals[kind], obs, policy))

        # --- 离开太久 ---
        # 门槛就是"离开多少秒才提醒", 级别上限是 low(=1 声) -> 一次离开只提醒一次;
        # 声音/弹窗由 policy.quiet 决定(安静时段自动只弹窗)。
        limit = self.cfg.detection.away_reminder_seconds
        if limit > 0:
            active = not obs.pose_present
            evidence = ""
            if active and self.away_since is not None:
                mins = (obs.ts - self.away_since) / 60.0
                evidence = (f"已经离开座位 {mins:.0f} 分钟"
                            f"(阈值 {limit / 60:.0f} 分钟)")
            actions.extend(self._update_signal(SignalKind.AWAY,
                                               SignalEval(active, evidence),
                                               obs, policy))
        return actions

    # ---- 离开座位 ----
    def _update_away(self, obs: Observation, actions: list[Action]) -> None:
        det = self.cfg.detection
        if not obs.pose_present:
            if self.away_since is None:
                self.away_since = obs.ts
                self.away_at = obs.at
            elif not self.away_reported and obs.ts - self.away_since >= det.away_seconds:
                self.away_reported = True
                actions.append(AwayChange(True, obs.at, obs.ts, 0.0))
        else:
            returned = False
            if self.away_since is not None and self.away_reported:
                actions.append(AwayChange(False, obs.at, obs.ts, obs.ts - self.away_since))
                returned = True
            # 只有"真的报告过离开、然后真的回来了"才给坐下宽限期。
            # 之前是"只要 away_since 不为空就重置", 于是 Pose 每抖一下就把宽限期
            # 往后推 20 秒, 把整个信号判定饿死(这就是"该提醒却不提醒"的根因)。
            if returned:
                self.resume_until = max(self.resume_until,
                                        obs.ts + det.resume_grace_seconds)
            self.away_since = None
            self.away_at = None
            self.away_reported = False

    # ---- 单个信号 ----
    def _update_signal(self, kind: SignalKind, ev: SignalEval,
                       obs: Observation, policy: Policy) -> list[Action]:
        actions: list[Action] = []
        tr = self.tracks[kind]
        rem = self.cfg.reminder
        threshold = self.threshold_for(kind)
        record_only = policy.allow_phone and kind is SignalKind.PHONE

        if ev.active:
            tr.last_active_ts = obs.ts
            if tr.active_since is None:
                tr.active_since = obs.ts
                tr.evidence = ev.evidence

            if tr.episode_start is None and obs.ts - tr.active_since >= threshold:
                # 升级阶梯从"首次提醒"算起, 不是从信号出现算起:
                # 否则 10 秒门槛的信号一被判定就已经够到第 2 级, 第一声直接跳到三声。
                tr.episode_start = obs.ts
                tr.signal_start = tr.active_since
                tr.level_emitted = 0
                tr.max_level = 0
                tr.forced = False          # 新的一集重新给"强行打破静默"一次机会
                tr.record_only = record_only
                actions.append(EpisodeStart(kind, ev.evidence, obs.at, obs.ts, record_only))

            if tr.episode_start is not None:
                elapsed = obs.ts - tr.episode_start
                level = 1
                for i, after in enumerate(rem.escalate_after, start=1):
                    if elapsed >= after:
                        level = i
                level = min(level, self.max_level_for(kind))

                # 冷却只约束"这一集的**第一声**", 锚点是"上一次真的响过的时刻":
                #   - 锚在"上一次提醒"而不是"上一次分心结束", 才不会"切换窗口把冷却
                #     无限顺延"; 已经开始提醒的这一集, 升级不受冷却影响
                #     (否则 L2/L3 永远发不出来)。
                cooldown_active = (
                    tr.level_emitted == 0
                    and tr.last_nudge_ts is not None
                    and obs.ts - tr.last_nudge_ts < rem.cooldown_seconds
                )
                # 但**连续分心超过 max_silent_episode_seconds 必须出声**:
                # 漏掉一段两分钟的分心, 比多叫一声严重得多。
                force = (cooldown_active and not tr.forced
                         and elapsed >= rem.max_silent_episode_seconds)
                muted = (obs.ts < tr.muted_until
                         or obs.ts < self.resume_until          # 刚坐下: 别马上念你
                         or (cooldown_active and not force))
                if level > tr.level_emitted and not muted and not record_only:
                    tr.level_emitted = level
                    tr.max_level = max(tr.max_level, level)
                    tr.last_nudge_ts = obs.ts
                    if force:
                        tr.forced = True
                    actions.append(Nudge(kind, level, ev.evidence, not policy.quiet,
                                         obs.at, obs.ts, policy.block_name))
                elif record_only:
                    tr.max_level = 0
        else:
            if tr.episode_start is not None and tr.last_active_ts is not None \
                    and obs.ts - tr.last_active_ts >= RELEASE_SECONDS:
                start = tr.signal_start if tr.signal_start is not None else tr.episode_start
                actions.append(EpisodeEnd(kind, obs.at, obs.ts, obs.ts - start,
                                          tr.max_level, tr.record_only, tr.evidence))
                tr.last_episode_end = obs.ts
                tr.episode_start = None
                tr.signal_start = None
                tr.level_emitted = 0
                tr.active_since = None
                tr.last_active_ts = None
            elif tr.episode_start is None and tr.last_active_ts is not None \
                    and obs.ts - tr.last_active_ts >= RELEASE_SECONDS:
                tr.active_since = None
                tr.last_active_ts = None
        return actions

    # ---- 给托盘/日报用 ----
    def snapshot(self, now_ts: float) -> dict[str, object]:
        # 注意: 这个布尔量的键**不能**叫 "away" —— 下面循环里 SignalKind.AWAY.value
        # 正好也是 "away", 会把布尔量覆盖成字典(字典恒为真), 于是"判定中"永远显示成
        # "离开座位"。这个坑被 test_away.py 里的托盘测试当场抓住。
        out: dict[str, object] = {"is_away": self.away_since is not None and self.away_reported}
        for kind, tr in self.tracks.items():
            out[kind.value] = {
                "in_episode": tr.episode_start is not None,
                "level": tr.level_emitted,
                "elapsed": (now_ts - tr.episode_start) if tr.episode_start else 0.0,
                "muted_for": max(0.0, tr.muted_until - now_ts),
            }
        return out
