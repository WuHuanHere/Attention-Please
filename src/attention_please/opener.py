"""用外部程序打开文件(只有一处实现, 免得两个模块各写一份)。

为什么需要专门一个模块:
  这台机器上 `.md` **没有文件关联**(`assoc .md` 报 "File association not found"),
  `os.startfile` 会静默失败 —— 表现就是"点了'打开今日日报'毫无反应"。
  而且你的 VS Code 装在 **D 盘**, 也不能写死 C 盘路径。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def viewer_candidates(code_on_path: str | None,
                      env: dict[str, str]) -> list[tuple[str, str]]:
    """返回可以打开 .md 的候选程序 [(显示名, 可执行文件路径)]。纯函数, 便于测试。"""
    out: list[tuple[str, str]] = []
    if code_on_path:
        p = Path(code_on_path)
        if p.suffix.lower() in (".cmd", ".bat"):
            # <安装目录>\bin\code.cmd -> <安装目录>\Code.exe
            out.append(("VS Code", str(p.parent.parent / "Code.exe")))
        out.append(("VS Code", str(p)))
    for key in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        base = env.get(key)
        if not base:
            continue
        out.append(("VS Code",
                    str(Path(base) / "Programs" / "Microsoft VS Code" / "Code.exe")))
        out.append(("VS Code", str(Path(base) / "Microsoft VS Code" / "Code.exe")))
    return out


def find_markdown_viewer() -> tuple[str, str] | None:
    """挑一个真实存在的查看器; 找不到就返回 None。"""
    for name, path in viewer_candidates(shutil.which("code"), dict(os.environ)):
        p = Path(path)
        if p.suffix.lower() == ".exe" and p.exists():
            return name, str(p)
    notepad = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "notepad.exe"
    if notepad.exists():
        return "记事本", str(notepad)
    return None


def open_path(path: Path) -> tuple[bool, str]:
    """打开文件, 返回 (是否成功, 用了什么程序)。

    注意: 从后台进程启动的 GUI **抢不到前台**(Windows 前台锁), 窗口可能开在别的窗口后面。
    所以日报的首选展示方式是项目自带的 Tk 查看器(ui.show_report),
    这个函数只作为"我想用 VS Code 看"的次要选项。
    """
    path = Path(path)
    viewer = find_markdown_viewer()
    if viewer is not None:
        name, exe = viewer
        try:
            subprocess.Popen([exe, str(path)])  # noqa: S603 - 路径来自本机探测
            return True, name
        except Exception:  # noqa: BLE001
            pass
    try:
        os.startfile(str(path))  # noqa: S606
        return True, "系统默认程序"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def open_folder(folder: Path) -> tuple[bool, str]:
    folder = Path(folder)
    try:
        os.startfile(str(folder))  # noqa: S606
        return True, "资源管理器"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
