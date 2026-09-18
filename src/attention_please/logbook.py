"""耐久文件日志。

为什么必须有它(不是"锦上添花"):
  托盘版用 `pythonw.exe` 启动, **stdout 是不存在的** —— 出事时现场什么都不剩。
  2026-09-17 晚上就吃过这个亏: 数据库显示该有 3 次 L3 提醒(20:58:58 / 21:02:28 /
  21:05:45)却一条都没有, 而循环明明在正常跑(每分钟 300 tick)。因为没有任何日志,
  只能靠"用真实时间戳回放状态机"反推, 至今无法确定那 3 次的异常被谁吞了。

设计取舍:
  - 按天一个文件, 放 data/logs/, 默认留 14 天;
  - **每条都 flush**: 崩溃/断电时最后几行也要留下来(这台机器有 0x124 硬件蓝屏前科);
  - 线程安全(托盘线程、监控线程都会写)。
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

RETENTION_DAYS = 14


class StreamTee:
    """把 sys.stdout / sys.stderr 接进文件日志。

    托盘版(pythonw.exe)**没有控制台**: 所有 print 都掉进虚空, 出事现场什么都不剩。
    启动时把 stdout/stderr 指到这里, 就能"一行不改"地留下全部输出
    (连 MediaPipe 自己的警告也一起留下), 有控制台时同时照常显示。
    """

    def __init__(self, logbook: "Logbook", level: str = "OUT", echo=None):
        self.log = logbook
        self.level = level
        self.echo = echo

    def write(self, text: str) -> int:
        """作为 sys.stdout 时必须**永不抛异常**: 它一抛, 整个进程的 print 都会炸。"""
        if not text:
            return 0
        if self.echo is not None:
            try:
                self.echo.write(text)
            except Exception:  # noqa: BLE001
                pass
        try:
            for line in text.rstrip("\n").split("\n"):
                if line.strip():
                    self.log.write(line, level=self.level)
        except Exception:  # noqa: BLE001
            pass
        return len(text)

    def flush(self) -> None:
        if self.echo is not None:
            try:
                self.echo.flush()
            except Exception:  # noqa: BLE001
                pass

    def isatty(self) -> bool:
        return False

    @property
    def encoding(self) -> str:
        return "utf-8"


def install_streams(logbook: "Logbook") -> bool:
    """把当前进程的 stdout/stderr 接到日志上。重复调用只生效一次。"""
    import sys

    if getattr(sys, "attention_logging_installed", False):
        return False
    real_out = sys.stdout
    real_err = sys.stderr
    sys.stdout = StreamTee(logbook, "OUT", echo=real_out)
    sys.stderr = StreamTee(logbook, "ERR", echo=real_err)
    sys.attention_logging_installed = True        # type: ignore[attr-defined]
    return True


class Logbook:
    def __init__(self, log_dir: Path, retention_days: int = RETENTION_DAYS):
        self.dir = Path(log_dir)
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._fh = None
        self._day = ""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.dir = Path(".")      # 极少数情况下退化成当前目录, 也不能因为日志把程序搞崩

    @property
    def path(self) -> Path:
        return self.dir / f"{datetime.now():%Y-%m-%d}.log"

    def _handle(self):
        day = datetime.now().strftime("%Y-%m-%d")
        if self._fh is None or day != self._day:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:  # noqa: BLE001
                    pass
            self._fh = open(self.path, "a", encoding="utf-8", buffering=1)
            self._day = day
            self._cleanup()
        return self._fh

    def _cleanup(self) -> None:
        try:
            cutoff = time.time() - self.retention_days * 86400
            for old in self.dir.glob("*.log"):
                if old.stat().st_mtime < cutoff:
                    old.unlink()
        except Exception:  # noqa: BLE001
            pass

    def write(self, message: str, level: str = "INFO") -> None:
        line = f"{datetime.now():%H:%M:%S} {level:5s} {message}"
        try:
            with self._lock:
                fh = self._handle()
                fh.write(line + "\n")
                fh.flush()
        except Exception:  # noqa: BLE001 - 日志失败绝不能影响监控
            pass

    def exception(self, where: str, exc: BaseException) -> None:
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.write(f"{where} 出错: {type(exc).__name__}: {exc}\n{detail}", level="ERROR")

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except Exception:  # noqa: BLE001
                    pass
                self._fh = None
