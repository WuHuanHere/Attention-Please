"""事件与覆盖率入库(SQLite, 标准库, 不引第三方)。

存两类东西, 目的完全不同:
  1) `event`          —— 发生了什么(分心分集、提醒、离开、暂停、判定错了、摄像头被占用)。
                         所有"有效专注时长"都是从这里算出来的, 所以宁可多存证据字段。
  2) `coverage_minute` —— 每分钟一行: 总帧数 / Pose 检出 / 人脸检出。
                         **人脸覆盖率是这套系统的诚实度指标**: 覆盖率低的时候,
                         "看手机/发呆"两个信号其实是瞎的, 日报必须把这段时间标成"看不清",
                         而不是当成"你很专注"。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS event (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    at       TEXT NOT NULL,
    day      TEXT NOT NULL,
    kind     TEXT NOT NULL,
    signal   TEXT,
    level    INTEGER,
    duration REAL,
    evidence TEXT,
    block    TEXT,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_event_day ON event(day);

CREATE TABLE IF NOT EXISTS coverage_minute (
    day        TEXT NOT NULL,
    minute     TEXT NOT NULL,
    ticks      INTEGER NOT NULL DEFAULT 0,
    pose_hits  INTEGER NOT NULL DEFAULT 0,
    face_hits  INTEGER NOT NULL DEFAULT 0,
    judging    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, minute)
);
"""


@dataclass
class DayStats:
    day: str
    planned_seconds: float = 0.0      # 计划学习时长(时间表里的 focus 块)
    focus_seconds: float = 0.0        # 有效专注 = 监控时间 - 分心 - 看不清 - 离开
    distract_seconds: float = 0.0
    away_seconds: float = 0.0
    pause_seconds: float = 0.0
    blind_seconds: float = 0.0        # 看不清(人脸覆盖率不足)的时间
    pose_weak_seconds: float = 0.0    # 看不清的时间里, Pose 弱证据显示"低头/转头"的时长
    nudges: int = 0
    away_nudges: int = 0              # "离开太久"的提醒次数(单独统计, 不算分心提醒)
    episodes: int = 0
    wrong_feedback: int = 0
    camera_busy_seconds: float = 0.0
    coverage: float = 0.0             # 人脸覆盖率 0..1
    monitored_seconds: float = 0.0    # **实际监控时长** —— 比值要用它当分母
    first_monitored: str = ""         # "HH:MM", 今天第一次判定的时刻
    last_monitored: str = ""          # "HH:MM"


