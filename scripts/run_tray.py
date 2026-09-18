"""带托盘的启动入口(开机自启用这个, 无控制台窗口)。

线程分工:
  - **主线程跑 pystray**(托盘的消息循环必须是主线程, 这是 Windows 上的稳妥做法);
  - 工作线程跑监控主循环(runtime.run());
  - 托盘菜单的动作通过线程安全的接口(runtime.request_pause / resume / stop)影响主循环。

试跑阶段(要看终端输出)请用 scripts\\run.py; 日常和自启用这个。
"""
from __future__ import annotations

import pathlib
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.config import Calibration, Config  # noqa: E402
from attention_please.runtime import Runtime, acquire_single_instance  # noqa: E402
from attention_please.tray import Tray  # noqa: E402


def main() -> int:
    cfg = Config.load()
    cal = Calibration.load()
    if not cal.is_calibrated:
        print("还没校准, 先跑 scripts\\calibrate.py")
        return 2

    lock = acquire_single_instance()
    if lock is None:
        print("已经有一个 attention_please 在跑。")
        return 3

    rt = Runtime(cfg, cal)
    worker = threading.Thread(target=rt.run, name="monitor", daemon=True)
    worker.start()

    tray = Tray(rt, cfg.report_dir)
    try:
        tray.run()          # 阻塞在主线程, 直到菜单里点"退出"
    except KeyboardInterrupt:
        pass
    finally:
        rt.stop()
        worker.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
