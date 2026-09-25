"""把日报的 markdown 渲染成**带样式的预览**(块解析是纯逻辑, Tk 只在渲染时才 import)。

## 为什么要自己写

用户 2026-09-25 的原话:「可以将专注日报界面的内容渲染成 md 的预览的画面吗, 不然这个
表格不太方便看」。原来的查看器是把 markdown 记号删掉后**原样**显示, 于是八列宽的表变成
一堆 `| 08:30–10:30 高数强化听课 | 2 小时 00 分 | 0 分钟 | …`, 完全对不齐 —— 表格是日报
里信息密度最高的一块, 恰恰最需要对齐。

## 为什么不用现成的库

这台机器上装第三方包要下载(用户在用移动流量), 而且日报是**我们自己生成的**, 用到的
markdown 只有一个很小的子集: `#`/`##` 标题、`> ` 引用、`- ` 列表(可缩进)、`| |` 表格、
`---` 分隔线, 加上行内的 `**粗体**` / `` `代码` `` / `*斜体*`。为这个子集写 60 行解析器,
比引一个依赖更划算, 也更好测。

## 分层

- `parse()` 把 markdown 变成**数据块**(`Heading` / `Paragraph` / ...) —— 这一层不 import
  tkinter, 所以能脱离窗口单测(见 `tests/test_mdview.py`)。
- `render()` 把数据块塞进一个 `tk.Text`, 用标签做字体/颜色/缩进。**表格用按像素算出来的
  tab 停靠位**对齐, 而不是靠空格 —— 中英混排时一个汉字不是一个西文字符宽, 用空格对齐
  必然错位; 像素停靠位由 `Font.measure()` 实测得出, 汉字、全角标点都不会错。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Union

# --------------------------------------------------------------------------
# 行内样式
# --------------------------------------------------------------------------
# 三个记号一起扫, 取最早出现的那个 —— 分开 replace 会让 `**a*b*c**` 这种互相吃掉。
_TOKEN = re.compile(
    r"\*\*(?P<bold>.+?)\*\*"
    r"|`(?P<code>[^`]+)`"
    r"|(?<!\*)\*(?P<italic>[^*\n]+)\*(?!\*)"
)
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_RULE = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
# 表格分隔行: 必须以 `|` 开头(否则会被 `---` 分隔线误判), 且至少有一个 `-`
_SEPARATOR = re.compile(r"^\|[\s:|-]*-[\s:|-]*\|?$")


@dataclass(frozen=True)
class Span:
    """一段同样式文字。"""

    text: str
    bold: bool = False
    code: bool = False
    italic: bool = False


Cell = list[Span]


def parse_spans(text: str) -> Cell:
    """把一行里的 `**粗体**` / `` `代码` `` / `*斜体*` 拆成带样式的片段。"""
    out: Cell = []
    pos = 0
    for m in _TOKEN.finditer(text):
        if m.start() > pos:
            out.append(Span(text[pos:m.start()]))
        if m.group("bold") is not None:
            out.append(Span(m.group("bold"), bold=True))
        elif m.group("code") is not None:
            out.append(Span(m.group("code"), code=True))
        else:
            out.append(Span(m.group("italic"), italic=True))
        pos = m.end()
    if pos < len(text):
        out.append(Span(text[pos:]))
    return out or [Span(text)]


def span_text(spans: Cell) -> str:
    """片段的纯文本(量宽度、算下标都用它)。"""
    return "".join(s.text for s in spans)


# --------------------------------------------------------------------------
# 块
# --------------------------------------------------------------------------
@dataclass
class Heading:
    level: int
    spans: Cell


@dataclass
class Paragraph:
    spans: Cell


@dataclass
class Bullet:
    level: int          # 0 = 顶层, 1 = 缩进一层(日报的"其中 …"就是这一层)
    spans: Cell


@dataclass
class Quote:
    spans: Cell


@dataclass
class Rule:
    pass


@dataclass
class Table:
    """`header` 与每一行都已经补齐成同样的列数(见 `parse`)。"""

    header: Cell
    rows: list[Cell] = field(default_factory=list)


Block = Union[Heading, Paragraph, Bullet, Quote, Rule, Table]


def _split_row(line: str) -> Cell:
    """`| a | b |` -> 两个单元格。日报生成器不写转义竖线, 所以直接 split。"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [parse_spans(part.strip()) for part in s.split("|")]


