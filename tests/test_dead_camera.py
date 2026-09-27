"""黑帧回归: 摄像头"还活着、但给的是纯黑帧"时, **不许**把它算成「离开座位」。

现场(2026-09-27 09:27:45, 实测):
  - DirectShow 流死了, `cap.read()` 每次等满 1000ms 超时才返回 —— 帧率从 5.0 Hz
    掉到 **0.95 Hz**(1/1.05s), 而 `ok=True`、帧**不是 None**, 只是**纯黑**;
  - 于是人脸 0% / Pose 0% -> 90 秒后判定「离开座位」, 一直错了 36 分钟;
  - 只有手动"让出 + 收回摄像头"(= release + 重开采集流)才恢复(10:07 立刻回到 5.0Hz/99%)。
  - 铁证: captures/2026-09-27_09-42-29.jpg 的下半张(摄像头画面)灰度 std = **0.05**,
    而正常画面是 **65~70**。

这个文件用"假摄像头 + 假时钟"把那段按真实 tick 循环跑一遍 —— 走的是**真正的**
`Runtime._tick()`, 不是另写一套逻辑。守两条铁律:
  - 误报最烦: 没有画面时**不许**说你离开座位;
  - 漏报必须可见: 那段时间必须变成 `camera_blank`/`camera_reopen` 事件 + 日报里的一行。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please import runtime as runtime_mod  # noqa: E402
from attention_please import store as store_mod  # noqa: E402
from attention_please.config import Calibration, Config  # noqa: E402
from attention_please.perception import FrameFeatures  # noqa: E402
from attention_please.report import build_report  # noqa: E402
from attention_please.runtime import Runtime  # noqa: E402

W, H = 640, 480                            # 小一点: 这些测试每帧都要过一遍灰度统计
TICK = 0.2                                 # 5 Hz, 和 config.toml 一致
START = datetime(2026, 9, 27, 9, 0, 0)
DAY = START.date().isoformat()

RAW = {
    "general": {"camera_index": 0, "camera_width": W, "camera_height": H, "tick_hz": 5},
    # away_seconds 从默认的 90 压到 30: 判据是"故障(20s)必须远早于离开", 30 秒照样守得住,
    # 而模拟时长能砍掉一大半(这个文件里的每一秒都是真跑一遍 _tick)。
    "detection": {"enabled_signals": ["screen", "phone", "pose"],
                  "away_seconds": 30},
    "reminder": {"max_level_high": 3, "cooldown_seconds": 180,
                 "wrong_feedback_mute_seconds": 900},
    "schedule": {"block": [{"name": "上午", "start": "08:30", "end": "10:30"}]},
    "report": {"daily_report": False, "report_dir": "data/reports"},
    "privacy": {"save_captures": False, "capture_retention_days": 7},
}


# --------------------------------------------------------------------------
# 假时钟: 让 40 分钟在毫秒级跑完, 而且逐帧确定
# --------------------------------------------------------------------------
class _Clock:
    def __init__(self, start: datetime, mono: float = 1000.0):
        self.at = start
        self.mono = mono

    def advance(self, seconds: float) -> None:
        self.at = self.at + timedelta(seconds=seconds)
        self.mono += seconds


class _FakeDatetime(datetime):
    """`now()` 走假时钟, 其余(combine/fromisoformat/算术)还是真的。"""
    clock: _Clock | None = None

    @classmethod
    def now(cls, tz=None):                       # noqa: ARG003
        assert cls.clock is not None, "测试没装假时钟"
        return cls.clock.at


class _FakeTime:
    def __init__(self, clock: _Clock):
        self.clock = clock

    def monotonic(self) -> float:
        return self.clock.mono

    def sleep(self, _seconds: float) -> None:    # 单测里不真睡
        pass


# --------------------------------------------------------------------------
# 假摄像头: 一个开关决定它给"活画面"还是"纯黑帧"
# --------------------------------------------------------------------------
_LIVE_BASE: np.ndarray | None = None


def _live_base() -> np.ndarray:
    """有结构的画面(全屏渐变 + 一点噪声)。

    降到 160x120 后 std ≈ 70, 和真实摄像头的 65 同一个量级 —— 所以它**不会**
    被"没有画面"的判据误伤, 这正是这个测试要守的边界。
    **只造一次**: 逐帧重造一张 1280x720 的随机图要 40ms, 会让单测慢到不可用。
    """
    global _LIVE_BASE
    if _LIVE_BASE is None:
        y = np.linspace(0, 255, H).astype(np.uint8)
        frame = np.repeat(y[:, None], W, axis=1)[:, :, None].repeat(3, axis=2)
        rng = np.random.default_rng(0)
        frame = (frame.astype("int16") + rng.integers(-6, 7, frame.shape)).clip(0, 255)
        _LIVE_BASE = frame.astype(np.uint8)
    return _LIVE_BASE


def _live_frame(step: int = 0):
    """在基准图上挪一个亮块 —— 帧间有真实变化(不是冻住的同一张图)。"""
    frame = _live_base().copy()
    x = 20 + (step % 60)
    frame[40:160, x:x + 80] = 240
    return frame


class _Stream:
    """摄像头状态: blank=True 时读到的每一帧都是纯黑(和 09:27 那次一模一样)。"""

    def __init__(self, blank: bool = False, heal_on_open: int | None = None):
        self.blank = blank
        self.heal_on_open = heal_on_open     # 第 N 次 open 之后自动恢复(模拟重开治好)
        self.opens = 0
        self.reads = 0
        self.step = 0
        self._black = np.zeros((H, W, 3), np.uint8)

    def open(self, index, width, height):     # noqa: ARG002 - 和 camera.open_camera 同签名
        self.opens += 1
        if self.heal_on_open is not None and self.opens >= self.heal_on_open:
            self.blank = False
        return _FakeCap(self), "FAKE"

    def read(self):
        self.reads += 1
        self.step += 1
        return True, (self._black if self.blank else _live_frame(self.step))


class _FakeCap:
    def __init__(self, stream: _Stream):
        self.stream = stream
        self.released = 0

    def read(self):
        return self.stream.read()

    def set(self, *_args):
        return True

    def get(self, prop):
        return W if prop == 3 else (H if prop == 4 else 0)

    def release(self):
        self.released += 1


class _NoopEstimator:
    def reset(self):
        pass


class _FakeAnalyzer:
    """真实 MediaPipe 在纯黑帧上的行为: 人脸和 Pose 都是 0%。"""

    def __init__(self, stream: _Stream):
        self.stream = stream
        self.pose_head_estimator = _NoopEstimator()
        self.calls = 0

    def analyze(self, frame_bgr, ts_ms=None, now=None):   # noqa: ARG002
        self.calls += 1
        live = not self.stream.blank
        return FrameFeatures(pose_present=live, face_present=live)

    def close(self):
        pass

    def close_async(self):
        pass


class DeadCameraHarness(unittest.TestCase):
    blank_std: float | None = None          # 设成 0 就等于"关掉判据"(用来证明它真的在起作用)

    def _raw(self) -> dict:
        det = dict(RAW["detection"])
        if self.blank_std is not None:
            det["blank_std"] = self.blank_std
        return {**RAW, "detection": det}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.clock = _Clock(START)
        self.stream = _Stream()
        cfg = Config.from_dict(self._raw(), path=self.root / "config.toml")
        _FakeDatetime.clock = self.clock
        self._patches = [
            mock.patch.object(runtime_mod, "time", _FakeTime(self.clock)),
            mock.patch.object(runtime_mod, "datetime", _FakeDatetime),
            mock.patch.object(store_mod, "datetime", _FakeDatetime),
            mock.patch.object(runtime_mod, "open_camera", self.stream.open),
            mock.patch.object(runtime_mod.foreground, "foreground_title", lambda: ""),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.rt = Runtime(cfg, Calibration())
        self.rt.notifier = types.SimpleNamespace(buzz=lambda *a, **k: None)
        self.rt.ui = types.SimpleNamespace(show=lambda req: None,
                                           ask_reason=lambda *a: None,
                                           drain=lambda: [])
        self.rt.analyzer = _FakeAnalyzer(self.stream)
        self.rt.cap = _FakeCap(self.stream)     # 摄像头已经开着(不让它去找真设备)
        self.said: list[str] = []
        self.rt._say = self.said.append
        self.rt.start_manual_session(120)       # 用手动学习把"判定中"钉死, 不依赖真实作息

    def tearDown(self):
        self.rt.close()
        self.tmp.cleanup()

    # ---- 跑 ----
    def run_for(self, seconds: float) -> None:
        for _ in range(int(round(seconds / TICK))):
            self.clock.advance(TICK)
            self.rt._tick()

    def events(self, kind: str) -> list[tuple]:
        cur = self.rt.store.conn.execute(
            "SELECT duration, detail FROM event WHERE kind = ? ORDER BY id", (kind,))
        return cur.fetchall()

    def stats(self):
        return self.rt.store.day_stats(DAY, tick_hz=5.0)


class TestBlackFramesAreNotAway(DeadCameraHarness):
    def test_black_frames_never_become_away(self):
        """核心回归: 黑帧期间不许出现 away_start, 而且必须留下可见的故障痕迹。"""
        self.run_for(40)                        # 1) 正常 40 秒
        normal_focus = self.stats().focus_seconds
        self.assertGreater(normal_focus, 20, "假摄像头正常时应该能记到专注")

        self.stream.blank = True                # 2) 09:27:45 —— 采集流死了
        self.stream.heal_on_open = 2            #    第一次自动重开就治好(模拟 10:06 那次)
        self.run_for(60)                        #    远超 away_seconds(30), 以前必判离开

        self.run_for(40)                        # 3) 恢复后继续正常判定

        self.assertEqual(self.events("away_start"), [],
                         "黑帧被算成了「离开座位」—— 这正是 2026-09-27 那个 bug")
        self.assertEqual(self.stats().away_seconds, 0.0)
        self.assertGreaterEqual(len(self.events("camera_blank")), 1,
                                "没有画面必须留下 camera_blank 事件(漏报必须可见)")
        self.assertGreaterEqual(len(self.events("camera_reopen")), 1,
                                "应该自动重开摄像头, 而不是等用户手动让出+收回")
        self.assertGreaterEqual(len(self.events("camera_blank_end")), 1,
                                "画面恢复也要收尾, 否则日报里那段时长是未知的")
        self.assertGreater(self.stats().camera_blank_seconds, 5.0)
        self.assertTrue(any("没有画面" in m for m in self.said),
                        f"日志里应该说清楚发生了什么, 实际: {self.said[-6:]}")
        self.assertEqual(self.rt.state_key(), "judging")

        report = build_report(self.rt.cfg, self.rt.store, DAY, now=self.clock.at)
        self.assertIn("没有画面", report)
        self.assertNotIn("离开座位 | 3", report)

    def test_monitored_time_excludes_black_frames(self):
        """没有画面的时间**不算实际监控时长** —— 否则它会被拿去当分母, 或被算成"看不清"。"""
        self.run_for(40)
        self.stream.blank = True
        self.stream.heal_on_open = 2
        self.run_for(60)
        self.run_for(40)
        st = self.stats()
        # 模拟总长 140 秒, 其中约 25 秒是黑帧; 监控时长必须明显小于 140。
        self.assertLess(st.monitored_seconds, 130.0)
        self.assertGreater(st.monitored_seconds, 80.0)

    def test_focus_resumes_after_recovery(self):
        self.run_for(40)
        before = self.stats().focus_seconds
        self.stream.blank = True
        self.stream.heal_on_open = 2
        self.run_for(60)
        self.stream.blank = False
        self.run_for(40)
        self.assertGreater(self.stats().focus_seconds, before,
                           "画面恢复后必须重新开始累计专注")


class TestPersistentBlackKeepsTrying(DeadCameraHarness):
    def test_persistent_black_retries_with_backoff_but_stays_one_episode(self):
        """一直黑下去时: 反复重开(带退避), 但**仍然只算一段**故障, 绝不算离开。"""
        self.stream.blank = True
        self.stream.heal_on_open = None         # 怎么重开都没用
        self.run_for(60)

        self.assertGreaterEqual(len(self.events("camera_reopen")), 1)
        self.assertLessEqual(len(self.events("camera_reopen")), 8,
                             "重开必须有退避, 不能每帧都去抢摄像头")
        self.assertEqual(len(self.events("camera_blank")), 1,
                         "一整段故障只该记一次起点")
        self.assertEqual(self.stats().away_seconds, 0.0)
        self.assertEqual(self.events("away_start"), [])


class TestGuardIsLoadBearing(DeadCameraHarness):
    """把判据关掉(blank_std=0), 那个 bug 必须**立刻回来**。

    这条不是多余的: 它证明上面那些 green 是**判据在起作用**, 而不是"这个场景本来
    就没事"。以后谁把判据改坏(或者不小心让它永远返回 OK), 这里会先炸给他看。
    """

    blank_std = 0.0

    def test_without_the_guard_black_frames_become_away_again(self):
        self.stream.blank = True
        self.run_for(60)
        self.assertEqual(len(self.events("camera_blank")), 0)
        self.assertEqual(len(self.events("away_start")), 1,
                         "关掉判据之后应该原样复现「黑帧 -> 离开座位」")
        # 注意这里是 unfinished_away 而不是 away_seconds: 那一段"离开"一直开着没有收尾
        # (人根本没走, 所以永远等不到"回到座位"), 而日报的离开时长只认 away_end ——
        # 也就是说真实事故里它连"离开 40 分钟"都要等到让出摄像头那一刻才结出来。
        self.assertEqual(self.stats().unfinished_away, 1)


if __name__ == "__main__":
    unittest.main()
