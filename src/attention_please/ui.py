"""提醒弹窗 + 暂停理由输入 + 日报查看器(全部 Tk 都跑在同一个独立线程里)。

为什么提醒一定要带证据:
  你的底线是"误报最烦"。一个不说理由的提醒会让人火大, 而"检测到你在看手机:
  头右偏 32° 持续 26 秒"能让你自己判断它是不是搞错了 —— 顺手还能点一下
  "判定错了", 该信号立刻静默 15 分钟。

线程模型: tkinter 不是线程安全的, 所以 **整个进程里只在这里创建一个 Tk 根窗口**,
它和 mainloop 都住在自己的线程里, 外面只通过两个队列通信(requests 进, events 出)。
托盘里的"暂停"也走这里弹输入框, 避免出现第二个 Tk 实例。

## 两个必须记住的 Tk 陷阱(都是实测踩出来的)

1. **`root.after()` 必须放在 `finally` 里**:轮询回调的最后一行才是"排下一次轮询",
   所以回调里任何一次异常都会让轮询链**永久断掉** —— mainloop 还活着, 但再也没人处理请求,
   表现就是"第一下能用, 之后点什么都没反应"。
2. **用户点右上角 X 关窗口时,Tk 直接把 widget 销毁**,我们记录的 `state["win"]` 会变成
   "指向已死 widget 的引用";下一轮拿它算倒计时就抛 `TclError`(触发陷阱 1)。
   所以必须用 `<Destroy>` 事件把状态复位, 而不是只在按钮回调里复位。
"""
from __future__ import annotations

import pathlib
import queue
import threading
from dataclasses import dataclass

from . import mdview


@dataclass
class AlertRequest:
    title: str
    body: str
    signal: str
    level: int
    auto_close_seconds: float = 30.0
    sound_hint: str = ""


@dataclass
class AskRequest:
    title: str
    prompt: str


@dataclass
class ReportRequest:
    title: str
    text: str
    path: str = ""          # 磁盘上的 md 文件; 窗口里的"用 VS Code 打开"会用到
    plain: bool = False     # True = 退回纯文本; 默认渲染成带样式的预览


@dataclass
class _DestroyCurrent:
    """仅供测试: 绕过 close_window() 直接销毁窗口, 用来模拟"点右上角 X"。"""


def strip_markdown(text: str) -> str:
    """把 markdown 里影响阅读的记号去掉(表格和缩进保留)。"""
    out = []
    for line in text.splitlines():
        s = line.replace("**", "").replace("`", "")
        if s.startswith("#"):
            s = s.lstrip("#").strip()
        out.append(s)
    return "\n".join(out)


