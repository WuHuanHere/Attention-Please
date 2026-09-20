"""观测特征 -> 信号 -> 分集(episode) -> 分级提醒。

这是整个程序的心脏, 而且是**纯逻辑**: 输入是一帧观测(数字), 输出是一串动作事件。
不碰摄像头、不碰 MediaPipe、不碰声音, 所以可以完全用假数据单测。

已确认的判定口径:
  切到娱乐窗口(screen)  高可信  连续 screen_continuous_seconds 秒    可升到 max_level_high
  看手机(phone)          中可信  连续 phone_continuous_seconds 秒     可升到 max_level_mid
  离开(away)             低可信  Pose 丢失 away_reminder_seconds 秒   只记录 + 最多 1 声
  Pose 弱证据(pose)      ——      连续 pose_continuous_seconds 秒     **只记录, 永不报警**

**发呆(daze)已于 2026-09-18 砍掉**(用户拍板, 见 HANDOFF §9)。三条独立证据:
  1. 它依赖人脸关键点, 而低头写题时人脸覆盖率只有 12-15% —— 在最需要它的场景下天然不可用;
  2. 它的眨眼闸门用校准基线 3.0/分(正常人 15-20), `blink_rate < 1.5` 几乎永不成立
     -> 它既不误报也不生效, 是个"哑"信号;
  3. 改用 Pose 来报警也不行: Pose 判"低头"确实准(真人标定 98%), 但"低头 + 头不动 4 分钟"
     正是**认真写题**的样子 —— 拿它报警就是最大的误报源。
它想要的价值(把"看不到脸"那段时间说清楚)现在由 **Pose 弱证据通道**承担, 而那条通道
**只记录不报警**。代码在 git 历史里(commit dfc1114 之前), 要复活请连同上面的证据一起重新评估。

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
from .pose_head import PoseHead
from .wordlist import TitleVerdict, classify


class SignalKind(str, Enum):
    SCREEN = "screen"
    PHONE = "phone"
    # 离开座位太久。它**不走 enabled_signals 开关**, 由 away_reminder_seconds 控制
    # (0 = 关闭), 因为"人不在"和"人在但分心"是两件不同的事。
    AWAY = "away"
    # Pose 弱证据: **看不到脸**的时候, 用 Pose 的头部关键点粗判"低头/转头"。
    # 它**永远不能报警**(上限 0 级 + 结构上强制 record_only), 只进库里当记录。
    # 存在的理由: 低头写题时人脸覆盖率只有 12-15%, 而这段时间 Pose 还有 80% ——
    # 没有它, 日报上那段时间只是一句"看不清", 分不出"在写题"还是"人不在"。
    POSE = "pose"


CONFIDENCE: dict[SignalKind, str] = {
    SignalKind.SCREEN: "high",
    SignalKind.PHONE: "mid",
    SignalKind.AWAY: "low",      # 上限 1 声 -> 一次离开只会响一次, 不会连环叫
    SignalKind.POSE: "record",   # 只记录: 见 max_level_for 里的硬性 0
}

SIGNAL_LABEL: dict[SignalKind, str] = {
    SignalKind.SCREEN: "切到了娱乐窗口",
    SignalKind.PHONE: "在看手机",
    SignalKind.AWAY: "离开座位太久",
    SignalKind.POSE: "Pose 弱证据(只记录)",
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
    # ⚠️ 下面四个字段**目前没有任何信号消费**(发呆已于 2026-09-18 砍掉, 见模块 docstring)。
    # 留着是因为采集层(perception)仍在测, 将来真要再做"头没动/眨眼"类判据时有现成入口 ——
    # 但**别以为它们现在有用**: 要启用必须先回答"当年砍掉发呆的那三条证据还成立吗"。
    yaw_std: float | None = None      # 头姿在 20 秒窗内的波动
    pitch_std: float | None = None
    blink_rate: float | None = None   # 次/分
    eye_closed_ratio: float | None = None
    idle_seconds: float = 0.0         # 键鼠空闲时长
    title: str | None = None          # 前台窗口标题
    # Pose 弱证据(看不到脸时的粗头姿)。None = Pose 弃权。**只记录不报警**。
    pose_head: PoseHead | None = None


@dataclass
class Policy:
    """当前上下文策略, 由 runtime 按时间表和设置填。"""

    judging: bool = True          # 是否处于判定状态(时间表外/暂停时为 False)
    allow_phone: bool = False     # 该时段"看手机"只记录不报警
    quiet: bool = False           # 安静时段: 不发声, 只弹窗
    block_name: str = ""
    # 这一轮不判定的原因是"要把摄像头让给别的程序"。状态机不用它(它只看 judging),
    # 但 runtime 要据此**立刻**释放摄像头(而不是等 120 秒的常规释放)。
    camera_yield: bool = False
    # 只启用这些信号。默认 = 全开(默认值就该是"不设限"), 真正生效的集合由 runtime
    # 按 config.toml 的 enabled_signals 填。曾经这里硬写 ["screen","phone","daze"],
    # 结果是"新增一个信号但默认值里没有它" —— 单测里它永远不触发, 而生产里会触发,
    # 两边行为不一致。
    enabled: frozenset[str] = frozenset(
        {"screen", "phone", "pose"})


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
    # 用户点了「判定错了」-> 这一段是误报。**必须能从事件里看出来**:
    # 以前 report_wrong 只设了静默, 于是用户已经明确判为误报的那段分心照样累计时长、
    # 照样从有效专注里扣掉, 而日报还并列写着"你标记了 1 次误判"。
    wrong: bool = False


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

    # --- 发呆(DAZE)已于 2026-09-18 砍掉 —— 见模块 docstring 里的三条证据 ---
    # 原来这里是"低头 + 头没动 + 眨眼变少"三合一。它被砍掉之后, `yaw_std`/`pitch_std`/
    # `blink_rate`/`eye_closed_ratio` 这四个观测字段就**没有任何信号消费了**
    # (采集层仍在测, 见 Observation 上的注释)。

    # --- Pose 弱证据: **只在看不到脸的时候**用它(看得到脸就有更好的证据) ---
    # 它回答的是"看不清的那段时间里, 你到底在不在、是不是在写题", 而不是"要不要提醒你"。
    # 所以它永不报警(见 max_level_for), 只进库里当记录, 日报据此把"看不清"拆开。
    ph = obs.pose_head
    pose_active = (
        ph is not None
        and obs.pitch is None                 # 看得到脸就不用它兜底
        and (ph.head_down or ph.head_turned)
    )
    out[SignalKind.POSE] = SignalEval(pose_active, ph.describe() if pose_active else "")
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
        # Pose 弱证据**永远 0 级** —— 它是模型从身体外推出来的低精度头姿,
        # 按"误报最烦"的铁律, 它连"一声"都不配有, 只能记录。
        if signal is SignalKind.POSE:
            return 0
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
            SignalKind.POSE: d.pose_continuous_seconds,
            SignalKind.AWAY: d.away_reminder_seconds,
        }[signal]

    def report_wrong(self, signal: SignalKind, now_ts: float,
                     now_at: datetime) -> list[Action]:
        """用户点了「判定错了」: **立刻结束分集**(并标记成误报), 该信号静默一段时间。

        以前这里只设了 `muted_until`, 而 docstring 却写着"立刻结束分集" —— 结果是:
        用户已经明确判定为误报的那段分心继续累计时长, 照样从有效专注里扣掉,
        而日报还并列印着"你标记了 1 次误判"。现在真的把它结掉并打上 `wrong` 标记,
        由 `store.day_stats` 把它从分心时长里剔除(时长单列, 仍然可见)。
        """
        tr = self.tracks[signal]
        tr.muted_until = now_ts + self.cfg.reminder.wrong_feedback_mute_seconds
        actions: list[Action] = []
        if tr.episode_start is not None:
            start = tr.signal_start if tr.signal_start is not None else tr.episode_start
            actions.append(EpisodeEnd(signal, now_at, now_ts, max(0.0, now_ts - start),
                                      tr.max_level, tr.record_only, tr.evidence,
                                      wrong=True))
            tr.episode_start = None
            tr.signal_start = None
            tr.level_emitted = 0
            tr.active_since = None
            tr.last_active_ts = None
        return actions

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
        # 两道独立的闸门, 防止 Pose 弱证据意外出声:
        #   1) max_level_for(POSE) == 0 -> 等级永远到不了 1;
        #   2) 这里强制 record_only   -> 就算等级算错了, 下面也不会发 Nudge。
        # 双保险是故意的: 这一条一旦破掉, 就是"看不到脸还替你下结论 + 吵你",
        # 正好同时踩中两条铁律。
        record_only = (kind is SignalKind.POSE
                       or (policy.allow_phone and kind is SignalKind.PHONE))

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
