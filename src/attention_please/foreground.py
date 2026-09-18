"""前台窗口标题(判定"切到了娱乐窗口"的唯一依据)。

用 win32gui 拿 GetForegroundWindow 的标题。拿不到(锁屏/桌面/权限问题)时返回 None,
上层按"未知"处理 —— 未知永远不算分心, 宁可漏报。
"""
from __future__ import annotations

import sys

if sys.platform == "win32":
    import win32gui  # type: ignore

    def foreground_title() -> str | None:
        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return None
            title = win32gui.GetWindowText(hwnd)
            if not title:
                # 有些窗口(如 Windows 10 的"开始"、UWP 壳)标题为空, 退回类名
                return None
            return title
        except Exception:  # noqa: BLE001 - 前台窗口查询失败绝不能拖垮主循环
            return None

    def foreground_process_name() -> str | None:
        """进程名(用于日报里"分心前在看什么"), 失败返回 None。"""
        try:
            import win32process
            import psutil  # 没装就退回 None
        except Exception:  # noqa: BLE001
            return None
        try:
            hwnd = win32gui.GetForegroundWindow()
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            return psutil.Process(pid).name()
        except Exception:  # noqa: BLE001
            return None

else:

    def foreground_title() -> str | None:
        return None

    def foreground_process_name() -> str | None:
        return None


if __name__ == "__main__":
    import time

    for _ in range(10):
        print(repr(foreground_title()))
        time.sleep(1)
