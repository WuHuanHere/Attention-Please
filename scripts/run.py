"""启动监控(M3 试跑用)。Ctrl+C 退出。

用法:
  .\\.venv\\Scripts\\python.exe scripts\\run.py

它做什么:
  - 按 config.toml 的作息表, 在 focus 时间段内自动开始判定, 区间外待机;
  - 判定结果实时打印在终端(便于试跑时看它凭什么提醒), 同时写入 data/events.sqlite3;
  - 每 5 分钟打一次今日进度(有效专注/分心/看不清/人脸覆盖率);
  - 摄像头打不开时会重试并把这 段"漏检"记进日志。
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from attention_please.config import Calibration, Config  # noqa: E402
from attention_please.runtime import Runtime, acquire_single_instance  # noqa: E402


def main() -> int:
    cfg = Config.load()
    cal = Calibration.load()

    if not cal.is_calibrated:
        print("还没校准。先跑: .\\.venv\\Scripts\\python.exe scripts\\calibrate.py")
        return 2

    lock = acquire_single_instance()
    if lock is None:
        print("已经有一个 attention_please 在跑(单实例锁被占用)。先把它关掉。")
        return 3

    if cal.camera_index != cfg.general.camera_index:
        print(f"⚠️ 校准时的摄像头是 {cal.camera_index}, 配置里现在是 "
              f"{cfg.general.camera_index} —— 阈值可能不适用, 建议重新校准。")

    return Runtime(cfg, cal).run()


if __name__ == "__main__":
    raise SystemExit(main())