class AlertUI:
    """弹窗桥。start() 之后 show()/ask_reason()/show_report() 都是非阻塞的。"""

    def __init__(self) -> None:
        self._requests: queue.Queue[object] = queue.Queue()
        self._events: queue.Queue[tuple[str, str]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self.windows_shown = 0        # 诊断用: UI 到底还活着吗
        self.last_ui_error = ""

    # ---- 外部接口 ----
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="alert-ui", daemon=True)
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def show(self, req: AlertRequest) -> None:
        self.start()
        self._requests.put(req)

    def ask_reason(self, title: str, prompt: str) -> None:
        """弹一个必填理由的输入框(用于"暂停")。"""
        self.start()
        self._requests.put(AskRequest(title=title, prompt=prompt))

    def show_report(self, title: str, text: str, path: str = "") -> None:
        """用自己的窗口显示日报。

        为什么不用外部编辑器: 这台机器 `.md` 没有文件关联, 而且从后台进程启动的
        GUI 抢不到前台 —— 实测 VS Code 确实打开了文件, 但窗口在别的窗口后面,
        用户看到的就是"点了没反应"。自己的窗口一定能显示。
        """
        self.start()
        self._requests.put(ReportRequest(title=title, text=text, path=path))

    def destroy_current_window_for_test(self) -> None:
        """仅供测试: 模拟用户点右上角 X(绕过 close_window 的状态复位)。"""
        self.start()
        self._requests.put(_DestroyCurrent())

    def drain(self) -> list[tuple[str, str]]:
        """取出用户动作:
        ("wrong"|"dismissed"|"reason"|"reason_cancelled", 值) 与 ("ui_error", 描述)。
        """
        out: list[tuple[str, str]] = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    # 供测试与内部使用
    def _emit(self, kind: str, value: str = "") -> None:
        self._events.put((kind, value))

    # ---- UI 线程 ----
    def _run(self) -> None:
        import tkinter as tk
        import traceback

        # Tk 起不来时(没有可用的桌面会话 / Tcl 坏了 / 开机自启跑得太早)**必须说出来**。
        # 这一行以前在任何 try 之外: 线程直接死掉, last_ui_error 是空的、一个事件都不发,
        # 之后每次 show() 都只是再起一个秒死的线程, 请求永远排在队列里 ——
        # 表现就是"点了没反应", 而且哪儿都查不到原因。
        try:
            root = tk.Tk()
        except Exception as exc:  # noqa: BLE001
            self.last_ui_error = f"tk-init: {type(exc).__name__}: {exc}"
            self._emit("ui_error", self.last_ui_error)
            try:
                print(f"[ui] Tk 起不来, 提醒窗口不可用: {type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001
                pass
            return
        root.withdraw()
        root.title("attention_please")

        state: dict[str, object] = {"win": None, "deadline": 0.0, "label": None,
                                    "signal": "", "ask": None, "autoclose": True}

        def note_error(where: str, exc: BaseException) -> None:
            self.last_ui_error = f"{where}: {type(exc).__name__}: {exc}"
            self._emit("ui_error", self.last_ui_error)
            try:
                print(f"[ui] {where} 出错: {type(exc).__name__}: {exc}")
                print("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            except Exception:  # noqa: BLE001
                pass

        # Tk 回调里的异常默认只打到 stderr(托盘版没有 stderr) —— 接管它
        def on_tk_error(exc_type, exc_value, exc_tb) -> None:
            note_error("tk-callback", exc_value)

        root.report_callback_exception = on_tk_error

        def reset_state() -> None:
            state["win"] = None
            state["label"] = None
            state["ask"] = None
            state["signal"] = ""
            state["autoclose"] = True
            state["deadline"] = 0.0

        def on_destroy(event) -> None:
            """窗口以任何方式被销毁(按钮/X/外部)都要复位 —— 见模块开头的陷阱 2。"""
            if event.widget is not state.get("win"):
                return                      # <Destroy> 会冒泡, 忽略子控件
            was_asking = state.get("ask") is not None
            reset_state()
            if was_asking:
                # 点 X 关掉「暂停理由」输入框 = 取消暂停。以前这里什么都不发, 于是
                # runtime 一直以为你没暂停, 库和日志里也一个字都没有 —— 用户看着框没了,
                # 以为暂停生效了, 起身走人, 然后被"离开太久"提醒。
                self._emit("reason_cancelled")

        def track(win) -> None:
            win.bind("<Destroy>", on_destroy, add="+")
            state["win"] = win
            self.windows_shown += 1

        def close_window(notify_cancel: bool = False) -> None:
            """关掉当前窗口。`notify_cancel=True` 时, 如果关的是"暂停理由"输入框,
            要发一条 `reason_cancelled` —— 别的窗口(提醒/日报)顶掉它时就是这种情况。"""
            win = state.get("win")
            was_asking = state.get("ask") is not None
            reset_state()                   # 先复位, 再销毁(销毁会触发 <Destroy>, 幂等)
            if win is not None:
                try:
                    win.destroy()  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
            if was_asking and notify_cancel:
                self._emit("reason_cancelled")

        def on_dismiss() -> None:
            if state.get("signal"):
                self._emit("dismissed", str(state["signal"]))
            close_window()

        def on_wrong() -> None:
            if state.get("signal"):
                self._emit("wrong", str(state["signal"]))
            close_window()

        def place(win, corner: bool = True) -> None:
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
            w = max(win.winfo_width(), 360)
            h = win.winfo_height()
            x = max(0, sw - w - 40) if corner else max(0, (sw - w - 40) // 2)
            y = max(0, sh - h - 80) if corner else max(0, (sh - h) // 3)
            win.geometry(f"+{x}+{y}")

        def place_center(win, min_w: int = 1000, min_h: int = 680) -> tuple[int, int]:
            """把窗口放到屏幕中上位置, 返回最终尺寸(渲染表格要用它算可用宽度)。

            比原来(720x560)宽一截: 日报里那张八列的表在 720 宽下只能缩到 7pt 才塞得下。
            但**不许比屏幕还宽** —— 小屏幕上开一个超出屏幕的窗口, 右边的列就永远看不到了。
            """
            sw = win.winfo_screenwidth()
            sh = win.winfo_screenheight()
            w = max(560, min(max(win.winfo_width(), min_w), sw - 80))
            h = max(480, min(max(win.winfo_height(), min_h), sh - 120))
            win.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
            return w, h

        def now_ms(win) -> float:
            return float(win.tk.call("clock", "milliseconds"))

        # ---- 三类窗口 ----
        def show_alert(req: AlertRequest) -> None:
            close_window(notify_cancel=True)
            win = tk.Toplevel(root)
            win.attributes("-topmost", True)
            win.title(f"attention_please — 第 {req.level} 级提醒")
            win.configure(bg="#1b1b1b")
            frame = tk.Frame(win, bg="#1b1b1b", padx=18, pady=14)
            frame.pack(fill="both", expand=True)
            tk.Label(frame, text=req.title, fg="#ffd166", bg="#1b1b1b",
                     font=("Microsoft YaHei UI", 13, "bold"),
                     anchor="w", justify="left").pack(fill="x")
            tk.Label(frame, text=req.body, fg="#f2f2f2", bg="#1b1b1b",
                     font=("Microsoft YaHei UI", 11), wraplength=460,
                     anchor="w", justify="left").pack(fill="x", pady=(8, 4))
            if req.sound_hint:
                tk.Label(frame, text=req.sound_hint, fg="#9aa0a6", bg="#1b1b1b",
                         font=("Microsoft YaHei UI", 9), anchor="w").pack(fill="x")
            countdown = tk.Label(frame, text="", fg="#9aa0a6", bg="#1b1b1b",
                                 font=("Microsoft YaHei UI", 9), anchor="w")
            countdown.pack(fill="x", pady=(6, 8))
            buttons = tk.Frame(frame, bg="#1b1b1b")
            buttons.pack(fill="x")
            tk.Button(buttons, text="我回来了", command=on_dismiss, width=12,
                      font=("Microsoft YaHei UI", 10)).pack(side="left")
            tk.Button(buttons, text="判定错了(静默该信号)", command=on_wrong,
                      font=("Microsoft YaHei UI", 10)).pack(side="left", padx=(10, 0))
            win.update_idletasks()
            place(win)
            track(win)
            state["signal"] = req.signal
            state["label"] = countdown
            # auto_close_seconds <= 0 表示"不自动关闭"(离开座位的提醒要等你回来看到)
            if req.auto_close_seconds and req.auto_close_seconds > 0:
                state["autoclose"] = True
                state["deadline"] = now_ms(win) + req.auto_close_seconds * 1000
            else:
                state["autoclose"] = False
                state["deadline"] = 0.0
                countdown.configure(text="(不会自动关闭)")

        def show_ask(req: AskRequest) -> None:
            close_window(notify_cancel=True)
            win = tk.Toplevel(root)
            win.attributes("-topmost", True)
            win.title(req.title)
            win.configure(bg="#1b1b1b")
            frame = tk.Frame(win, bg="#1b1b1b", padx=18, pady=14)
            frame.pack(fill="both", expand=True)
            tk.Label(frame, text=req.prompt, fg="#f2f2f2", bg="#1b1b1b",
                     font=("Microsoft YaHei UI", 11), wraplength=420,
                     anchor="w", justify="left").pack(fill="x")
            entry = tk.Entry(frame, width=46, font=("Microsoft YaHei UI", 11))
            entry.pack(fill="x", pady=(10, 10))
            entry.focus_force()

            def submit(_event=None) -> None:
                text = entry.get().strip()
                if not text:
                    return          # 理由必填 —— 这就是"暂停的代价"
                self._emit("reason", text)
                close_window()

            def cancel(_event=None) -> None:
                self._emit("reason_cancelled")
                close_window()

            buttons = tk.Frame(frame, bg="#1b1b1b")
            buttons.pack(fill="x")
            tk.Button(buttons, text="确定暂停", command=submit, width=12,
                      font=("Microsoft YaHei UI", 10)).pack(side="left")
            tk.Button(buttons, text="算了", command=cancel, width=8,
                      font=("Microsoft YaHei UI", 10)).pack(side="left", padx=(10, 0))
            entry.bind("<Return>", submit)
            win.bind("<Escape>", cancel)
            win.update_idletasks()
            place(win)
            track(win)
            state["ask"] = entry
            state["autoclose"] = False

        def show_report_window(req: ReportRequest) -> None:
            close_window(notify_cancel=True)
            win = tk.Toplevel(root)
            win.attributes("-topmost", True)
            win.title(req.title)
            win.configure(bg="#1b1b1b")
            frame = tk.Frame(win, bg="#1b1b1b", padx=12, pady=10)
            frame.pack(fill="both", expand=True)
            body = tk.Text(frame, wrap="word", width=80, height=28, bg="#111111",
                           fg="#eaeaea", insertbackground="#eaeaea", relief="flat",
                           font=("Microsoft YaHei UI", 10), padx=12, pady=10)
            scroll = tk.Scrollbar(frame, command=body.yview)
            body.configure(yscrollcommand=scroll.set)
            scroll.pack(side="right", fill="y")
            body.pack(side="left", fill="both", expand=True)

            # ⚠️ 注意: tk.Frame 的 padx/pady 是**控件选项, 只接受整数**;
            # (0, 10) 这种元组只有 pack()/grid() 认 —— 写在构造函数里会抛
            # `TclError: bad screen distance "0 10"`, 而且是在窗口已经画出来之后才抛,
            # 于是表现为"第一次能用, 之后整个 UI 都死了"(2026-09-17 的真实事故)。
            bar = tk.Frame(win, bg="#1b1b1b", padx=12)
            bar.pack(fill="x", pady=(0, 10))
            hint = tk.Label(bar, text=req.path or "", fg="#9aa0a6", bg="#1b1b1b",
                            font=("Microsoft YaHei UI", 8), anchor="w")
            hint.pack(side="left")

            def open_external() -> None:
                from .opener import open_path

                try:
                    ok, how = open_path(pathlib.Path(req.path))
                    hint.configure(
                        text=(f"已用「{how}」打开 —— 但它可能抢不到前台, 去任务栏找窗口"
                              if ok else f"打开失败: {how}"))
                except Exception as exc:  # noqa: BLE001
                    note_error("open_external", exc)

            mode = {"plain": req.plain}

            def render_now(avail_px: int) -> None:
                body.configure(state="normal")
                body.delete("1.0", "end")
                if mode["plain"]:
                    body.insert("1.0", strip_markdown(req.text))
                else:
                    try:
                        mdview.render(body, req.text, avail_px=avail_px)
                    except Exception as exc:  # noqa: BLE001
                        # 渲染炸了**绝不能留一个空窗口**: 退回纯文本, 并把这次失败记成
                        # ui_error(日报的"数据可信度"那一段会显示出来)。
                        note_error("render_report", exc)
                        body.delete("1.0", "end")
                        body.insert("1.0", strip_markdown(req.text))
                body.configure(state="disabled")
                body.yview_moveto(0.0)

            def toggle_plain() -> None:
                mode["plain"] = not mode["plain"]
                toggle.configure(text="看预览" if mode["plain"] else "看纯文本")
                render_now(avail)

            toggle = tk.Button(bar, text="看预览" if req.plain else "看纯文本",
                               command=toggle_plain, width=10,
                               font=("Microsoft YaHei UI", 10))
            tk.Button(bar, text="关闭", command=close_window, width=8,
                      font=("Microsoft YaHei UI", 10)).pack(side="right")
            if req.path:
                tk.Button(bar, text="用 VS Code 打开", command=open_external,
                          font=("Microsoft YaHei UI", 10)).pack(side="right", padx=(0, 10))
            toggle.pack(side="right", padx=(0, 10))

            win.update_idletasks()
            w, _h = place_center(win)
            # 正文可用像素宽 = 窗口 - 外层 padx(12*2) - 滚动条 - Text 自己的 padx(12*2)
            avail = max(320, w - 2 * 12 - 18 - 2 * 12 - 6)
            render_now(avail)
            track(win)
            state["autoclose"] = False

        # ---- 轮询 ----
        def handle(req) -> None:
            if isinstance(req, AlertRequest):
                show_alert(req)
            elif isinstance(req, AskRequest):
                show_ask(req)
            elif isinstance(req, ReportRequest):
                show_report_window(req)
            elif isinstance(req, _DestroyCurrent):
                win = state.get("win")
                if win is not None:
                    win.destroy()  # type: ignore[union-attr]  # 故意绕过状态复位

        def tick() -> None:
            try:
                try:
                    req = self._requests.get_nowait()
                except queue.Empty:
                    req = None
                if req is not None:
                    handle(req)

                win = state.get("win")
                if win is not None and state.get("autoclose"):
                    left = (float(state["deadline"]) - now_ms(win)) / 1000.0
                    label = state.get("label")
                    if label is not None:
                        label.configure(text=(f"{max(0.0, left):.0f} 秒后自动关闭"  # type: ignore[attr-defined]
                                              if left > 0 else ""))
                    if left <= 0:
                        on_dismiss()
            except Exception as exc:  # noqa: BLE001
                note_error("tick", exc)
            finally:
                # ★ 必须放在 finally: 一次异常不能让轮询链永久断掉(实测踩到过)
                try:
                    root.after(200, tick)
                except Exception:  # noqa: BLE001
                    pass

        root.after(200, tick)
        try:
            root.mainloop()
        except Exception as exc:  # noqa: BLE001 - UI 挂了也不该拖垮监控
            note_error("mainloop", exc)


if __name__ == "__main__":
    import time

    ui = AlertUI()
    ui.show(AlertRequest(title="检测到你在看手机", signal="phone", level=2,
                         body="头向右偏 32°, 持续 26 秒(阈值 19°)",
                         sound_hint="安静时段: 不发声, 仅弹窗"))
    time.sleep(4)
    ui.ask_reason("暂停监控", "为什么要暂停?(会记进日报, 别写'不想学')")
    for _ in range(60):
        for ev in ui.drain():
            print("事件:", ev)
        time.sleep(0.5)
