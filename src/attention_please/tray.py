"""系统托盘图标与菜单。

状态用颜色区分(托盘图标很小, 颜色比文字快):
  绿 = 判定中   灰 = 待机(时间表外)   黄 = 已暂停   红 = 摄像头不可用
  蓝 = 人不在画面里(只记录)   紫 = 已让出摄像头(给别的程序用)

菜单里的"暂停"必须填理由 —— 这就是"有代价的暂停"的代价本身, 理由会进日报。
输入框走 ui.AlertUI(整个进程只有一个 Tk 实例), 所以这里只发请求、不等结果。
"""
from __future__ import annotations

import os
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw

from . import capture
from .opener import open_folder, open_path

ICON_SIZE = 64
STATE_COLORS = {
    "judging": (46, 204, 113),
    "idle": (149, 165, 166),
    "paused": (241, 196, 15),
    "camera_busy": (231, 76, 60),
    "away": (52, 152, 219),      # 蓝: 人不在画面里(只记录, 不提醒)
    "yielded": (155, 89, 182),   # 紫: 摄像头让给别的程序了(会议/通话/直播)
}


def make_icon(state: str = "idle") -> Image.Image:
    color = STATE_COLORS.get(state, STATE_COLORS["idle"])
    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, ICON_SIZE - 4, ICON_SIZE - 4), fill=color,
                 outline=(20, 20, 20), width=3)
    # 中间画一个"眼睛"的样子: 提醒"我在看着你"
    cy = ICON_SIZE // 2
    draw.ellipse((18, cy - 10, 46, cy + 10), outline=(20, 20, 20), width=4)
    draw.ellipse((28, cy - 5, 38, cy + 5), fill=(20, 20, 20))
    return img


def viewer_candidates(code_on_path: str | None,
                      env: dict[str, str]) -> list[tuple[str, str]]:
    """(已移到 opener.py, 这里保留一层转发以免旧引用失效)"""
    from .opener import viewer_candidates as impl

    return impl(code_on_path, env)


def find_markdown_viewer() -> tuple[str, str] | None:
    """(已移到 opener.py)"""
    from .opener import find_markdown_viewer as impl

    return impl()


class Tray:
    def __init__(self, rt, report_dir: Path):
        self.rt = rt
        self.report_dir = report_dir
        self.icon = None
        self._last_state = ""

    # ---- 菜单动作 ----
    def _pause(self) -> None:
        self.rt.request_pause()

    def _resume(self) -> None:
        self.rt.resume(datetime.now())
        self._refresh_icon()

    def _manual(self) -> None:
        self.rt.start_manual_session(60)
        self._notify("已开始 60 分钟手动学习(时间表外也会判定)")

    def _yield(self) -> None:
        """把摄像头让给别的程序(会议/通话/直播)。

        给"关键词匹配不到"的软件兜底 —— 你打开别的软件发现拿不到摄像头时, 点这个。
        """
        mins = self.rt.cfg.camera_yield.manual_minutes
        self.rt.yield_camera(mins)
        self._notify(f"已让出摄像头 {mins} 分钟(判定暂停, 会记进日报)")
        self._refresh_icon()

    def _reclaim(self) -> None:
        self.rt.reclaim_camera()
        self._notify("已收回摄像头, 恢复判定")
        self._refresh_icon()

    def _open_report(self) -> None:
        """生成(或读取)今天的日报, 然后**用自己的窗口显示**。

        实测: VS Code 确实打开了文件, 但从后台进程启动的 GUI 抢不到前台,
        窗口开在别的窗口后面 —— 表现就是"点了没反应"。所以主展示路径改成
        自带的 Tk 查看器(一定能看见), VS Code 只作为窗口里的次要按钮。
        """
        today = datetime.now().date().isoformat()
        path = self.report_dir / f"{today}.md"
        text: str | None = None
        try:
            from .report import build_report, write_report
            text = build_report(self.rt.cfg, self.rt.store, today)
            path = write_report(self.rt.cfg, self.rt.store, today)
        except Exception as exc:  # noqa: BLE001
            self._notify(f"日报生成失败: {type(exc).__name__}: {exc}")
            return
        try:
            self.rt.ui.show_report(f"attention_please — {today} 专注日报",
                                   text or "", str(path))
        except Exception as exc:  # noqa: BLE001
            ok, how = open_path(path)
            self._notify(f"窗口显示失败({exc}), 已改用 {how}" if ok
                         else f"日报在 {path}(打不开: {how})")

    def _open_captures(self) -> None:
        target = self.rt.cfg.capture_dir
        if not target.exists() or not any(target.glob("*.jpg")):
            self._notify("还没有分心截图(分心发生时才会存)")
            return
        ok, how = open_folder(target)
        if not ok:
            self._notify(f"打开文件夹失败({how});路径 {target}")

    def _clear_captures(self) -> None:
        removed = capture.clear_all(self.rt.cfg.capture_dir)
        print(f"已清空 {removed} 张截图")
        self._notify(f"已清空 {removed} 张分心截图")

    def _quit(self) -> None:
        self.rt.stop()
        if self.icon is not None:
            self.icon.stop()

    # ---- 反馈与刷新 ----
    def _notify(self, message: str, title: str = "attention_please") -> None:
        """给"没有可见效果"的菜单动作一个反馈, 否则你会以为按钮坏了。"""
        print(f"[托盘] {message}")
        if self.icon is not None:
            try:
                self.icon.notify(message, title)
            except Exception:  # noqa: BLE001
                pass

    def _refresh_icon(self) -> None:
        state = self.rt.state_key()
        self._last_state = state
        if self.icon is None:
            return
        try:
            self.icon.icon = make_icon(state)
            self.icon.title = self.rt.status_text()
        except Exception:  # noqa: BLE001
            pass

    # ---- 菜单 ----
    def _menu(self):
        import pystray

        return pystray.Menu(
            pystray.MenuItem(lambda item: self.rt.status_text(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("暂停监控(要写理由)", lambda: self._pause()),
            pystray.MenuItem("恢复学习", lambda: self._resume()),
            pystray.MenuItem("现在开始学习(60 分钟)", lambda: self._manual()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda item: f"让出摄像头({self.rt.cfg.camera_yield.manual_minutes} 分钟)",
                             lambda: self._yield()),
            pystray.MenuItem("收回摄像头", lambda: self._reclaim()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("打开今日日报", lambda: self._open_report()),
            pystray.MenuItem("查看分心截图", lambda: self._open_captures()),
            pystray.MenuItem("清空所有截图", lambda: self._clear_captures()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda: self._quit()),
        )

    def _refresh_loop(self) -> None:
        """每 2 秒刷新图标颜色和悬停提示。

        注意: 不要用 `icon.visible` 当循环条件 —— 这个线程在 icon.run() 之前启动,
        那一刻 visible 还是 False, 循环会**立刻退出**, 于是图标永远不刷新
        (实测踩到: 暂停后图标不变黄, 看起来像菜单失灵)。
        """
        while not self.rt._stop_event.wait(2.0):  # noqa: SLF001 - 同一模块族的内部协作
            try:
                self._refresh_icon()
            except Exception:  # noqa: BLE001
                pass

    def run(self) -> None:
        import pystray

        self.icon = pystray.Icon("attention_please", make_icon(self.rt.state_key()),
                                 self.rt.status_text(), self._menu())
        threading.Thread(target=self._refresh_loop, name="tray-refresh",
                         daemon=True).start()
        self.icon.run()
