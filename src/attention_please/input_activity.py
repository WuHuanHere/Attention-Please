"""全局键鼠活动(判断"发呆"的一个辅助证据)。

GetLastInputInfo 是 Windows 的标准做法, 不需要管理员权限、不装钩子、
不记录你按了什么 —— 只回答"距离上一次任何输入过了多少秒"。
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

if sys.platform == "win32":

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    def idle_seconds() -> float:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not _user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        now = _kernel32.GetTickCount()
        return max(0.0, (now - info.dwTime) / 1000.0)

else:  # 只为让纯逻辑单测能在别的平台上跑

    def idle_seconds() -> float:
        return 0.0


if __name__ == "__main__":
    import time

    for _ in range(3):
        print(f"idle: {idle_seconds():.1f}s")
        time.sleep(1)
