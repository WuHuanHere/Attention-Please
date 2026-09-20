"""事件与覆盖率入库(SQLite, 标准库, 不引第三方)。

存两类东西, 目的完全不同:
  1) `event`          —— 发生了什么(分心分集、提醒、离开、暂停、判定错了、摄像头被占用)。
                         所有"有效专注时长"都是从这里算出来的, 所以宁可多存证据字段。
  2) `coverage_minute` —— 每分钟一行: 总帧数 / Pose 检出 / 人脸检出。
                         **人脸覆盖率是这套系统的诚实度指标**: 覆盖率低的时候,
                         "看手机"信号其实是瞎的, 日报必须把这段时间标成"看不清",
                         而不是当成"你很专注"。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path

# 撞上写锁时等多久才放弃。**这个数字和 runtime.FLUSH_EVERY_SECONDS 是一对**:
# 监控线程攥着写锁的时长必须远小于它, 否则托盘线程的写必然失败。
# tests/test_store_lock.py 里有守这条关系的测试。
BUSY_TIMEOUT_SECONDS = 10

# 哪些信号的分集才算"分心分集" —— 口径必须和 day_stats 里的一致:
# away 的时长走 away_end(提醒次数也单独统计), pose 是"只记录不报警"的弱证据。
DISTRACTION_SIGNALS = ("screen", "phone")

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
    # 有 episode_start 却没有 episode_end 的分集 = **程序在分心过程中被关掉/崩溃/蓝屏**。
    # 它的时长是未知的(不是 0!), 所以只报"有几段", 绝不编一个数字出来 —— 宁可缺, 不可假。
    unfinished_episodes: int = 0
    # 同上, 但针对"离开座位": 有 away_start 没有 away_end。那段离开时长同样是未知的。
    unfinished_away: int = 0
    nudges: int = 0
    away_nudges: int = 0              # "离开太久"的提醒次数(单独统计, 不算分心提醒)
    episodes: int = 0
    wrong_feedback: int = 0
    camera_busy_seconds: float = 0.0
    yield_seconds: float = 0.0        # 把摄像头让给别的程序(会议/通话/直播)的时长
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
            conn = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_SECONDS,
                                   check_same_thread=False)
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

    # ---- 读写的两道安全阀 ----
    #
    # 这两个方法是 2026-09-20 那个"日报卡在 17 分钟"的 bug 的直接产物, 别删:
    #
    #   pysqlite 在执行 DML 之前会**隐式 BEGIN**。如果那条语句本身失败(最常见的是
    #   "database is locked"), 事务不会自动结束 —— 它留在连接里。此后这个连接:
    #     * 所有 SELECT 都钉在失败那一刻的快照上(WAL 读事务的快照语义),
    #     * 写操作也不再重新 BEGIN, 于是一直撞在同一个锁上。
    #   实测时间线: 13:37:54 托盘线程写事件撞锁 -> 13:37:56 悬停提示轮询读了第一次,
    #   快照就此冻结 -> 之后菜单里的日报**永远**显示 13:37 的 17 分钟, 而监控线程
    #   自己的进度早就是 0.97 h。连接不是坏了, 是"半死", 而且没有任何报错。
    #
    # 所以: 写失败必须回滚(把连接放回干净状态), 读之前必须了结挂着的写事务
    # (保证读到的是最新数据, 而不是某个旧快照)。
    def _rollback_quietly(self) -> None:
        try:
            self.conn.rollback()
        except Exception:  # noqa: BLE001 - 回滚失败时已无路可走, 不能让清理动作再抛
            pass

    def _write(self, sql: str, params: tuple) -> None:
        try:
            self.conn.execute(sql, params)
        except Exception:
            self._rollback_quietly()
            raise

    def _fresh_read(self) -> None:
        """读之前把本连接上挂着的写事务落定。

        开事务的永远是写路径, 所以这里 commit 只会把**已经写好的东西**落盘, 不会丢数据;
        代价是可能把一个还在攒的批次提前提交 —— 对这套系统来说"读到最新"远比
        "攒批次"重要。写完还没 commit 的 tick 也不会丢, 它们只是提前落盘了。
        """
        try:
            if self.conn.in_transaction:
                self.conn.commit()
        except sqlite3.Error:
            self._rollback_quietly()

    # ---- 写入 ----
    def event(self, kind: str, *, at: datetime | None = None, signal: str | None = None,
              level: int | None = None, duration: float | None = None,
              evidence: str | None = None, block: str | None = None,
              detail: str | None = None) -> None:
        at = at or datetime.now()
        self._write(
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
        self._write(
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
        self._fresh_read()
        st = DayStats(day=day, planned_seconds=planned_seconds)
        # 分集的开头/结尾分开数, 用来找"有开头没结尾"的那种(见 unfinished_episodes)。
        starts: dict[str, int] = {}
        ends: dict[str, int] = {}
        away_starts = away_ends = 0
        cur = self.conn.execute(
            "SELECT kind, signal, level, duration FROM event WHERE day = ?", (day,))
        for kind, signal, _level, duration in cur.fetchall():
            duration = duration or 0.0
            if kind == "episode_start":
                starts[signal] = starts.get(signal, 0) + 1
            elif kind == "episode_end":
                ends[signal] = ends.get(signal, 0) + 1
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
            elif kind == "away_start":
                away_starts += 1
            elif kind == "away_end":
                away_ends += 1
                st.away_seconds += duration
            elif kind == "pause_end":
                st.pause_seconds += duration
            elif kind == "wrong":
                st.wrong_feedback += 1
            elif kind == "camera_busy_end":
                st.camera_busy_seconds += duration
            elif kind == "camera_yield_end":
                st.yield_seconds += duration

        # 有开头没结尾的分集 = 程序在分心过程中被关掉/崩溃/蓝屏。
        # **不能假装它不存在**: 提醒已经响过了, 而它既不计时长也不计次数, 于是日报会
        # 出现"提醒 2 次 / 分心 0 分钟"这种自相矛盾的行(2026-09-20 实测: 10:49:31 那次
        # 分心刚开 16 秒, 程序就被关掉了)。时长未知, 所以只数段数, 不编数字。
        # 注: 退出时 runtime.shutdown() 会把进行中的分集收口, 所以这里剩下的都是"硬死"
        # (被杀/崩溃/蓝屏)。pose 与 away 的分集不在这里计 —— 它们本来就不算分心分集。
        st.unfinished_episodes = sum(
            max(0, n - ends.get(sig, 0)) for sig, n in starts.items()
            if sig in DISTRACTION_SIGNALS)
        # 离开也一样: 有 away_start 没 away_end -> 那段离开时长静默丢失。
        # 实测(2026-09-17): 21:29:11 离开, 最后一个时间表块 21:30:00 结束, away_end 被丢,
        # 日报就写「离开座位 0 分钟」—— 人明明走了。现在单独报出来。
        st.unfinished_away = max(0, away_starts - away_ends)

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
        self._fresh_read()
        cur = self.conn.execute(
            "SELECT at, duration FROM event WHERE day = ? AND kind = 'episode_end'"
            " AND (signal IS NULL OR signal NOT IN ('away', 'pose'))", (day,))
        buckets: dict[int, float] = {}
        for at, duration in cur.fetchall():
            hour = datetime.fromisoformat(at).hour
            buckets[hour] = buckets.get(hour, 0.0) + (duration or 0.0)
        return sorted(buckets.items(), key=lambda kv: kv[1], reverse=True)[:top]

    def top_titles_before(self, day: str, top: int = 5) -> list[tuple[str, int]]:
        self._fresh_read()
        cur = self.conn.execute(
            "SELECT detail, COUNT(*) FROM event WHERE day = ? AND kind = 'nudge'"
            " AND detail IS NOT NULL GROUP BY detail ORDER BY COUNT(*) DESC LIMIT ?",
            (day, top))
        return [(d, n) for d, n in cur.fetchall()]

    def pause_reasons(self, day: str) -> list[tuple[str, float]]:
        """暂停理由与时长。暂停是"有代价的" —— 代价就是理由会被记进日报。"""
        self._fresh_read()
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