def _starts_block(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    return bool(s.startswith(("#", ">", "|", "- ", "* ")) or _RULE.match(s))


def parse(md: str) -> list[Block]:
    """把日报的 markdown 变成块列表。

    只认这个子集(见模块 docstring)。**认不出来的一律当普通段落** —— 宁可显示得朴素,
    也不能因为格式没料到就吞掉内容。
    """
    lines = md.splitlines()
    blocks: list[Block] = []
    i = 0
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        if not s:
            i += 1
            continue
        if _RULE.match(s):
            blocks.append(Rule())
            i += 1
            continue
        m = _HEADING.match(s)
        if m:
            blocks.append(Heading(len(m.group(1)), parse_spans(m.group(2).strip())))
            i += 1
            continue
        # 表格 = 一行 `|…|` 后面紧跟分隔行。只认到分隔行才当表, 免得把普通文字里的
        # 竖线误判成表。
        if s.startswith("|") and i + 1 < len(lines) and _SEPARATOR.match(lines[i + 1].strip()):
            header = _split_row(raw)
            i += 2
            rows: list[Cell] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i]))
                i += 1
            # 补齐列数: 手写的 markdown 少写一列时, 渲染层不该为它写防御代码。
            # ⚠️ 补的是**单元格**(list[Span]), 不是 Span —— 直接 append Span 会让
            # `span_text` 拿到一个不可迭代的对象(单测当场抓住过)。
            for row in rows:
                row.extend([[Span("")] for _ in range(len(header) - len(row))])
            blocks.append(Table(header=header, rows=rows))
            continue
        if s.startswith(">"):
            # 日报里的引用每行都是一条独立的提示, 所以一行一块(不按 markdown 那样合并)
            blocks.append(Quote(parse_spans(s[1:].strip())))
            i += 1
            continue
        if s.startswith(("- ", "* ")):
            indent = len(raw) - len(raw.lstrip())
            blocks.append(Bullet(1 if indent >= 2 else 0, parse_spans(s[2:].strip())))
            i += 1
            continue
        buf = [s]
        i += 1
        while i < len(lines) and not _starts_block(lines[i]):
            buf.append(lines[i].strip())
            i += 1
        blocks.append(Paragraph(parse_spans(" ".join(buf))))
    return blocks


# --------------------------------------------------------------------------
# 表格排版
# --------------------------------------------------------------------------
def column_widths(tbl: Table, measure: Callable[[str], int], pad: int = 16) -> list[int]:
    """每一列该有多宽(像素)。

    `measure` 由调用方注入(真实渲染时是 `tkinter.font.Font.measure`) —— 这样排版逻辑
    可以脱离 Tk 单测, 而且量宽度用的是**真实字体度量**: 一个汉字在这里自然就是两个
    西文字符宽, 不用手工按"全角算两个"去猜。
    """
    cols = len(tbl.header)
    widths = [0] * cols
    for row in [tbl.header] + tbl.rows:
        for j in range(cols):
            widths[j] = max(widths[j], measure(span_text(row[j])))
    return [w + pad for w in widths]


