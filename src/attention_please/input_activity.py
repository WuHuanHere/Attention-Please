"""全局键鼠活动(窗口信号的辅助证据: "已 N 秒没有键鼠操作")。

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

    # ⚠️ **必须显式声明返回类型**。ctypes 不声明时默认 restype 是 `c_int`(有符号 32 位),
    # 而 `GetTickCount()` 返回的是 DWORD —— 开机满 2^31 毫秒(约 **24.85 天**)之后,
    # 它会被读成负数, 于是 `now - info.dwTime` 恒为负, `idle_seconds()` **永远返回 0**,
    # 「已 N 秒没有键鼠操作」这句证据文案就再也不会出现(实测模拟 25 天: 返回 0.0)。
    _kernel32.GetTickCount.restype = wintypes.DWORD
    _kernel32.GetTickCount.argtypes = []
    _user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
    _user32.GetLastInputInfo.restype = wintypes.BOOL

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