class Store:
    """SQLite 事件库。

    **每个线程一份连接**(thread-local)。这不是优雅, 是必须:
    托盘版里 Runtime 在主线程创建、监控循环在工作线程跑, 托盘悬停提示又在
    第三个线程查库 —— 共用一条 sqlite3 连接会直接抛
    `ProgrammingError: SQLite objects created in a thread can only be used in that same thread`,
    而且是每帧都抛(实测踩到: 整条监控链路静默瘫痪, 只有托盘菜单还活着)。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._conn_lock = threading.Lock()
        self.conn  # 立刻建一次, 顺便建表

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # check_same_thread=False 是**为了能在 shutdown 时由主线程统一关闭**
            # 各个工作线程的连接; 每个线程仍然只用自己的那一份(thread-local), 不存在并发共用。
            conn = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
            try:
                conn.execute("PRAGMA journal_mode=WAL")   # 读写不互相阻塞(托盘每 2 秒读一次)
            except sqlite3.Error:
                pass
            conn.executescript(SCHEMA)      # 幂等
            conn.commit()
            self._local.conn = conn
            with self._conn_lock:
                self._conns.append(conn)
        return conn

    # ---- 写入 ----
    def event(self, kind: str, *, at: datetime | None = None, signal: str | None = None,
              level: int | None = None, duration: float | None = None,
              evidence: str | None = None, block: str | None = None,
              detail: str | None = None) -> None:
        at = at or datetime.now()
        self.conn.execute(
            "INSERT INTO event (ts, at, day, kind, signal, level, duration, evidence, block, detail)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (at.timestamp(), at.isoformat(timespec="seconds"), at.date().isoformat(),
             kind, signal, level, duration, evidence, block, detail))
        self.conn.commit()

    def coverage_tick(self, at: datetime, pose_hit: bool, face_hit: bool,
                      judging: bool) -> None:
        """累加当前分钟的覆盖率。调用方按帧调用。"""
        day = at.date().isoformat()
        minute = at.strftime("%Y-%m-%dT%H:%M")
        self.conn.execute(
            "INSERT INTO coverage_minute (day, minute, ticks, pose_hits, face_hits, judging)"
            " VALUES (?,?,1,?,?,?)"
            " ON CONFLICT(day, minute) DO UPDATE SET"
            "   ticks = ticks + 1,"
            "   pose_hits = pose_hits + excluded.pose_hits,"
            "   face_hits = face_hits + excluded.face_hits,"
            "   judging = judging + excluded.judging",
            (day, minute, 1 if pose_hit else 0, 1 if face_hit else 0, 1 if judging else 0))

    def flush(self) -> None:
        self.conn.commit()

    # ---- 查询(M5 日报用) ----
    def day_stats(self, day: str, planned_seconds: float = 0.0,
                  tick_hz: float = 5.0) -> DayStats:
        st = DayStats(day=day, planned_seconds=planned_seconds)
        cur = self.conn.execute(
            "SELECT kind, signal, level, duration FROM event WHERE day = ?", (day,))
        for kind, signal, _level, duration in cur.fetchall():
            duration = duration or 0.0
            if kind == "episode_end":
                if signal == "away":
                    # "离开太久"的分集不是分心 —— 那段时间已经算进 away_seconds 了,
                    # 再计一次会重复扣减有效专注。
                    continue
                if signal == "pose":
                    # Pose 弱证据是**记录**, 不是判定: 它是"看不到脸时 Pose 觉得你在低头/
                    # 转头", 精度低到连一声提醒都不配(见 signals.max_level_for)。
                    # 算进分心会让"低头写题"直接变成"分心", 那正是最烦的误报。
                    st.pose_weak_seconds += duration
                    continue
                st.episodes += 1
                st.distract_seconds += duration
            elif kind == "nudge":
                if signal == "away":
                    st.away_nudges += 1
                else:
                    st.nudges += 1
            elif kind == "away_end":
                st.away_seconds += duration
            elif kind == "pause_end":
                st.pause_seconds += duration
            elif kind == "wrong":
                st.wrong_feedback += 1
            elif kind == "camera_busy_end":
                st.camera_busy_seconds += duration

        cur = self.conn.execute(
            "SELECT COALESCE(SUM(ticks),0), COALESCE(SUM(pose_hits),0),"
            " COALESCE(SUM(face_hits),0), MIN(minute), MAX(minute)"
            " FROM coverage_minute WHERE day = ?", (day,))
        ticks, pose_hits, face_hits, first_minute, last_minute = cur.fetchone()
        # 实际监控时长 = 判定 tick 数 / 频率。日报的比值**必须**用它当分母:
        # 用"计划时长"当分母, 会把"机器没开机/程序没跑"算成"你没专注"(2026-09-18 实测踩到)。
        st.monitored_seconds = ticks / max(tick_hz, 0.1)
        st.first_monitored = (first_minute or "")[11:16]
        st.last_monitored = (last_minute or "")[11:16]
        if ticks:
            st.coverage = face_hits / ticks
            # 判定中但脸看不到的分钟 -> 记成"看不清"(不给专注时长, 也不当成分心)
            cur = self.conn.execute(
                "SELECT minute, ticks, face_hits, judging FROM coverage_minute"
                " WHERE day = ?", (day,))
            for _minute, m_ticks, m_face, m_judging in cur.fetchall():
                if not m_ticks or not m_judging:
                    continue
                if m_face / m_ticks < 0.5:
                    st.blind_seconds += m_judging / max(tick_hz, 0.1)

        st.focus_seconds = max(0.0, st.monitored_seconds - st.distract_seconds
                               - st.blind_seconds - st.away_seconds)
        return st

    def busiest_distraction_hours(self, day: str, top: int = 3) -> list[tuple[int, float]]:
        """最常分心的时段。

        **必须排除 away 与 pose**: 这两类分集进了 event 表, 但它们不是分心 ——
        away 的时间已经在 away_seconds 里, pose 是"看不到脸时的弱证据记录"。
        不排除的话"最常分心的时段"会把离开座位和低头写题算进去(实测口径 bug)。
        """
        cur = self.conn.execute(
            "SELECT at, duration FROM event WHERE day = ? AND kind = 'episode_end'"
            " AND (signal IS NULL OR signal NOT IN ('away', 'pose'))", (day,))
        buckets: dict[int, float] = {}
        for at, duration in cur.fetchall():
            hour = datetime.fromisoformat(at).hour
            buckets[hour] = buckets.get(hour, 0.0) + (duration or 0.0)
        return sorted(buckets.items(), key=lambda kv: kv[1], reverse=True)[:top]

    def top_titles_before(self, day: str, top: int = 5) -> list[tuple[str, int]]:
        cur = self.conn.execute(
            "SELECT detail, COUNT(*) FROM event WHERE day = ? AND kind = 'nudge'"
            " AND detail IS NOT NULL GROUP BY detail ORDER BY COUNT(*) DESC LIMIT ?",
            (day, top))
        return [(d, n) for d, n in cur.fetchall()]

    def pause_reasons(self, day: str) -> list[tuple[str, float]]:
        """暂停理由与时长。暂停是"有代价的" —— 代价就是理由会被记进日报。"""
        cur = self.conn.execute(
            "SELECT detail, SUM(duration) FROM event WHERE day = ? AND kind = 'pause_end'"
            " GROUP BY detail ORDER BY SUM(duration) DESC", (day,))
        return [((d or "(未填理由)"), float(s or 0.0)) for d, s in cur.fetchall()]

    def close(self) -> None:
        """关闭**所有**线程的连接。

        只关当前线程那一份会让工作线程的连接一直挂着(实测: 临时目录删不掉、
        退出时 WAL 文件残留), 所以每个连接都登记在册, 退出时统一关。
        """
        with self._conn_lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            try:
                conn.commit()
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._local = threading.local()


def today() -> str:
    return date.today().isoformat()


def now_ts() -> float:
    return time.time()