# --------------------------------------------------------------------------
# 渲染(这一层要 Tk)
# --------------------------------------------------------------------------
def render(body, md: str, *, avail_px: int = 900,
           family: str = "Microsoft YaHei UI", size: int = 10) -> None:
    """把 markdown 渲染进一个已经清空的 `tk.Text`。

    `avail_px` 是正文区可用的像素宽度 —— 表格比它宽时会**自动缩小字号**(最小 7pt)
    来塞进去。表一旦换行就彻底废了, 所以宁可字小一点。
    """
    import tkinter.font as tkfont

    base = tkfont.Font(family=family, size=size)
    mono = tkfont.Font(family="Consolas", size=max(7, size - 1))

    # 先配块级标签, 再配行内强调标签: Tk 里**后创建的标签优先级更高**, 所以
    # `**粗体**` 才能盖住段落的常规字体。反过来(先强调后块级)会让强调全部失效。
    body.tag_configure("h1", font=(family, size + 5, "bold"), foreground="#ffd166",
                       spacing1=6, spacing3=8)
    body.tag_configure("h2", font=(family, size + 2, "bold"), foreground="#8ecae6",
                       spacing1=16, spacing3=6)
    body.tag_configure("h3", font=(family, size + 1, "bold"), spacing1=8, spacing3=4)
    body.tag_configure("p", font=base, spacing3=5)
    body.tag_configure("quote", font=base, lmargin1=14, lmargin2=14, background="#1d1d1d",
                       foreground="#cfcfcf", spacing1=5, spacing3=5)
    body.tag_configure("b0", font=base, lmargin1=20, lmargin2=38, spacing3=3)
    body.tag_configure("b1", font=base, lmargin1=42, lmargin2=60, spacing3=3)
    body.tag_configure("rule", foreground="#3a3a3a", spacing1=8, spacing3=8)
    body.tag_configure("zebra", background="#181818")
    body.tag_configure("bold", font=(family, size, "bold"))
    body.tag_configure("italic", font=(family, size, "italic"))
    body.tag_configure("code", font=mono, background="#262626")

    seq = 0
    for blk in parse(md):
        if isinstance(blk, Heading):
            _insert_spans(body, blk.spans, f"h{min(blk.level, 3)}")
            body.insert("end", "\n")
        elif isinstance(blk, Paragraph):
            _insert_spans(body, blk.spans, "p")
            body.insert("end", "\n")
        elif isinstance(blk, Bullet):
            tag = "b0" if blk.level == 0 else "b1"
            body.insert("end", "•  " if blk.level == 0 else "–  ", tag)
            _insert_spans(body, blk.spans, tag)
            body.insert("end", "\n")
        elif isinstance(blk, Quote):
            _insert_spans(body, blk.spans, "quote")
            body.insert("end", "\n")
        elif isinstance(blk, Rule):
            body.insert("end", "─" * 46 + "\n", "rule")
        elif isinstance(blk, Table):
            seq += 1
            _insert_table(body, blk, seq, avail_px, family, size)


def _insert_spans(body, spans: Cell, tag: str, bold_tag: str = "bold") -> None:
    """插入一段带样式的文字, 并把 `tag` 打在整段上、强调样式打在片段上。"""
    for sp in spans:
        start = body.index("end-1c")
        body.insert("end", sp.text, tag)
        end = body.index("end-1c")
        if sp.bold:
            body.tag_add(bold_tag, start, end)
        if sp.code:
            body.tag_add("code", start, end)
        if sp.italic:
            body.tag_add("italic", start, end)


def _insert_table(body, tbl: Table, seq: int, avail_px: int,
                  family: str, size: int) -> None:
    """把一张表渲染成 tab 停靠位对齐的几行。

    每个表用**自己的一对标签**(字号和停靠位都可能不同), 而且这两个标签在块级标签之后
    创建 —— 所以表内 `**加粗**` 用的是表格自己的粗体字号, 不会突然变大一行。
    """
    import tkinter.font as tkfont

    tsize = size
    font = None
    widths: list[int] = []
    # 塞不下就**先缩字号、再缩列间距**。表一旦换行就彻底废了, 所以宁可字小、挤一点。
    # 三级足够: 1000px 宽的窗口里, 日报最宽的那张表(八列)在 10pt 下约 690px。
    for tsize, pad in ((size, 16), (max(8, size - 1), 12), (7, 8)):
        font = tkfont.Font(family=family, size=tsize)
        widths = column_widths(tbl, font.measure, pad=pad)
        if sum(widths) <= avail_px:
            break
    assert font is not None
    bold_font = tkfont.Font(family=family, size=tsize, weight="bold")

    tabs: list[int] = []
    x = 0
    for w in widths[:-1]:
        x += w
        tabs.append(x)
    tag, bold_tag = f"tbl{seq}", f"tbl{seq}b"
    body.tag_configure(tag, font=font, tabs=tuple(tabs))
    body.tag_configure(bold_tag, font=bold_font)

    for i, row in enumerate([tbl.header] + tbl.rows):
        line_start = body.index("end-1c")
        for j, cell in enumerate(row):
            if j:
                body.insert("end", "\t", tag)
            _insert_spans(body, cell, tag, bold_tag)
        body.insert("end", "\n", tag)
        line_end = body.index("end-1c")
        body.tag_add(tag, line_start, line_end)
        if i == 0:
            body.tag_add(bold_tag, line_start, line_end)     # 表头
        elif i % 2 == 0:
            body.tag_add("zebra", line_start, line_end)      # 隔行底色, 便于横着读数
