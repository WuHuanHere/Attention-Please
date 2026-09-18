"""配置与校准数据的加载。

两个来源, 职责分开:
  - config.toml      : 人手改(作息/白黑名单/提醒/隐私), 带注释
  - calibration.json : calibrate.py 生成(你的坐姿阈值), 机器写

纯逻辑，不依赖摄像头/MediaPipe，可单测。
"""
from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config.toml"
DEFAULT_CALIBRATION = PROJECT_ROOT / "calibration.json"


# --------------------------------------------------------------------------
# 时间
# --------------------------------------------------------------------------
def parse_hhmm(text: str) -> dtime:
    hh, mm = text.strip().split(":")
    h, m = int(hh), int(mm)
    if h == 24 and m == 0:
        # "24:00" 是常见的"到当天结束"写法, 但 datetime.time 上界是 23:59:59.999999
        return dtime.max
    return dtime(h, m)


@dataclass(frozen=True)
class TimeRange:
    start: dtime
    end: dtime

    @classmethod
    def parse(cls, text: str) -> "TimeRange":
        a, b = text.split("-")
        return cls(parse_hhmm(a), parse_hhmm(b))

    def contains(self, t: dtime) -> bool:
        """支持跨午夜区间(如 18:00-24:00 或 23:00-02:00)。end==00:00 视为当天结束。"""
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------
@dataclass
class General:
    camera_index: int = 0
    # 采集分辨率。实测 640x480 下人脸检出率是 0%, 1280x720 + Pose 裁头才可用,
    # 所以默认就是 720p, 不是随手写的默认值。
    camera_width: int = 1280
    camera_height: int = 720
    tick_hz: float = 4.0
    auto_monitor_in_schedule: bool = True
    single_instance: bool = True


@dataclass
class ScheduleBlock:
    name: str
    start: dtime
    end: dtime
    kind: str = "focus"
    allow_phone: bool = False

    @property
    def is_focus(self) -> bool:
        return self.kind == "focus"

    def contains(self, t: dtime) -> bool:
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end


@dataclass
class Schedule:
    blocks: list[ScheduleBlock] = field(default_factory=list)

    def block_at(self, when: datetime | dtime) -> ScheduleBlock | None:
        t = when.time() if isinstance(when, datetime) else when
        for b in self.blocks:
            if b.is_focus and b.contains(t):
                return b
        return None

    def in_focus_window(self, when: datetime | dtime) -> bool:
        return self.block_at(when) is not None

    def planned_seconds(self) -> float:
        """作息表里 focus 块的总时长 = 今天的"计划学习时长"(日报的分母)。"""
        total = 0.0
        for b in self.blocks:
            if not b.is_focus:
                continue
            start = b.start.hour * 3600 + b.start.minute * 60 + b.start.second
            end = b.end.hour * 3600 + b.end.minute * 60 + b.end.second
            if end <= start:      # 跨午夜
                end += 86400
            total += end - start
        return total

    def elapsed_planned_seconds(self, when: datetime) -> float:
        """今天到 when 为止**已经过去**的 focus 时长。

        日报要判断"计划里该监控却完全没有数据"的时间, 分母必须是"已过去"的部分 ——
        用全天计划会让中午生成的日报出现"有 7 小时没有监控数据"这种吓人但没意义的数字。
        """
        now_s = when.hour * 3600 + when.minute * 60 + when.second
        total = 0.0
        for b in self.blocks:
            if not b.is_focus:
                continue
            start = b.start.hour * 3600 + b.start.minute * 60 + b.start.second
            end = b.end.hour * 3600 + b.end.minute * 60 + b.end.second
            if end <= start:
                end += 86400
            if now_s <= start:
                continue
            total += min(now_s, end) - start
        return total


