"""前台窗口标题 -> 专注 / 分心 / 未知。

判定顺序(按已确认的共识): **白名单优先**。
  - 标题先查白名单, 命中即算专注 —— 这样"Bilibili 课堂"不会被 "bilibili" 黑名单误杀;
  - 白名单没命中再查黑名单, 命中算分心;
  - 都没命中算"未知", 而未知**按专注处理**(你选了"浏览器默认算专注, 除非标题命中黑名单"),
    因为查资料、新标签页、空白标题都不该被念。

⚠️ **"白名单优先"是用户 2026-09-20 明确确认过的决定, 不要擅自改成"黑名单站点优先"。**
   原话:「考研数学基础班 - 知乎 应该归为专注, 因为这是我在利用平台查找相关考研资料,
   还有类似的我会在 b 站上看题目解析, 应该是白名单的优先级高于黑名单」。
   也就是说「知乎/哔哩哔哩/微博」这类站点名**故意**输给「数学/英语/考研/真题」这类
   学习词 —— 他是在用这些平台学习, 不是在摸鱼。
   `TitleMatch.shadowed` 会把"这次是白名单盖住了黑名单"记下来(`runtime._note_shadowed_title`
   写成 `title_shadowed` 事件), 那是**诊断数据**, 不是警告。

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
    # 判定仍然按白名单 —— 这是用户 2026-09-20 明确确认的决定(见模块 docstring):
    # 他用知乎/B站查考研资料, 站点名该输给学习词。
    # 记下来只是当**诊断数据**(`title_shadowed` 事件), 不是"可能漏判"的警告。
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
