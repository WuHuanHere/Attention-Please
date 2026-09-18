"""每日日报: 把事件流和覆盖率算成一份给人看的 markdown。

北极星指标是"有效专注时长", 但单独一个数字会骗人, 所以日报固定四行
(计划 / 有效专注 / 分心 / 暂停+离开), 再加一段**数据可信度**:
人脸覆盖率低的时候, "看手机"其实是在瞎猜, 那段时间记为"看不清",
既不算专注也不算分心 —— 不能拿"看不清"冒充"你很专注"。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .config import Config
from .store import Store


def _hm(seconds: float) -> str:
    m = int(round(seconds / 60.0))
    if m < 60:
        return f"{m} 分钟"
    return f"{m // 60} 小时 {m % 60:02d} 分"


def _bar(fraction: float, width: int = 20) -> str:
    filled = max(0, min(width, int(round(fraction * width))))
    return "█" * filled + "·" * (width - filled)


def uncovered_plan_note(cfg: Config, st, planned_seconds: float,
                        day: str | None = None) -> list[str]:
    """指出"计划里该学、但完全没有监控数据"的时间。

    这是"漏报必须可见"的延伸: 程序没跑/机器没开机的那段时间, 报表上什么都不显示,
    会被误读成"你没专注"。所以宁可写一行警告。
    """
    notes: list[str] = []
    if not st.first_monitored:
        return ["今天没有任何监控数据(程序没运行过?)"]
    blocks = [b for b in cfg.schedule.blocks if b.is_focus]
    if not blocks:
        return notes
    first_block = min(blocks, key=lambda b: (b.start.hour, b.start.minute))
    start_txt = f"{first_block.start.hour:02d}:{first_block.start.minute:02d}"
    if st.first_monitored > start_txt:
        notes.append(f"计划里的「{first_block.name}」从 {start_txt} 开始, "
                     f"但监控是 {st.first_monitored} 才起的 —— 那段时间没有数据"
                     f"(机器没开机 / 程序没跑), 不要当成没专注。")
    # "该监控却缺失"的分母必须是**已过去**的计划时段: 中午生成日报时,
    # 下午和晚上还没到, 用全天计划会算出"有 7 小时没监控"这种没意义的数字。
    today = day or datetime.now().date().isoformat()
    elapsed_plan = (cfg.schedule.elapsed_planned_seconds(datetime.now())
                    if today == datetime.now().date().isoformat() else planned_seconds)
    gap = elapsed_plan - st.monitored_seconds
    if gap > 600:
        notes.append(f"计划里已经过去的 {_hm(elapsed_plan)} 中, 有约 {_hm(gap)} 没有监控数据"
                     f"(见上面的未监控警告与暂停记录)。")
    return notes


def build_report(cfg: Config, store: Store, day: str,
                 planned_seconds: float | None = None) -> str:
    if planned_seconds is None:
        planned_seconds = cfg.schedule.planned_seconds()
    st = store.day_stats(day, planned_seconds=planned_seconds,
                         tick_hz=cfg.general.tick_hz)
    # 比值分母用**实际监控时长**, 不是计划时长:
    # 用计划时长当分母会把"机器没开机/程序没跑/暂停"算成"你没专注"
    # (2026-09-18 实测: 09:09 才启动, 报表显示 19%, 看起来像摸了一天鱼)。
    denom = st.monitored_seconds or planned_seconds
    ratio = (st.focus_seconds / denom) if denom else 0.0
    lines: list[str] = []
    lines.append(f"# {day} 专注日报")
    lines.append("")
    lines.append(f"> 有效专注 **{_hm(st.focus_seconds)}** / 实际监控 {_hm(st.monitored_seconds)}   "
                 f"`{_bar(ratio)}` {ratio * 100:.0f}%")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---|")
    lines.append(f"| **有效专注** | **{_hm(st.focus_seconds)}**(占监控时间 {ratio * 100:.0f}%) |")
    lines.append(f"| 实际监控时长 | {_hm(st.monitored_seconds)}"
                 + (f"({st.first_monitored}–{st.last_monitored})" if st.first_monitored else "")
                 + " |")
    lines.append(f"| 计划学习时长 | {_hm(planned_seconds)}(作息表) |")
    lines.append(f"| 分心 | {_hm(st.distract_seconds)}({st.episodes} 次) |")
    lines.append(f"| 暂停 | {_hm(st.pause_seconds)} |")
    away_txt = _hm(st.away_seconds) + (f"(提醒过 {st.away_nudges} 次)" if st.away_nudges else "")
    lines.append(f"| 离开座位 | {away_txt} |")
    lines.append(f"| 看不清(脸不在画面) | {_hm(st.blind_seconds)} |")
    lines.append(f"| 提醒次数 | {st.nudges} |")
    lines.append(f"| 判定了「判定错了」 | {st.wrong_feedback} |")
    lines.append("")

    # 计划里本该被监控、但完全没有数据的时间 —— 必须写出来, 否则会被误读成"你在摸鱼"
    missing = uncovered_plan_note(cfg, st, planned_seconds, day)
    if missing:
        for note in missing:
            lines.append(f"> ⚠️ {note}")
        lines.append("")

    hours = store.busiest_distraction_hours(day)
    lines.append("## 最常分心的时段")
    if hours:
        for hour, secs in hours:
            lines.append(f"- {hour:02d}:00–{hour + 1:02d}:00  {_hm(secs)}")
    else:
        lines.append("- (今天没有被判定的分心)")
    lines.append("")

    titles = store.top_titles_before(day)
    lines.append("## 分心前在看什么(窗口标题)")
    if titles:
        for title, count in titles:
            clean = (title or "").strip() or "(标题为空)"
            lines.append(f"- {clean[:70]}  × {count}")
    else:
        lines.append("- (无)")
    lines.append("")

    reasons = store.pause_reasons(day)
    lines.append("## 暂停理由")
    if reasons:
        for reason, secs in reasons:
            lines.append(f"- {_hm(secs)}:{reason}")
    else:
        lines.append("- (今天没有暂停)")
    lines.append("")

    lines.append("## 数据可信度")
    lines.append(f"- 人脸覆盖率 **{st.coverage * 100:.0f}%**"
                 f"(低于 90% 时「看手机」信号不可靠)")
    if st.camera_busy_seconds:
        lines.append(f"- ⚠️ 因摄像头被占用漏检 {_hm(st.camera_busy_seconds)}")
    if st.blind_seconds > 60:
        lines.append(f"- ⚠️ 有 {_hm(st.blind_seconds)} 人脸不在画面里, "
                     f"这段时间只做了窗口检测")
        # 「看不清」是个很粗的桶: 低头写题(正当)和真的人不在(可疑)全落在里面,
        # 而低头写题恰恰是人脸覆盖率最低的时候(实测 12-15%)。Pose 弱证据就是用来
        # 把这个桶拆开的 —— 它只记录不报警, 所以这里能写, 但不能反过来当专注算。
        if st.pose_weak_seconds > 30:
            lines.append(f"  - 其中 **{_hm(st.pose_weak_seconds)}** Pose 显示在座且"
                         f"低头/转头(疑似纸笔模式或侧身, **只记录未报警**)")
        # 只在"确实还剩下一大段谁也看不到的时间"时才写这一行 ——
        # 否则会印出"剩下约 0 分钟"这种纯噪声。
        pose_gap = max(0.0, st.blind_seconds - st.pose_weak_seconds)
        if pose_gap > 30:
            lines.append(f"  - 剩下约 {_hm(pose_gap)} 里 Pose 也没能给出姿态"
                         f"(可能真的不在画面里, 或 Pose 也检不到) —— "
                         f"这段时间**什么都没看到**, 既不算专注也不算分心")
    if st.wrong_feedback:
        lines.append(f"- 你标记了 {st.wrong_feedback} 次误判 —— 攒够样本后按日志调阈值"
                     f"(程序不自动调参)")
    lines.append("")
    lines.append(f"*生成时间 {datetime.now():%Y-%m-%d %H:%M:%S}*")
    lines.append("")
    return "\n".join(lines)


def write_report(cfg: Config, store: Store, day: str | None = None) -> Path:
    day = day or datetime.now().date().isoformat()
    text = build_report(cfg, store, day)
    cfg.report_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.report_dir / f"{day}.md"
    path.write_text(text, encoding="utf-8")
    return path
