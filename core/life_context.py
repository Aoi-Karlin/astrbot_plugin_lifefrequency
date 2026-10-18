# -*- coding: utf-8 -*-
"""主动开口时的对话上下文（纯逻辑，便于测试）。

## 为什么需要

主动开口原来是「裸问」：只给模型一条素材 + 她的生活状态，完全不知道这个群
刚才在聊什么、对方是谁。结果就是她只能说「下雨了呢」这种放之四海而皆准的话。

## 三层信息

1. **近期摘要**：用小模型把最近 ``summary_turns`` 轮读一遍，压成几句话。
   解决「最近这个群在聊什么」——只看 10 条会断章取义。
2. **最近几条原文**：摘要会丢语气和细节，留几条原话当参照。
3. **隔了多久**：距上次主动搭话过了多长时间，决定语气是「顺口一提」还是
   「隔了好久又想起你」。

## 取哪一段

一律取**本会话**。只取当前会话，天然按群隔离，不会把 A 群的话带进 B 群。

## 这里不碰宿主

真正的取数与模型调用在 ``main.py``。这个模块只负责
「给定一堆消息行，裁出该给模型看的那几行」＋「把秒数说成人话」——
这部分有边界条件（空历史、全是工具调用、超长消息、负数时长），值得单独测。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 单条消息保留的最大字符数。太长的消息会把素材淹没。
MAX_LINE_CHARS = 300

#: 拼出来的上下文总长度上限，超过就从**最旧的**开始丢。
MAX_CONTEXT_CHARS = 2000

#: 摘要最多保留多少字（防止小模型啰嗦）。
MAX_SUMMARY_CHARS = 400

#: 给用户看的中文角色名。
ROLE_LABELS = {
    "user": "对方",
    "assistant": "她",
}


@dataclass(frozen=True)
class ContextBundle:
    """一段准备好的对话上下文。

    Attributes:
        lines: 已经排好序、裁好的对话行（每行就是一句话）。
        summary: 小模型压缩出来的「最近在聊什么」；空串表示没做总结。
        gap_text: 「距上次主动搭话多久」的人话；空串表示不显示。
        dropped: 因为长度/条数被丢掉的条数。
        empty_reason: 为什么没有上下文（空串表示有内容）。
    """

    lines: list[str] = field(default_factory=list)
    summary: str = ""
    gap_text: str = ""
    dropped: int = 0
    empty_reason: str = ""
    #: 是否因为这批上下文而**放弃这次开口**。
    #: 只有配置明确要求（``summary_on_fail=block``）时才为 True；
    #: 其余「取不到历史」的情况都只是降级，照样开口。
    abort: bool = False

    @property
    def available(self) -> bool:
        """有摘要、有原话、或只知道隔了多久，都算「有东西可参考」。

        隔了多久单独也有用：它决定语气是顺口一提还是久别重逢。
        """

        return bool(self.lines or self.summary or self.gap_text)

    def as_prompt_block(self) -> str:
        """拼成可以直接放进提示词的一段；什么都没有时返回空串。"""

        parts: list[str] = []
        if self.summary:
            parts.append(f"【最近聊到的大概】{self.summary}")
        if self.lines:
            parts.append("【最近这个会话的对话】\n" + "\n".join(self.lines))
        if self.gap_text:
            parts.append(f"【距上次主动搭话】{self.gap_text}")
        return "\n\n".join(parts)


def _clip(text: str, limit: int) -> str:
    """按字符裁断；超长时加省略号（保留头尾各一半，末尾的话往往更重要）。"""

    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head - 1
    return f"{text[:head]}…{text[-tail:]}" if tail > 0 else f"{text[:limit]}…"


def _normalize_line(raw: str) -> str:
    """把 AstrBot 给的 ``"User: xxx"`` / ``"Assistant: xxx"`` 换成中文标签。

    认不出来的行原样保留（宁可多一句看不懂的，也不要凭空丢掉信息）。
    """

    text = str(raw or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    for role, label in ROLE_LABELS.items():
        prefix = f"{role}:"
        if lowered.startswith(prefix):
            body = text[len(prefix) :].strip()
            if not body:
                return ""
            return f"{label}：{body}"
    return text


def build_bundle(
    raw_lines: list[str] | None,
    *,
    limit: int = 10,
    max_chars: int = MAX_CONTEXT_CHARS,
    max_line_chars: int = MAX_LINE_CHARS,
) -> ContextBundle:
    """把原始对话行裁成该给模型看的 ``ContextBundle``。

    Args:
        raw_lines: ``get_human_readable_context`` 返回的行（按时间正序）。
        limit: 最多保留几条（从最新的往回收）。
        max_chars: 总长度上限，超了从最旧的开始丢。
        max_line_chars: 单条长度上限。

    Returns:
        ``ContextBundle``。没有可用内容时 ``lines`` 为空且带上 ``empty_reason``。
    """

    source = [str(x) for x in (raw_lines or []) if str(x or "").strip()]
    if not source:
        return ContextBundle(empty_reason="这个会话还没有可用的对话记录")

    limit = max(0, int(limit))
    if limit == 0:
        return ContextBundle(empty_reason="上下文条数设为 0（已关闭）")

    # 从最新的往回收
    tail = source[-limit:]
    dropped = len(source) - len(tail)

    normalized = [line for line in (_normalize_line(raw) for raw in tail) if line]
    if not normalized:
        return ContextBundle(dropped=dropped, empty_reason="对话记录里没有可用文本")

    # 单条裁剪
    normalized = [_clip(line, max_line_chars) for line in normalized]

    # 总长度：从最旧的开始丢，直到装得下
    kept = list(normalized)
    while kept and sum(len(line) + 1 for line in kept) > max_chars:
        kept.pop(0)
        dropped += 1
    if not kept:
        return ContextBundle(dropped=dropped, empty_reason="对话内容超出长度上限")

    return ContextBundle(lines=kept, dropped=dropped)


def format_gap(seconds: float) -> str:
    """把「距上次主动搭话过了多久」说成人话。

    粒度故意做得粗：说「3 小时前」比「2 小时 47 分前」更像人记时间的方式，
    而且太精确反而会让模型拿这个数字做文章。
    """

    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return ""
    if value <= 0 or value != value:  # 0 / 负数 / NaN —— 就是没有记录
        return ""

    minutes = value / 60.0
    if minutes < 1:
        return "刚刚"
    if minutes < 60:
        return f"{int(minutes)} 分钟前"
    hours = minutes / 60.0
    if hours < 24:
        return f"{int(hours)} 小时前"
    days = hours / 24.0
    if days < 7:
        return f"{int(days)} 天前"
    if days < 30:
        return f"{int(days // 7)} 周前"
    if days < 365:
        return f"{int(days // 30)} 个月前"
    return f"{int(days // 365)} 年前"


def clean_summary(text: str, *, limit: int = MAX_SUMMARY_CHARS) -> str:
    """把小模型吐出来的摘要收拾干净。

    模型很爱加「好的，以下是摘要：」这种前缀、或者包一层代码块。留着会让
    最终提示词显得很乱，也让主模型跟着学这些套话。
    """

    out = str(text or "").strip()
    if not out:
        return ""

    # 去掉整体包裹的代码块
    if out.startswith("```"):
        lines = out.splitlines()
        if len(lines) >= 2:
            body = [ln for ln in lines[1:] if not ln.strip().startswith("```")]
            out = "\n".join(body).strip()

    # 去掉常见的开场套话（一行一行的剥，最多剥 3 行）
    prefixes = (
        "以下是总结",
        "以下是摘要",
        "以下是概括",
        "总结如下",
        "摘要如下",
        "对话总结",
        "最近的对话",
        "好的，",
        "好的：",
        "好的:",
    )
    for _ in range(3):
        before = out
        for prefix in prefixes:
            if out.startswith(prefix):
                out = out[len(prefix) :].lstrip("：:，, 　\n")
                break
        if out == before:
            break

    return _clip(out, limit)