@dataclass
class Reminder:
    max_level_high: int = 3
    max_level_mid: int = 2
    max_level_low: int = 1
    escalate_after: list[int] = field(default_factory=lambda: [0, 10, 30])
    # 静默上限: 一次分心**连续**超过这么久还没出过声, 就强行提醒一次。
    # (冷却的锚点是"上一次真的响过的时刻"; 没有这条, 连续切换窗口会把冷却无限顺延,
    #  实测出现过"整整 109 秒一声不吭"。漏掉两分钟的分心比多叫一声严重得多。)
    max_silent_episode_seconds: int = 90
    cooldown_seconds: int = 180
    wrong_feedback_mute_seconds: int = 900
    show_evidence: bool = True


@dataclass
class QuietHours:
    enabled: bool = True
    ranges: list[TimeRange] = field(default_factory=list)

    def is_quiet(self, when: datetime | dtime) -> bool:
        if not self.enabled:
            return False
        t = when.time() if isinstance(when, datetime) else when
        return any(r.contains(t) for r in self.ranges)


@dataclass
class Detection:
    screen_continuous_seconds: float = 10.0
    phone_continuous_seconds: float = 25.0
    daze_continuous_seconds: float = 240.0
    daze_yaw_std_deg: float = 3.0
    daze_pitch_std_deg: float = 3.0
    daze_blink_drop_ratio: float = 0.5
    # --- Pose 弱证据(看不到脸时的粗头姿, **只记录不报警**) ---
    # 连续这么多秒才算一段, 免得单帧抖动造出一堆碎片分集。
    pose_continuous_seconds: float = 10.0
    # 判定余量 = max(pose_head_min_margin, k * 短窗离散度)。见 pose_head.py。
    pose_head_down_k: float = 1.5
    pose_head_turn_k: float = 1.5
    # ⚠️ 这个默认值目前只由**一个**真人低头样本支撑(probe_out/posenoise.md),
    # 必须等真人实测标定后再定稿。调大 = 更不容易判成低头(更少误报)。
    pose_head_min_margin: float = 0.08
    away_seconds: float = 90.0
    # 离开多久才提醒一次(0 = 关闭)。级别上限是"低"(1 声), 所以一次离开只提醒一次;
    # 安静时段自动变成只弹窗。默认 15 分钟 —— 上个厕所不该被念, 跑去躺着就该。
    away_reminder_seconds: float = 900.0
    resume_grace_seconds: float = 20.0
    # 启用哪些信号。screen=窗口标题; phone=头右偏; pose=Pose 弱证据(只记录不报警);
    # daze=发呆(依赖人脸, 而低头写题时人脸覆盖率只有 12-15% -> 暂不建议开)。
    enabled_signals: list[str] = field(
        default_factory=lambda: ["screen", "phone", "pose"])


@dataclass
class Wordlists:
    whitelist: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)


@dataclass
class Privacy:
    save_captures: bool = True
    save_raw_camera_frame: bool = False
    capture_retention_days: int = 7
    capture_width: int = 960


@dataclass
class Report:
    daily_report: bool = True
    report_time: dtime = dtime(21, 30)
    report_dir: str = "data/reports"


