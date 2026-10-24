# -*- coding: utf-8 -*-
"""主动开口提示词的模板渲染（v1.9.0）。

用户可以把内置的那段「要求」整体换成自己的模板，并在这里做 ``{占位符}`` 代换。

**为什么不用 ``str.format``**：用户写的模板里几乎一定会出现中文标点、JSON 片段、
甚至别的花括号（``{"a": 1}``）。``str.format`` 遇到没定义的键会抛 ``KeyError``，
遇到落单的 ``{`` 会抛 ``ValueError``——用户只是写错一个词，整次开口就废了。
所以这里用正则只替换**已知**的占位符名，其余原样保留。
"""

from __future__ import annotations

import re
from typing import Mapping

#: 单个占位符：``{name}``，名字限定为 ASCII 字母/数字/下划线。
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: 默认模板。留空时用它，保证老配置行为不变。
DEFAULT_TEMPLATE = (
    "【想跟对方说的话】{material}\n"
    "【开口的理由】{intent}\n"
    "{context}"
    "要求：用一两句口语把这句心里话说出来，符合人设与此刻的状态（{activity}）；"
    "不要解释这是谁安排的，不要用列表，不要提到「素材」「任务」「总结」这类词。"
)

#: 每个占位符的中文说明（给 WebUI 的 hint 用，也让报错更友好）。
PLACEHOLDER_HELP: dict[str, str] = {
    "material": "素材正文（她心里冒出来的那句话）",
    "intent": "这次开口的理由",
    "context": "对话上下文整段（含总结与最近几条原话；没有上下文时为空）",
    "activity": "她此刻在做什么",
    "emotion": "当前情绪，形如「7.3/10」",
    "energy": "当前体力，形如「3.1/10」",
    "mood": "情绪词，如「心情不错」",
    "energy_word": "体力词，如「有点累」",
    "health": "健康状况，如「健康」「感冒」",
    "time": "当前时间，如「21:05」",
    "date": "当前日期，如「10月21日」",
    "gap": "距上次搭话多久，如「约 5 小时」",
    "persona": "人设文本",
    "name": "她的名字",
}


def render(template: str, values: Mapping[str, object]) -> str:
    """把模板里的已知占位符替换成对应值。

    未知占位符、落单的花括号、中文全角括号一律原样保留，不抛异常。

    Args:
        template: 用户的模板（或默认模板）。
        values: 占位符 → 文本。

    Returns:
        替换后的文本。
    """

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)  # 未知占位符原样留着，用户能一眼看出拼错了
        text = values[key]
        return "" if text is None else str(text)

    return _PLACEHOLDER_RE.sub(_sub, template or "")


def unknown_placeholders(template: str, known: Mapping[str, object] | None = None) -> list[str]:
    """模板里用了但没人认识的占位符（用于启动时给个提醒）。"""

    names = set(known if known is not None else PLACEHOLDER_HELP)
    found: list[str] = []
    for name in _PLACEHOLDER_RE.findall(template or ""):
        if name not in names and name not in found:
            found.append(name)
    return found
