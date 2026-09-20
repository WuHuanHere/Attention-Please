"""守 2026-09-20 那个"日报卡在 17 分钟"的 bug —— 一个连接是怎么被悄悄弄成半死的。

现场: 用户说"统计专注时长和实际监控时长的功能不准, 今天的专注日报上这个时间一直
保持在 17 分钟"。监控线程自己的进度行早就写到 0.97 h 了, 只有托盘菜单里的日报卡着。

机制(已用最小复现证实):
  1. 每帧的 coverage_tick 会隐式开启一个写事务, 而当时 30 秒才 commit 一次 ——
     整个库的**写锁**有 30/30 秒在监控线程手里;
  2. 托盘线程(菜单动作)写事件时撞上写锁, 10 秒 busy timeout 后抛 database is locked;
  3. **pysqlite 隐式 BEGIN 之后失败的语句不会结束事务** —— 连接被留在事务里;
  4. 2 秒后悬停提示轮询做了一次 SELECT, WAL 读事务的快照就此钉死在那一刻;
  5. 从此这个连接读到的永远是 13:37 的数据, 写也永远撞同一个锁, 而且一声不响。

所以这里守三件事:
  * 写失败必须回滚(连接不留事务);
  * 读之前必须了结挂着的写事务(读到的永远是最新, 不是某个旧快照);
  * 监控线程攥写锁的时长必须远小于 busy timeout(从源头不让失败发生)。
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.runtime import FLUSH_EVERY_SECONDS  # noqa: E402
from attention_please.store import BUSY_TIMEOUT_SECONDS, Store  # noqa: E402

DAY = "2026-09-20"
# 测试里把托盘的 busy timeout 压到 200 毫秒, 不然每个用例都要真等 10 秒。
TEST_BUSY_MS = 200


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 9, 20, hh, mm, ss)


class LockFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "t.sqlite3"

    def tearDown(self):
        for s in getattr(self, "_stores", []):
            s.close()
        self.tmp.cleanup()

    def two_connections(self) -> tuple[Store, Store]:
        """(监控线程的连接, 托盘线程的连接) —— 两个 Store 就是两份 thread-local 连接。"""
        monitor = Store(self.path)
        tray = Store(self.path)
        tray.conn.execute(f"PRAGMA busy_timeout={TEST_BUSY_MS}")
        self._stores = [monitor, tray]
        return monitor, tray

    def hold_write_lock(self, monitor: Store) -> None:
        """复现监控线程攥住写锁: 写一帧但不提交。"""
        monitor.coverage_tick(at(9, 0, 0), pose_hit=True, face_hit=True, judging=True)
        self.assertTrue(monitor.conn.in_transaction)


class TestFailedWriteDoesNotPoisonTheConnection(LockFixture):
    def test_failed_write_leaves_no_open_transaction(self):
        """核心不变量: 写失败之后连接必须是干净的。

        修复前 `tray.conn.in_transaction` 是 True —— 就是它让后面所有读都停在旧快照。
        """
        monitor, tray = self.two_connections()
        self.hold_write_lock(monitor)
        with self.assertRaises(sqlite3.OperationalError):
            tray.event("nudge", at=at(9, 0, 1), signal="screen", level=1)
        self.assertFalse(tray.conn.in_transaction,
                         "写失败后连接被留在事务里: 之后它的读会永远停在失败那一刻的快照")

    def test_reads_are_not_frozen_by_a_failed_write(self):
        """用户实际看到的症状: 日报/悬停提示的数字再也不动了。"""
        monitor, tray = self.two_connections()
        self.hold_write_lock(monitor)
        with self.assertRaises(sqlite3.OperationalError):
            tray.event("nudge", at=at(9, 0, 1), signal="screen", level=1)

        tray.day_stats(DAY)          # 悬停提示轮询: 以前正是在这里把快照钉死

        monitor.event("nudge", at=at(9, 0, 2), signal="screen", level=1)
        monitor.flush()              # 监控线程正常提交

        self.assertEqual(tray.day_stats(DAY).nudges, 1,
                         "托盘读到的是旧快照 —— 数字会一直停着不动")

    def test_write_works_again_once_the_lock_is_free(self):
        """写失败不是永久的: 锁一放开, 同一个连接必须能正常写。"""
        monitor, tray = self.two_connections()
        self.hold_write_lock(monitor)
        with self.assertRaises(sqlite3.OperationalError):
            tray.event("nudge", at=at(9, 0, 1), signal="screen", level=1)
        monitor.flush()

        tray.event("nudge", at=at(9, 0, 3), signal="screen", level=1)
        self.assertEqual(tray.day_stats(DAY).nudges, 1)

    def test_commit_of_the_failed_write_is_not_lost_silently(self):
        """写失败必须往外抛 —— 调用方(托盘)要能把"记账失败"说出来。"""
        monitor, tray = self.two_connections()
        self.hold_write_lock(monitor)
        with self.assertRaises(sqlite3.OperationalError):
            tray.event("episode_end", at=at(9, 0, 1), signal="screen", duration=30)
        monitor.flush()
        self.assertEqual(tray.day_stats(DAY).episodes, 0,
                         "没写进去的东西不该出现在统计里(宁可缺, 不可假)")


class TestReadGuard(LockFixture):
    def test_read_discards_a_dangling_transaction(self):
        """第二道阀: 就算将来又有人把事务挂住了, 读也必须是最新的。

        这里故意绕过 `_write` 手工 BEGIN, 模拟"任何未来的同类 bug"。
        """
        monitor, tray = self.two_connections()
        self.hold_write_lock(monitor)
        tray.conn.execute("BEGIN")           # 手工制造悬挂事务
        tray.day_stats(DAY)                  # 快照钉在此刻

        monitor.event("nudge", at=at(9, 0, 2), signal="screen", level=1)
        monitor.flush()

        self.assertEqual(tray.day_stats(DAY).nudges, 1)

    def test_read_guard_does_not_lose_pending_writes(self):
        """读了结事务时只能落盘, 不能丢: 先写的 tick 必须还在。"""
        _monitor, tray = self.two_connections()
        for i in range(4):
            tray.coverage_tick(at(9, 0, i * 10), pose_hit=True, face_hit=True, judging=True)
        tray.day_stats(DAY)                  # 读一次 -> 顺带提交
        st = tray.day_stats(DAY)
        self.assertEqual(st.monitored_seconds, 4 / 5.0)


class TestLockWindowContract(unittest.TestCase):
    def test_flush_window_is_far_below_the_busy_timeout(self):
        """监控线程攥写锁的时长 << busy timeout, 否则托盘写必然失败。

        这条关系就是 bug 的根源: 原来是 30 秒 vs 10 秒, 比例反了。
        """
        self.assertLess(FLUSH_EVERY_SECONDS, BUSY_TIMEOUT_SECONDS / 2,
                        f"落盘间隔 {FLUSH_EVERY_SECONDS}s 太长: 监控线程会攥着写锁"
                        f"超过托盘 {BUSY_TIMEOUT_SECONDS}s 的等待上限, 托盘写必然失败")
        self.assertGreater(FLUSH_EVERY_SECONDS, 0)


if __name__ == "__main__":
    unittest.main()
