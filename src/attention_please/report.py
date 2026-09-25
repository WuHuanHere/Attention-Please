"""每日日报: 把事件流和覆盖率算成一份给人看的 markdown。

北极星指标是"有效专注时长", 但单独一个数字会骗人, 所以日报固定四行
(计划 / 有效专注 / 分心 / 暂停+离开), 再加一段**数据可信度**:
人脸覆盖率低的时候, "看手机"其实是在瞎猜, 那段时间记为"看不清",
既不算专注也不算分心 —— 不能拿"看不清"冒充"你很专注"。
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from .config import Config
from .store import OUTSIDE_NAME, Store


def _hm(seconds: float) -> str:
    m = int(round(seconds / 60.0))
    if m < 60:
        return f"{m} 分钟"
    return f"{m // 60} 小时 {m % 60:02d} 分"


def _clock(ts: float) -> str:
    return f"{datetime.fromtimestamp(ts):%H:%M}"


def _hm_short(seconds: float) -> str:
    """分时段表**单元格**里的时长: 不足一分钟的用秒。

    没有它的话, "某段有 2 次分心、共 20 秒"会显示成 `0 分钟(2 次)` —— 看着像记账出错。
    门槛是"**四舍五入后**仍有 1 秒以上": 0.4 秒的余数(减法兜底后的零头)写 `0 秒`
    比写 `0 分钟`更怪。
    """
    if 0 < round(seconds) < 60:
        return f"{int(round(seconds))} 秒"
    return _hm(seconds)


def _visible(b) -> bool:
    """这一桶在分钟精度下看得见吗 —— 看不见就别印, 免得每天多一行全 0 的噪声。

    真实数据里必然有一点点"表外"秒数: 一次离开跨过某个时段的结尾, 尾巴就落在空档里。
    20 秒的尾巴不值得占一行, 但**它仍然算在"各时段之和"里**, 所以下面的差额校验
    不会因为不显示而失效(隐藏的是显示, 不是账)。
    """
    return b.episodes > 0 or any(int(round(v / 60.0)) for v in (
        b.monitored_seconds, b.distract_seconds, b.blind_seconds,
        b.away_seconds, b.allowed_phone_seconds, b.wrong_seconds))



def _bar(fraction: float, width: int = 20) -> str:
    filled = max(0, min(width, int(round(fraction * width))))
    return "█" * filled + "·" * (width - filled)


def uncovered_plan_note(cfg: Config, st, planned_seconds: float,
                        day: str | None = None,
                        now: datetime | None = None) -> list[str]:
    """指出"计划里该学、但完全没有监控数据"的时间。

    这是"漏报必须可见"的延伸: 程序没跑/机器没开机的那段时间, 报表上什么都不显示,
    会被误读成"你没专注"。所以宁可写一行警告。
    """
    notes: list[str] = []
    now = now or datetime.now()
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
    today = day or now.date().isoformat()
    elapsed_plan = (cfg.schedule.elapsed_planned_seconds(now)
                    if today == now.date().isoformat() else planned_seconds)
    gap = elapsed_plan - st.monitored_seconds
    if gap > 600:
        notes.append(f"计划里已经过去的 {_hm(elapsed_plan)} 中, 有约 {_hm(gap)} 没有监控数据"
                     f"(见上面的未监控警告与暂停记录)。")
    return notes


def block_breakdown(cfg: Config, store: Store, st, day: str,
                    now: datetime | None = None) -> list[str]:
    """按**你自己的作息表**分时段统计专注时长(日报的一张表)。

    为什么值得单开一张表: 全天只有一个"有效专注 X 小时"时, "上午两小时全神贯注、
    下午三小时全废"会被平均成一个谁都不得罪的数字, 看不出该改哪一段。分时段之后,
    计划 / 监控 / 有效专注 / 分心 / 看不清 / 离开 全都落到具体那一段上。

    三条口径上的讲究(每条都对应一个踩过的坑):
      * 分母仍然是**实际监控时长**(和标题一致)。用计划时长当分母会把"程序没跑"
        算成"你没专注" —— 那正是 2026-09-18 修掉的那个误导。
      * **还没到的时段写「未到」, 不写 0 分钟 0%**。中午打开日报时, 下午那个 0% 是假的。
      * 已经过去、却一分钟都没监控到的时段写「无监控数据」而不是 0% —— 同理,
        没有数据不等于没专注。具体缺在哪一段, 正好就是这张表的价值。
    """
    now = now or datetime.now()
    now_ts = now.timestamp()
    windows = cfg.schedule.block_windows(date.fromisoformat(day))
    lines = ["## 各时段专注情况(按作息表)", ""]
    if not windows:
        lines.append("- (作息表里没有 focus 时段, 没法分时段统计)")
        lines.append("")
        return lines
    rows = store.block_stats(day, windows, tick_hz=cfg.general.tick_hz)
    # 差额校验用**全部**桶; 只有"时间表外"那一桶可以因为看不见而被藏起来(见 _visible)。
    # 作息表里的时段**一个都不许省** —— "这一段一分钟都没监控到"正是这张表的价值。
    summed = sum(b.focus_seconds for b in rows)
    shown = [b for b in rows if not b.outside or _visible(b)]

    lines.append("| 时段 | 计划 | 实际监控 | 有效专注 | 专注率 | 分心 | 看不清 | 离开 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for b in shown:
        if b.outside:
            name, planned = OUTSIDE_NAME, "—"
            started = True                # 表外本来就不在作息里, 无所谓"未到"
        else:
            name = f"{_clock(b.start_ts)}–{_clock(b.end_ts)} {b.name}"
            planned = _hm(b.planned_seconds)
            started = now_ts >= b.start_ts
            if not started:
                name += "(未到)"
            elif b.start_ts <= now_ts < b.end_ts:
                name += "(进行中)"
        if not started:
            lines.append(f"| {name} | {planned} | — | — | — | — | — | — |")
            continue
        if b.monitored_seconds > 0:
            focus_cell = _hm_short(b.focus_seconds)
            rate_cell = f"{b.focus_seconds / b.monitored_seconds * 100:.0f}%"
        elif b.distract_seconds or b.blind_seconds or b.away_seconds:
            # 有账但没有监控数据(典型: 离开的尾巴落在作息表外) —— 写「—」而不是
            # 「无监控数据」, 因为它并不是"什么都没记到"。
            focus_cell, rate_cell = _hm_short(b.focus_seconds), "—"
        else:
            # 已经过去却一分钟都没监控到: **不能写 0%**, 没有数据不等于没专注。
            focus_cell, rate_cell = "—", "无监控数据"
        dist = _hm_short(b.distract_seconds) + (f"({b.episodes} 次)" if b.episodes else "")
        lines.append("| " + " | ".join([
            name, planned, _hm_short(b.monitored_seconds), focus_cell, rate_cell, dist,
            _hm_short(b.blind_seconds), _hm_short(b.away_seconds)]) + " |")
    # 合计用**全天总数**(不是上表各行相加): 它是标题上那个数字, 两者必须对得上。
    denom = st.monitored_seconds or st.planned_seconds
    ratio = (st.focus_seconds / denom) if denom else 0.0
    total_dist = _hm(st.distract_seconds) + (f"({st.episodes} 次)" if st.episodes else "")
    # 一分钟都没监控到时写「无监控数据」而不是 0% —— 和上面各行的口径保持一致,
    # 否则同一张表里"某段 0%"和"合计 0%"会一起把"没数据"说成"没专注"。
    if st.monitored_seconds <= 0:
        total_focus, total_rate = "—", "无监控数据"
    else:
        total_focus, total_rate = f"**{_hm(st.focus_seconds)}**", f"{ratio * 100:.0f}%"
    lines.append(f"| **合计** | {_hm(st.planned_seconds)} | {_hm(st.monitored_seconds)} | "
                 f"{total_focus} | {total_rate} | {total_dist} | "
                 f"{_hm(st.blind_seconds)} | {_hm(st.away_seconds)} |")
    lines.append("")

    notes: list[str] = []
    for b in shown:
        label = OUTSIDE_NAME if b.outside else f"{_clock(b.start_ts)}–{_clock(b.end_ts)} {b.name}"
        # 备注用**分钟**当门槛: 20 秒的"允许用手机"写进正文只是噪声, 表里已经能看到。
        if b.allowed_phone_seconds >= 30:
            notes.append(f"- {label} 里有 {_hm_short(b.allowed_phone_seconds)} 是「允许用手机」"
                         f"时段(背单词等), 看手机只记录、不算分心")
        if b.wrong_seconds >= 30:
            notes.append(f"- {label} 里有 {_hm_short(b.wrong_seconds)} 被你判为误报, 已从分心里剔除")
    if st.unfinished_episodes:
        notes.append(f"- 有 {st.unfinished_episodes} 段分心**没等到收尾**, 连时长都不知道, "
                     f"所以归不到任何时段 —— 它不在上表里(只会少算, 不会多算)")
    # 正常情况两者**精确相等**(store.block_stats 和 day_stats 用同一批数据、同一套口径),
    # 所以门槛取"四舍五入到分钟不为 0" —— 比这张表自己的精度还小的差额(几十秒)不值得
    # 报一次警, 但**只要够一分钟**就一定是真的记账不一致, 不是舍入。
    if int(round(abs(summed - st.focus_seconds) / 60.0)) >= 1:
        # 真出现差额只有一种可能: 某一段的离开/分心时长超过了它自己的监控时长
        # (例如离开的尾巴跨过了时段结尾, 或暂停期间离开计时还在走),
        # 逐段按 0 兜底而全天只兜一次。
        notes.append(f"- ⚠️ 上表各时段加起来({_hm(summed)})和合计({_hm(st.focus_seconds)})"
                     f"差 {_hm(abs(summed - st.focus_seconds))}: 有某一段的离开/分心时长"
                     f"超过了那一段的监控时长, 已按 0 兜底。**以合计为准**。")
    for note in notes:
        lines.append(note)
    if notes:
        lines.append("")
    return lines


def build_report(cfg: Config, store: Store, day: str,
                 planned_seconds: float | None = None,
                 now: datetime | None = None) -> str:
    now = now or datetime.now()
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
    distract_txt = f"{_hm(st.distract_seconds)}({st.episodes} 次)"
    if st.unfinished_episodes:
        # 不写出来的话, 这一行会和下面"提醒次数"自相矛盾(提醒响过, 分心却是 0 次)
        distract_txt += f" + **{st.unfinished_episodes} 段没等到收尾**"
    lines.append(f"| 分心 | {distract_txt} |")
    if st.allowed_phone_seconds:
        # 背单词时段用手机 App 是**策略允许**的, 不算分心 —— 但也不能假装没发生
        lines.append(f"| 其中「允许用手机」时段 | {_hm(st.allowed_phone_seconds)}"
                     f"(只记录, 不算分心) |")
    if st.wrong_seconds:
        lines.append(f"| 你判为误报、已剔除 | {_hm(st.wrong_seconds)}"
                     f"(不计入分心, 也不扣专注) |")
    lines.append(f"| 暂停 | {_hm(st.pause_seconds)} |")
    away_txt = _hm(st.away_seconds) + (f"(提醒过 {st.away_nudges} 次)" if st.away_nudges else "")
    if st.unfinished_away:
        # 没有 away_end 的那段离开时长是未知的, 不编数字, 但**必须写出来** ——
        # 否则这一行会写"离开 0 分钟", 而人明明走了(实测 2026-09-17)。
        away_txt += f" + **{st.unfinished_away} 段没等到收尾**"
    lines.append(f"| 离开座位 | {away_txt} |")
    lines.append(f"| 看不清(脸不在画面) | {_hm(st.blind_seconds)} |")
    lines.append(f"| 提醒次数 | {st.nudges} |")
    lines.append(f"| 判定了「判定错了」 | {st.wrong_feedback} |")
    lines.append("")

    # 计划里本该被监控、但完全没有数据的时间 —— 必须写出来, 否则会被误读成"你在摸鱼"
    missing = uncovered_plan_note(cfg, st, planned_seconds, day, now)
    if missing:
        for note in missing:
            lines.append(f"> ⚠️ {note}")
        lines.append("")

    if st.unfinished_episodes:
        # "漏报必须可见": 这段分心**确实发生过**(提醒都响过了), 只是我们不知道它多久。
        # 编一个时长出来就是造假, 假装它不存在就是漏报 —— 所以单列, 并说清方向。
        lines.append(f"> ⚠️ 有 {st.unfinished_episodes} 段分心**没有收尾**: 程序在分心过程中"
                     f"被关掉/崩溃/蓝屏, 不知道它持续了多久。它**没有**计入上面的分心时长, "
                     f"所以那个数字只会少算、不会多算(也就是有效专注会略微偏高)。")
        lines.append("")

    if st.unfinished_away:
        lines.append(f"> ⚠️ 有 {st.unfinished_away} 段「离开座位」**没有收尾**, 那段时间的"
                     f"离开时长是未知的, **没有**计入上面的离开时长。")
        lines.append("")

    lines.extend(block_breakdown(cfg, store, st, day, now))

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
    if st.yield_seconds > 30:
        # "漏报必须可见": 让出期间**完全没有监控数据**(而它可能是你的学习时段),
        # 必须写清楚是主动让给别的程序了 —— 否则看报告的人会以为"我明明在学却没记上"。
        lines.append(f"- 📷 主动把摄像头让给别的程序(会议/通话/直播)共 "
                     f"{_hm(st.yield_seconds)} —— 这段时间没有监控数据, 不是你没专注")
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
    if st.ui_errors:
        # 提醒窗口自己炸了 = 提醒可能没弹出来。这是"漏报", 必须在可信度这一段里出现,
        # 否则"该弹的窗没弹"会完全不留痕迹。
        lines.append(f"- ⚠️ 提醒窗口报错 {st.ui_errors} 次 —— 有提醒可能**没有弹出来**, "
                     f"那几次你只会听到声音(详见日志里的 ui_error)")
    if st.shadowed_titles:
        # 白名单优先是**用户确认过的设计**(他用知乎/B站查考研资料), 所以这里不是警告,
        # 只是一句中性的披露。它有意义的地方在于"量": 如果一天几十次, 说明黑名单
        # 对你实际上基本不起作用了, 那时该调的是白名单里的通用词。
        lines.append(f"- 有 {st.shadowed_titles} 次窗口标题同时命中白/黑名单, 按**白名单**"
                     f"算成了专注(设计如此: 你在用知乎/B站查考研资料)。次数多说明黑名单"
                     f"基本不起作用了, 可以考虑收窄 `[whitelist]` 里的通用词")
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
