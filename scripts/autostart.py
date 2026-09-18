"""开机自启的安装 / 卸载 / 检查。

刻意用 Python + pywin32 而不是 .ps1:
  - 本机 PowerShell 5.1 会把"UTF-8 无 BOM + 中文"的脚本读成 ANSI 直接解析失败
    (这个坑在 scripts/download_models.ps1 上已经踩过一次);
  - 这台机器的 PowerShell schannel 本身就不正常。
Python 两个问题都没有, 而且 pywin32 已经是运行依赖。

实现方式是在"启动"文件夹里放一个快捷方式(不是注册表、不是计划任务):
**看得见、删得掉**, 你随时可以手动把那个快捷方式拖进回收站。

用法:
  .\\.venv\\Scripts\\python.exe scripts\\autostart.py status
  .\\.venv\\Scripts\\python.exe scripts\\autostart.py install
  .\\.venv\\Scripts\\python.exe scripts\\autostart.py uninstall
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LNK_NAME = "attention_please.lnk"


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise SystemExit("找不到 %APPDATA%, 这个脚本只能在 Windows 上跑。")
    return (Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / "Startup")


def shortcut_path() -> Path:
    return startup_dir() / LNK_NAME


def pythonw_path() -> Path:
    return ROOT / ".venv" / "Scripts" / "pythonw.exe"


def target_script() -> Path:
    return ROOT / "scripts" / "run_tray.py"


def cmd_status() -> int:
    lnk = shortcut_path()
    print(f"启动文件夹: {startup_dir()}")
    print(f"快捷方式  : {lnk}")
    if not lnk.exists():
        print("状态      : 未安装")
        return 1
    try:
        import win32com.client

        shell = win32com.client.Dispatch("WScript.Shell")
        sc = shell.CreateShortcut(str(lnk))
        print(f"状态      : 已安装")
        print(f"  -> 目标  : {sc.TargetPath}")
        print(f"  -> 参数  : {sc.Arguments}")
        print(f"  -> 工作目录: {sc.WorkingDirectory}")
    except Exception as exc:  # noqa: BLE001
        print(f"状态      : 已安装(读取详情失败: {exc})")
    return 0


def cmd_install() -> int:
    import win32com.client

    pythonw = pythonw_path()
    script = target_script()
    if not pythonw.exists():
        print(f"找不到 {pythonw}\n先在项目根目录建虚拟环境并装依赖。")
        return 2
    if not script.exists():
        print(f"找不到 {script}")
        return 2

    startup_dir().mkdir(parents=True, exist_ok=True)
    lnk = shortcut_path()
    shell = win32com.client.Dispatch("WScript.Shell")
    sc = shell.CreateShortcut(str(lnk))
    sc.TargetPath = str(pythonw)
    sc.Arguments = f'"{script}"'
    sc.WorkingDirectory = str(ROOT)
    sc.WindowStyle = 7          # 最小化; pythonw 本来就没有窗口
    sc.Description = "attention_please 考研专注监控"
    sc.Save()
    print(f"已安装开机自启: {lnk}")
    print(f"  {pythonw} \"{script}\"")
    print("\n下次登录会自动启动(托盘图标)。想撤销就 uninstall, 或直接删掉那个快捷方式。")
    print("现在就试: 双击 .venv\\Scripts\\pythonw.exe 是没用的, 请直接在弹出的托盘里验证:")
    print("  .\\.venv\\Scripts\\pythonw.exe scripts\\run_tray.py")
    return 0


def cmd_uninstall() -> int:
    lnk = shortcut_path()
    if not lnk.exists():
        print("本来就没安装。")
        return 0
    lnk.unlink()
    print(f"已卸载: {lnk}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="attention_please 开机自启管理")
    ap.add_argument("action", choices=["status", "install", "uninstall"])
    args = ap.parse_args()
    return {"status": cmd_status, "install": cmd_install,
            "uninstall": cmd_uninstall}[args.action]()


if __name__ == "__main__":
    sys.exit(main())