@dataclass
class Config:
    general: General = field(default_factory=General)
    schedule: Schedule = field(default_factory=Schedule)
    reminder: Reminder = field(default_factory=Reminder)
    quiet_hours: QuietHours = field(default_factory=QuietHours)
    detection: Detection = field(default_factory=Detection)
    wordlists: Wordlists = field(default_factory=Wordlists)
    privacy: Privacy = field(default_factory=Privacy)
    report: Report = field(default_factory=Report)
    path: Path = DEFAULT_CONFIG

    # ---- 路径 ----
    @property
    def root(self) -> Path:
        return self.path.parent

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def capture_dir(self) -> Path:
        return self.root / "captures"

    @property
    def models_dir(self) -> Path:
        return self.root / "models"

    @property
    def report_dir(self) -> Path:
        d = Path(self.report.report_dir)
        return d if d.is_absolute() else self.root / d

    @property
    def db_path(self) -> Path:
        return self.data_dir / "events.sqlite3"

    # ---- 加载 ----
    @classmethod
    def load(cls, path: str | Path = DEFAULT_CONFIG) -> "Config":
        p = Path(path)
        with open(p, "rb") as fh:
            raw = tomllib.load(fh)
        return cls.from_dict(raw, path=p)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], path: Path = DEFAULT_CONFIG) -> "Config":
        g = raw.get("general", {})
        r = raw.get("reminder", {})
        q = raw.get("quiet_hours", {})
        d = raw.get("detection", {})
        wl = raw.get("whitelist", {})
        bl = raw.get("blacklist", {})
        pv = raw.get("privacy", {})
        rp = raw.get("report", {})
        sc = raw.get("schedule", {})

        blocks = [
            ScheduleBlock(
                name=b.get("name", ""),
                start=parse_hhmm(b["start"]),
                end=parse_hhmm(b["end"]),
                kind=b.get("kind", "focus"),
                allow_phone=bool(b.get("allow_phone", False)),
            )
            for b in sc.get("block", [])
        ]

        return cls(
            general=General(
                camera_index=int(g.get("camera_index", 0)),
                camera_width=int(g.get("camera_width", 1280)),
                camera_height=int(g.get("camera_height", 720)),
                tick_hz=float(g.get("tick_hz", 4.0)),
                auto_monitor_in_schedule=bool(g.get("auto_monitor_in_schedule", True)),
                single_instance=bool(g.get("single_instance", True)),
            ),
            schedule=Schedule(blocks=blocks),
            reminder=Reminder(
                max_level_high=int(r.get("max_level_high", 3)),
                max_level_mid=int(r.get("max_level_mid", 2)),
                max_level_low=int(r.get("max_level_low", 1)),
                escalate_after=[int(x) for x in r.get("escalate_after", [0, 10, 30])],
                cooldown_seconds=int(r.get("cooldown_seconds", 180)),
                max_silent_episode_seconds=int(r.get("max_silent_episode_seconds", 90)),
                wrong_feedback_mute_seconds=int(r.get("wrong_feedback_mute_seconds", 900)),
                show_evidence=bool(r.get("show_evidence", True)),
            ),
            quiet_hours=QuietHours(
                enabled=bool(q.get("enabled", True)),
                ranges=[TimeRange.parse(x) for x in q.get("ranges", [])],
            ),
            detection=Detection(
                screen_continuous_seconds=float(d.get("screen_continuous_seconds", 10)),
                phone_continuous_seconds=float(d.get("phone_continuous_seconds", 25)),
                daze_continuous_seconds=float(d.get("daze_continuous_seconds", 240)),
                daze_yaw_std_deg=float(d.get("daze_yaw_std_deg", 3.0)),
                daze_pitch_std_deg=float(d.get("daze_pitch_std_deg", 3.0)),
                daze_blink_drop_ratio=float(d.get("daze_blink_drop_ratio", 0.5)),
                pose_continuous_seconds=float(d.get("pose_continuous_seconds", 10)),
                pose_head_down_k=float(d.get("pose_head_down_k", 1.5)),
                pose_head_turn_k=float(d.get("pose_head_turn_k", 1.5)),
                pose_head_min_margin=float(d.get("pose_head_min_margin", 0.08)),
                away_seconds=float(d.get("away_seconds", 90)),
                away_reminder_seconds=float(d.get("away_reminder_seconds", 900)),
                resume_grace_seconds=float(d.get("resume_grace_seconds", 20)),
                enabled_signals=[str(x) for x in
                                 d.get("enabled_signals", ["screen", "phone", "pose"])],
            ),
            wordlists=Wordlists(
                whitelist=list(wl.get("words", [])),
                blacklist=list(bl.get("words", [])),
            ),
            privacy=Privacy(
                save_captures=bool(pv.get("save_captures", True)),
                save_raw_camera_frame=bool(pv.get("save_raw_camera_frame", False)),
                capture_retention_days=int(pv.get("capture_retention_days", 7)),
                capture_width=int(pv.get("capture_width", 960)),
            ),
            report=Report(
                daily_report=bool(rp.get("daily_report", True)),
                report_time=parse_hhmm(rp.get("report_time", "21:30")),
                report_dir=rp.get("report_dir", "data/reports"),
            ),
            path=path,
        )


