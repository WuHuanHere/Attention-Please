"""前台窗口标题 -> 专注 / 分心 / 未知。

判定顺序(按已确认的共识): **白名单优先**。
  - 标题先查白名单, 命中即算专注 —— 这样"Bilibili 课堂"不会被 "bilibili" 黑名单误杀;
  - 白名单没命中再查黑名单, 命中算分心;
  - 都没命中算"未知", 而未知**按专注处理**(你选了"浏览器默认算专注, 除非标题命中黑名单"),
    因为查资料、新标签页、空白标题都不该被念。

纯函数, 不依赖任何 IO。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TitleVerdict(str, Enum):
    FOCUS = "focus"
    DISTRACT = "distract"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TitleMatch:
    verdict: TitleVerdict
    matched: str | None = None
    source: str = ""  # "whitelist" | "blacklist" | ""
    # 白名单命中时, 如果标题**同时**命中了黑名单, 这里记下那个黑名单词。
    # 判定仍然按白名单(见模块开头), 但调用方必须把它记一笔 —— 否则
    # "考研数学基础班 - 知乎" 会被判成专注而且**什么都不留下**,
    # 用户以为知乎被黑名单守着, 实际上被通用词「数学」整个盖住了。
    shadowed: str | None = None

    @property
    def is_distraction(self) -> bool:
        return self.verdict is TitleVerdict.DISTRACT


def _hit(title_lower: str, words: list[str]) -> str | None:
    for w in words:
        w = w.strip()
        if w and w.lower() in title_lower:
            return w
    return None


def classify(title: str | None, whitelist: list[str], blacklist: list[str]) -> TitleMatch:
    if not title or not title.strip():
        return TitleMatch(TitleVerdict.UNKNOWN)
    low = title.lower()

    hit = _hit(low, whitelist)
    if hit is not None:
        return TitleMatch(TitleVerdict.FOCUS, hit, "whitelist",
                          shadowed=_hit(low, blacklist))

    hit = _hit(low, blacklist)
    if hit is not None:
        return TitleMatch(TitleVerdict.DISTRACT, hit, "blacklist")

    return TitleMatch(TitleVerdict.UNKNOWN)
