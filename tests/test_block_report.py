"""日报「各时段专注情况」—— 按用户自己的作息表分时段统计专注时长。

## 为什么单开一张表

全天只有一个"有效专注 X 小时"时, "上午两小时全神贯注、下午三小时全废"会被平均成
一个谁都不得罪的数字, 看不出该改哪一段。分时段之后, 计划/监控/有效专注/分心/看不清/
离开 全都落到具体那一段上。

## 守的三件事

1. **切分正确**: 分心和离开的区间经常横跨两个时段(15:35 开始的一次分心正好跨过 15:40),
   整段丢给哪一侧都会让另一侧的数字凭空好看或难看。
2. **可加性**: 所有时段(含"时间表外"那一桶)加起来必须等于 `day_stats` 的全天总数 ——
   否则表里的数字会和标题上的数字打架, 而没有人会去手算, 它只会静默地骗人。
3. **诚实**: 还没到的时段写「未到」而不是 0%; 已经过去却一分钟都没监控到的时段写
   「无监控数据」而不是 0% —— 没有数据不等于没专注。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest
from datetime import date, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.config import Config  # noqa: E402
from attention_please.report import _hm_short, build_report  # noqa: E402
from attention_please.store import OUTSIDE_NAME, Store  # noqa: E402

DAY = "2026-09-18"

RAW = {
    "general": {"tick_hz": 5},
    "schedule": {"block": [
        {"name": "高数强化听课", "start": "08:30", "end": "10:30", "kind": "focus"},
        {"name": "数学刷题", "start": "10:40", "end": "11:30", "kind": "focus"},
        {"name": "午休", "start": "11:30", "end": "13:00", "kind": "rest"},
        {"name": "英语单词", "start": "15:10", "end": "15:40", "kind": "focus",
         "allow_phone": True},
        {"name": "英语真题精读", "start": "15:40", "end": "16:40", "kind": "focus"},
    ]},
    "report": {"report_dir": "data/reports"},
}


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(2026, 9, 18, hh, mm, ss)


class TestCellFormatter(unittest.TestCase):
    """单元格里的时长格式: 不足一分钟用秒, 但零头不许写成"0 秒"。"""

    def test_seconds_for_sub_minute_values(self):
        self.assertEqual(_hm_short(20), "20 秒")
        self.assertEqual(_hm_short(59.4), "59 秒")

    def test_zero_and_rounding_dust_stay_in_minutes(self):
        self.assertEqual(_hm_short(0.0), "0 分钟")
        self.assertEqual(_hm_short(0.4), "0 分钟")

    def test_minutes_and_hours(self):
        self.assertEqual(_hm_short(60), "1 分钟")
        self.assertEqual(_hm_short(5400), "1 小时 30 分")


class TestBlockWindows(unittest.TestCase):
    """`Schedule.block_windows` 的边界 —— 它错了, 上面所有统计都错。"""

    def test_only_focus_blocks_sorted_by_start(self):
        cfg = Config.from_dict(RAW)
        wins = cfg.schedule.block_windows(date(2026, 9, 18))
        self.assertEqual([w[0] for w in wins],
                         ["高数强化听课", "数学刷题", "英语单词", "英语真题精读"])
        # 休息块不进统计(作息表里它的含义就是"不监控")
        self.assertNotIn("午休", [w[0] for w in wins])
        self.assertTrue(all(w[1] < w[2] for w in wins))

    def test_epochs_land_on_the_right_clock(self):
        cfg = Config.from_dict(RAW)
        _name, start, end = cfg.schedule.block_windows(date(2026, 9, 18))[0]
        self.assertEqual(datetime.fromtimestamp(start), at(8, 30))
        self.assertEqual(datetime.fromtimestamp(end), at(10, 30))

    def test_cross_midnight_ends_next_day(self):
        cfg = Config.from_dict({"schedule": {"block": [
            {"name": "深夜", "start": "23:30", "end": "01:00"}]}})
        _n, start, end = cfg.schedule.block_windows(date(2026, 9, 18))[0]
        self.assertEqual(end - start, 5400)
        self.assertEqual(datetime.fromtimestamp(end), datetime(2026, 9, 19, 1, 0))

    def test_empty_block_is_dropped_not_24h(self):
        """start == end 是空块(`contains()` 永不命中), 不能变成 24 小时。"""
        cfg = Config.from_dict({"schedule": {"block": [
            {"name": "空", "start": "09:00", "end": "09:00"}]}})
        self.assertEqual(cfg.schedule.block_windows(date(2026, 9, 18)), [])
        self.assertEqual(cfg.schedule.planned_seconds(), 0.0)


class BlockCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.cfg = Config.from_dict(RAW, path=root / "config.toml")
        self.store = Store(root / "data" / "events.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def fill_minute(self, hh: int, mm: int, face: int | None = None, ticks: int = 300):
        """填满一分钟的覆盖率数据。`face=0` = 整分钟脸都看不到(看不清)。"""
        for i in range(ticks):
            self.store.coverage_tick(at(hh, mm, 0), pose_hit=True,
                                     face_hit=(face is None or i < face), judging=True)

    def blocks(self) -> dict:
        wins = self.cfg.schedule.block_windows(date(2026, 9, 18))
        return {b.name: b for b in self.store.block_stats(DAY, wins, tick_hz=5)}


class TestSplitting(BlockCase):
    def test_minutes_land_in_their_own_block(self):
        self.fill_minute(9, 0)
        self.fill_minute(16, 0)
        got = self.blocks()
        self.assertAlmostEqual(got["高数强化听课"].monitored_seconds, 60)
        self.assertAlmostEqual(got["英语真题精读"].monitored_seconds, 60)
        self.assertAlmostEqual(got["数学刷题"].monitored_seconds, 0)
        self.assertNotIn(OUTSIDE_NAME, got)      # 没数据就不印这一行

    def test_distraction_spanning_two_blocks_is_split(self):
        """15:35 开始、持续 10 分钟的分心跨过 15:40, 必须两边各 5 分钟。"""
        self.store.event("episode_end", at=at(15, 45), signal="screen", duration=600)
        got = self.blocks()
        self.assertAlmostEqual(got["英语单词"].distract_seconds, 300)
        self.assertAlmostEqual(got["英语真题精读"].distract_seconds, 300)
        # 次数只算一次, 落在分集**结束**的那一刻所属的时段
        self.assertEqual(got["英语真题精读"].episodes, 1)
        self.assertEqual(got["英语单词"].episodes, 0)

    def test_away_spanning_block_and_gap_is_split(self):
        """15:00 开始、持续 20 分钟的离开: 10 分钟在表外(15:00-15:10), 10 分钟在英语单词。"""
        self.store.event("away_end", at=at(15, 20), duration=1200)
        got = self.blocks()
        self.assertAlmostEqual(got["英语单词"].away_seconds, 600)
        self.assertAlmostEqual(got[OUTSIDE_NAME].away_seconds, 600)

    def test_away_is_subtracted_from_blind_within_the_block(self):
        """离开的时间不许同时算进"看不清"(否则有效专注被扣两遍)。"""
        for m in range(10, 20):
            self.fill_minute(15, m, face=0)          # 10 分钟看不清
        self.store.event("away_end", at=at(15, 15), duration=180)   # 15:12-15:15 人不在
        got = self.blocks()
        self.assertAlmostEqual(got["英语单词"].blind_seconds, 600 - 180)
        self.assertAlmostEqual(got["英语单词"].away_seconds, 180)

    def test_outside_schedule_time_gets_its_own_row(self):
        """手动"现在开始学习"落在作息表外, 必须单独成行, 不能算进任何时段。"""
        self.fill_minute(12, 0)
        got = self.blocks()
        self.assertAlmostEqual(got[OUTSIDE_NAME].monitored_seconds, 60)
        for name in ("高数强化听课", "数学刷题", "英语单词", "英语真题精读"):
            self.assertAlmostEqual(got[name].monitored_seconds, 0)

    def test_overlapping_blocks_do_not_double_count(self):
        """作息表万一写重叠了: 靠前的窗口优先(和 Schedule.block_at 同语义), 不重复计数。"""
        cfg = Config.from_dict({"general": {"tick_hz": 5}, "schedule": {"block": [
            {"name": "甲", "start": "09:00", "end": "10:00"},
            {"name": "乙", "start": "09:30", "end": "10:30"}]}})
        self.cfg = cfg
        self.fill_minute(9, 30)
        wins = cfg.schedule.block_windows(date(2026, 9, 18))
        rows = {b.name: b for b in self.store.block_stats(DAY, wins, tick_hz=5)}
        self.assertAlmostEqual(rows["甲"].monitored_seconds, 60)
        self.assertAlmostEqual(rows["乙"].monitored_seconds, 0)
        self.assertAlmostEqual(rows["甲"].monitored_seconds + rows["乙"].monitored_seconds, 60)

    def test_allow_phone_is_recorded_not_counted(self):
        self.fill_minute(15, 10)
        self.store.event("episode_end", at=at(15, 11), signal="phone", duration=60,
                         detail="record_only")
        got = self.blocks()
        self.assertAlmostEqual(got["英语单词"].distract_seconds, 0)
        self.assertAlmostEqual(got["英语单词"].allowed_phone_seconds, 60)
        self.assertAlmostEqual(got["英语单词"].focus_seconds, 60)

    def test_wrong_episode_is_excluded_from_that_block(self):
        self.fill_minute(9, 0)
        self.store.event("episode_end", at=at(9, 1), signal="screen", duration=60,
                         detail="wrong")
        got = self.blocks()
        self.assertAlmostEqual(got["高数强化听课"].distract_seconds, 0)
        self.assertAlmostEqual(got["高数强化听课"].wrong_seconds, 60)
        self.assertAlmostEqual(got["高数强化听课"].focus_seconds, 60)

    def test_pose_episode_is_not_a_distraction(self):
        self.fill_minute(9, 0)
        self.store.event("episode_end", at=at(9, 1), signal="pose", duration=60)
        got = self.blocks()
        self.assertAlmostEqual(got["高数强化听课"].distract_seconds, 0)
        self.assertAlmostEqual(got["高数强化听课"].focus_seconds, 60)

    def test_away_episode_end_is_not_counted_twice(self):
        """away 的时长走 away_end; 它的 episode_end 再计一次就是重复扣分。"""
        self.fill_minute(9, 0)
        self.fill_minute(9, 1)
        self.store.event("away_start", at=at(9, 0))
        self.store.event("away_end", at=at(9, 1), signal="away", duration=60)
        self.store.event("episode_end", at=at(9, 1), signal="away", duration=60)
        got = self.blocks()
        self.assertAlmostEqual(got["高数强化听课"].away_seconds, 60)
        self.assertAlmostEqual(got["高数强化听课"].distract_seconds, 0)
        self.assertAlmostEqual(got["高数强化听课"].focus_seconds, 60)


class TestAdditivity(BlockCase):
    """分时段表的每一列加起来, 必须等于 day_stats 的全天总数。"""

    def _realistic_day(self):
        for m in range(0, 10):
            self.fill_minute(9, m)                       # 高数: 10 分钟
        for m in range(40, 50):
            self.fill_minute(10, m, face=(0 if m < 45 else None))   # 前 5 分钟看不清
        for m in range(10, 20):
            self.fill_minute(15, m)                      # 英语单词
        for m in range(0, 10):
            self.fill_minute(16, m)                      # 真题精读
        for m in range(0, 3):
            self.fill_minute(12, m)                      # 表外(手动学习)
        self.store.event("episode_end", at=at(10, 44), signal="screen", duration=120)
        self.store.event("episode_end", at=at(15, 14), signal="phone", duration=120,
                         detail="record_only")
        self.store.event("episode_end", at=at(15, 45), signal="screen", duration=600)
        self.store.event("away_end", at=at(16, 5), duration=180)
        self.store.event("episode_end", at=at(12, 1), signal="screen", duration=60,
                         detail="wrong")

    def test_every_column_adds_up(self):
        self._realistic_day()
        st = self.store.day_stats(DAY, tick_hz=5)
        wins = self.cfg.schedule.block_windows(date(2026, 9, 18))
        rows = self.store.block_stats(DAY, wins, tick_hz=5)
        self.assertAlmostEqual(sum(b.monitored_seconds for b in rows),
                               st.monitored_seconds, places=6)
        self.assertAlmostEqual(sum(b.distract_seconds for b in rows),
                               st.distract_seconds, places=6)
        self.assertAlmostEqual(sum(b.blind_seconds for b in rows),
                               st.blind_seconds, places=6)
        self.assertAlmostEqual(sum(b.away_seconds for b in rows),
                               st.away_seconds, places=6)
        self.assertAlmostEqual(sum(b.allowed_phone_seconds for b in rows),
                               st.allowed_phone_seconds, places=6)
        self.assertAlmostEqual(sum(b.wrong_seconds for b in rows),
                               st.wrong_seconds, places=6)
        self.assertEqual(sum(b.episodes for b in rows), st.episodes)
        # 有效专注是"每段各自兜底"的, 只要没有哪一段被扣成负数, 它也必须对得上
        self.assertAlmostEqual(sum(b.focus_seconds for b in rows),
                               st.focus_seconds, places=6)

    def test_report_does_not_warn_about_mismatch_on_a_clean_day(self):
        self._realistic_day()
        text = build_report(self.cfg, self.store, DAY, now=at(23, 0))
        self.assertNotIn("以合计为准", text)


class TestReportSection(BlockCase):
    def test_table_lists_every_block_and_a_total(self):
        self.fill_minute(9, 0)
        text = build_report(self.cfg, self.store, DAY, now=at(23, 0))
        self.assertIn("## 各时段专注情况(按作息表)", text)
        self.assertIn("08:30–10:30 高数强化听课", text)
        self.assertIn("15:10–15:40 英语单词", text)
        self.assertIn("| **合计** |", text)
        # 合计必须和标题上的数字一致
        self.assertIn("**1 分钟**", text)

    def test_future_block_says_not_yet_instead_of_zero_percent(self):
        """中午打开日报时, 下午那个 0% 是假的 —— 必须写「未到」。"""
        self.fill_minute(9, 0)
        text = build_report(self.cfg, self.store, DAY, now=at(12, 0))
        self.assertIn("(未到)", text)
        # 未到的那一行整行都是破折号, 不能出现"0 分钟 / 0%"
        self.assertIn("| 15:10–15:40 英语单词(未到) | 30 分钟 | — | — | — | — | — | — |", text)
        self.assertIn("| 15:40–16:40 英语真题精读(未到) | 1 小时 00 分 | — | — | — | — | — | — |", text)
        # 上午那段已经过去了, 既不是"未到"也不是"进行中"
        self.assertIn("| 08:30–10:30 高数强化听课 |", text)

    def test_running_block_is_marked(self):
        self.fill_minute(9, 0)
        text = build_report(self.cfg, self.store, DAY, now=at(9, 30))
        self.assertIn("08:30–10:30 高数强化听课(进行中)", text)
        self.assertIn("(未到)", text)          # 下午的块仍然是"未到"

    def test_elapsed_block_without_data_is_not_reported_as_zero_percent(self):
        """没有数据不等于没专注: 该写「无监控数据」, 不能写 0%。"""
        text = build_report(self.cfg, self.store, DAY, now=at(12, 0))
        self.assertIn("无监控数据", text)
        self.assertNotIn("| 0% |", text)

    def test_outside_row_and_notes(self):
        self.fill_minute(15, 10)
        self.store.event("episode_end", at=at(15, 11), signal="phone", duration=60,
                         detail="record_only")
        self.store.event("episode_end", at=at(15, 12), signal="screen", duration=60,
                         detail="wrong")
        text = build_report(self.cfg, self.store, DAY, now=at(23, 0))
        self.assertIn("「允许用手机」", text)
        self.assertIn("判为误报", text)

    def test_unfinished_episode_is_not_attributed_to_any_block(self):
        """没等到收尾的分心连时长都不知道, 归不到任何时段 —— 必须说明它不在表里。"""
        self.fill_minute(15, 10)
        self.store.event("episode_start", at=at(15, 11), signal="screen")
        self.store.event("nudge", at=at(15, 11), signal="screen", level=1, detail="微信")
        text = build_report(self.cfg, self.store, DAY, now=at(23, 0))
        self.assertIn("归不到任何时段", text)
        self.assertIn("| 15:10–15:40 英语单词 | 30 分钟 | 1 分钟 | 1 分钟 | 100% | 0 分钟 |",
                      text)

    def test_no_schedule_says_so(self):
        self.cfg = Config.from_dict({"schedule": {"block": []}})
        self.fill_minute(9, 0)
        text = build_report(self.cfg, self.store, DAY, now=at(23, 0))
        self.assertIn("作息表里没有 focus 时段", text)

    def test_tiny_outside_spillover_does_not_get_its_own_row(self):
        """真实数据里必然有一点点"表外"秒数(离开的尾巴跨过时段结尾)。

        20 秒不值得占一行 `时间表外 | — | 0 分钟 | …`(实测 2026-09-25 日报里就是这种
        全 0 行, 看着像记账出错), 但**它仍然算在差额校验里** —— 藏的是显示, 不是账。
        """
        self.fill_minute(15, 10)
        self.store.event("away_end", at=at(15, 41), duration=20)   # 15:40:40-15:41:00
        text = build_report(self.cfg, self.store, DAY, now=at(23, 0))
        self.assertNotIn(f"| {OUTSIDE_NAME} |", text)
        self.assertNotIn("以合计为准", text)      # 藏起来之后校验仍然必须通过

    def test_sub_minute_cell_shows_seconds_not_a_fake_zero(self):
        self.fill_minute(15, 10)
        self.store.event("episode_end", at=at(15, 11), signal="screen", duration=20)
        text = build_report(self.cfg, self.store, DAY, now=at(23, 0))
        self.assertIn("| 20 秒(1 次) |", text)
        self.assertNotIn("英语单词 | 30 分钟 | 1 分钟 | 40 秒 | 67% | 0 分钟(1 次)", text)

    def test_consistency_warning_when_a_block_is_clamped(self):
        """某段的离开/分心超过它自己的监控时长时会按 0 兜底 —— 差额必须写出来。

        构造: 甲段只有 1 分钟监控却有 1 小时离开(逐段兜底), 乙段正常。
        全天只看一次兜底, 于是"各段之和"必然大于全天总数。
        """
        self.cfg = Config.from_dict({"general": {"tick_hz": 5}, "schedule": {"block": [
            {"name": "甲", "start": "09:00", "end": "10:00"},
            {"name": "乙", "start": "11:00", "end": "12:00"}]}})
        self.fill_minute(9, 0)
        self.fill_minute(11, 0)
        self.store.event("away_end", at=at(10, 0), duration=3600)
        text = build_report(self.cfg, self.store, DAY, now=at(23, 0))
        self.assertIn("以合计为准", text)


if __name__ == "__main__":
    unittest.main()