# --------------------------------------------------------------------------
# 校准数据
# --------------------------------------------------------------------------
@dataclass
class PoseStat:
    """某个姿势下头部角度的分布(度)。"""

    mean: float = 0.0
    std: float = 0.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PoseStat":
        return cls(mean=float(d.get("mean", 0.0)), std=float(d.get("std", 0.0)))


@dataclass
class Calibration:
    """校准结果。没有校准文件时用保守默认值(宁可漏报)。"""

    version: int = 1
    created_at: str = ""
    camera_index: int = 0
    res_width: int = 640
    res_height: int = 480
    screen_yaw: PoseStat = field(default_factory=PoseStat)
    screen_pitch: PoseStat = field(default_factory=PoseStat)
    book_yaw: PoseStat = field(default_factory=PoseStat)
    book_pitch: PoseStat = field(default_factory=PoseStat)
    phone_yaw: PoseStat = field(default_factory=PoseStat)
    phone_pitch: PoseStat = field(default_factory=PoseStat)
    blink_rate_per_min: float = 15.0
    eye_closed_ratio: float = 0.1
    phone_yaw_deg: float = 30.0
    book_pitch_deg: float = 20.0
    screen_pitch_deg: float = 10.0
    # MediaPipe 给出的角度正负是经验性的(取决于相机摆位), 不靠猜: 校准用
    # "看手机 vs 看屏幕" 和 "看书 vs 看屏幕" 的方向反推出来, 下游统一成
    # "右偏为正 / 低头为正"。
    yaw_sign: int = 1
    pitch_sign: int = 1
    is_calibrated: bool = False

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CALIBRATION) -> "Calibration":
        p = Path(path)
        if not p.exists():
            return cls()
        raw = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Calibration":
        base = raw.get("baseline", {})
        th = raw.get("thresholds", {})
        cam = raw.get("camera", {})
        blink = base.get("blink", {})
        return cls(
            version=int(raw.get("version", 1)),
            created_at=raw.get("created_at", ""),
            camera_index=int(cam.get("index", 0)),
            res_width=int(cam.get("width", 640)),
            res_height=int(cam.get("height", 480)),
            screen_yaw=PoseStat.from_dict(base.get("screen", {}).get("yaw", {})),
            screen_pitch=PoseStat.from_dict(base.get("screen", {}).get("pitch", {})),
            book_yaw=PoseStat.from_dict(base.get("book", {}).get("yaw", {})),
            book_pitch=PoseStat.from_dict(base.get("book", {}).get("pitch", {})),
            phone_yaw=PoseStat.from_dict(base.get("phone", {}).get("yaw", {})),
            phone_pitch=PoseStat.from_dict(base.get("phone", {}).get("pitch", {})),
            blink_rate_per_min=float(blink.get("rate_per_min", 15.0)),
            eye_closed_ratio=float(blink.get("eye_closed_ratio", 0.1)),
            phone_yaw_deg=float(th.get("phone_yaw_deg", 30.0)),
            book_pitch_deg=float(th.get("book_pitch_deg", 20.0)),
            screen_pitch_deg=float(th.get("screen_pitch_deg", 10.0)),
            yaw_sign=int(raw.get("signs", {}).get("yaw", 1)),
            pitch_sign=int(raw.get("signs", {}).get("pitch", 1)),
            is_calibrated=bool(raw.get("is_calibrated", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        def stat(s: PoseStat) -> dict[str, float]:
            return {"mean": round(s.mean, 2), "std": round(s.std, 2)}

        return {
            "version": self.version,
            "created_at": self.created_at or datetime.now().isoformat(timespec="seconds"),
            "is_calibrated": self.is_calibrated,
            "camera": {
                "index": self.camera_index,
                "width": self.res_width,
                "height": self.res_height,
            },
            "baseline": {
                "screen": {"yaw": stat(self.screen_yaw), "pitch": stat(self.screen_pitch)},
                "book": {"yaw": stat(self.book_yaw), "pitch": stat(self.book_pitch)},
                "phone": {"yaw": stat(self.phone_yaw), "pitch": stat(self.phone_pitch)},
                "blink": {
                    "rate_per_min": round(self.blink_rate_per_min, 1),
                    "eye_closed_ratio": round(self.eye_closed_ratio, 3),
                },
            },
            "thresholds": {
                "phone_yaw_deg": round(self.phone_yaw_deg, 1),
                "book_pitch_deg": round(self.book_pitch_deg, 1),
                "screen_pitch_deg": round(self.screen_pitch_deg, 1),
            },
            "signs": {"yaw": self.yaw_sign, "pitch": self.pitch_sign},
        }

    def save(self, path: str | Path = DEFAULT_CALIBRATION) -> Path:
        p = Path(path)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")
        return p

    @classmethod
    def derive(cls, samples: dict[str, list[tuple[float, float]]], **kw) -> "Calibration":
        """从 {姿势: [(yaw, pitch), ...]} 推阈值。

        步骤: 1) 统计各姿势的角度分布; 2) 用方向差反推符号并把角度归一化成
        "右偏为正 / 低头为正"; 3) 阈值取两种姿势均值的中点。
        """
        import statistics

        def stat(pairs: list[tuple[float, float]], axis: int) -> PoseStat:
            vals = [p[axis] for p in pairs] or [0.0]
            return PoseStat(mean=statistics.fmean(vals),
                            std=statistics.pstdev(vals) if len(vals) > 1 else 0.0)

        screen = samples.get("screen", [])
        book = samples.get("book", [])
        phone = samples.get("phone", [])
        cal = cls(**kw)
        cal.screen_yaw, cal.screen_pitch = stat(screen, 0), stat(screen, 1)
        cal.book_yaw, cal.book_pitch = stat(book, 0), stat(book, 1)
        cal.phone_yaw, cal.phone_pitch = stat(phone, 0), stat(phone, 1)

        # --- 符号自学习 ---
        if screen and phone:
            cal.yaw_sign = 1 if cal.phone_yaw.mean >= cal.screen_yaw.mean else -1
        if screen and book:
            cal.pitch_sign = 1 if cal.book_pitch.mean >= cal.screen_pitch.mean else -1
        for s in (cal.screen_yaw, cal.phone_yaw):
            s.mean *= cal.yaw_sign
        for s in (cal.screen_pitch, cal.book_pitch, cal.phone_pitch):
            s.mean *= cal.pitch_sign

        # --- 阈值 ---
        if screen and phone:
            cal.phone_yaw_deg = (cal.screen_yaw.mean + cal.phone_yaw.mean) / 2.0
        elif screen:
            cal.phone_yaw_deg = cal.screen_yaw.mean + max(20.0, 3 * cal.screen_yaw.std)
        if screen and book:
            cal.book_pitch_deg = (cal.screen_pitch.mean + cal.book_pitch.mean) / 2.0
        elif screen:
            cal.book_pitch_deg = cal.screen_pitch.mean + max(12.0, 3 * cal.screen_pitch.std)
        cal.screen_pitch_deg = cal.screen_pitch.mean + max(8.0, 2 * cal.screen_pitch.std)
        cal.is_calibrated = True
        cal.created_at = datetime.now().isoformat(timespec="seconds")
        return cal


def today_str(when: datetime | None = None) -> str:
    return (when or datetime.now()).date().isoformat()


def date_str(d: date) -> str:
    return d.isoformat()
