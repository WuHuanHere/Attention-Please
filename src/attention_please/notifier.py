"""提醒层: 按可信度分级的提示音。

为什么是分级而不是"响一声":
  - 高可信信号(窗口标题命中黑名单)可以一路升级到"连续响";
  - 推断出来的信号(看手机)最多几声 —— 因为一旦升级力度用错,
    误报就不是"烦一下"而是"想卸载"。
分级上限由 signals.py 的 max_level_* 控制, 这里只负责把级别翻译成具体声音。

M4 会加"带证据的置顶弹窗 + 判定错了按钮"; 这一层保持可替换, 不把 UI 写死在里面。
"""
from __future__ import annotations

import sys
import threading
import time

from .logbook import safe_print

# (频率 Hz, 时长 ms); 频率 0 表示静音间隔
LEVEL_PATTERNS: dict[int, list[tuple[int, int]]] = {
    1: [(880, 130)],
    2: [(880, 110), (0, 70), (880, 110), (0, 70), (880, 110)],
    3: [(1046, 150), (0, 80), (1046, 150), (0, 80), (784, 200), (0, 100)] * 4,
}

LEVEL_LABEL = {1: "一声", 2: "三声", 3: "连续"}


class Notifier:
    def __init__(self, min_gap_seconds: float = 1.0):
        self.min_gap_seconds = min_gap_seconds
        self._lock = threading.Lock()
        self._last_played = 0.0
        self._winsound = None
        if sys.platform == "win32":
            try:
                import winsound
                self._winsound = winsound
            except Exception:  # noqa: BLE001
                self._winsound = None

    def buzz(self, level: int, *, sound: bool = True, label: str = "") -> None:
        """非阻塞播放。sound=False(安静时段)时只打印, 不发声。"""
        pattern = LEVEL_PATTERNS.get(level, LEVEL_PATTERNS[1])
        if not sound:
            safe_print(f"   (安静时段: 不发声, 只记录) {label}")
            return
        threading.Thread(target=self._play, args=(pattern,), daemon=True).start()

    def _play(self, pattern: list[tuple[int, int]]) -> None:
        with self._lock:
            now = time.monotonic()
            if now - self._last_played < self.min_gap_seconds:
                # 同一个 tick 里两条提醒(比如"头转向手机"和"切到黑名单窗口"同时越过门槛)
                # 时第二声会被压掉。压掉可以, **但不能一声不响** —— 用户只听到一声,
                # 而事件流里两条都在, 事后对不上账。
                safe_print(f"   (提醒声被最小间隔压掉: 距上一声 "
                           f"{now - self._last_played:.1f}s < {self.min_gap_seconds}s; "
                           f"提醒本身已经入库了)")
                return
            self._last_played = now
            for freq, dur in pattern:
                if freq <= 0:
                    time.sleep(dur / 1000.0)
                    continue
                if self._winsound is not None:
                    try:
                        self._winsound.Beep(int(freq), int(dur))
                        continue
                    except Exception:  # noqa: BLE001 - 没有蜂鸣器就退回终端响铃
                        self._winsound = None
                sys.stdout.write("\a")
                sys.stdout.flush()
                time.sleep(dur / 1000.0)


if __name__ == "__main__":
    n = Notifier()
    for lvl in (1, 2, 3):
        safe_print(f"level {lvl}: {LEVEL_LABEL[lvl]}")
        n.buzz(lvl)
        time.sleep(3)
