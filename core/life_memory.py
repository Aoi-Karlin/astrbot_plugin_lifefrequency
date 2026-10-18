# -*- coding: utf-8 -*-
"""与 LivingMemory 长期记忆插件联动：把她经历的事写进长期记忆（纯模块）。

## 为什么这样设计

**隔离语义**（用户确定）：*活动全局共享 + 群内事件分群*。

LivingMemory 的检索按 ``metadata.session_id`` **精确等值**过滤
（``bm25_retriever``: ``json_extract(metadata, '$.session_id') = ?``），
召回用的 scope 由它自己的 ``resolve_memory_scope`` 决定，群里就是 umo 本身。
因此有两件事必须分清：

- **她自己的生活**（下午在阳台看雨、睡了一觉）**不属于任何一次对话**，
  写进私有 scope ``livingmemory:life``。注意：这个 scope 在群里**检索不到**
  ——这是刻意的，它靠本插件自己的 ``inject_life_context`` 注入当前状态来共享，
  而不是靠记忆检索。写它只是为了**留存与可回顾**，不是为了被自动召回。
- **群内发生的事**（谁说了什么、她怎么回）写进**该群自己的 umo**，
  这样它和 LivingMemory 自己的对话记忆走同一条召回路径，天然按群隔离。

## 内容格式

用户选了「结构化记录」，但 LivingMemory 是向量 + BM25 检索：纯结构化文本
（``活动=阳台看雨``）很难被「今天下雨了」这类自然语言命中。所以每条记忆同时
带上 **结构化字段**（可读、可筛选、便于人工回顾）和**一句自然叙述**
（供 embedding 与分词命中）。两者拼在同一条 content 里，兼顾两种检索。

## 失败隔离

LivingMemory 没装、初始化失败、接口改名、写入抛异常——**一律降级为静默跳过**，
绝不影响本插件主流程。所有对外调用都包在 try/except 里。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping

#: 她自己的生活写进这个私有 scope（不属于任何对话）
LIFE_SCOPE = "livingmemory:life"

#: LivingMemory 插件可能的注册名（``metadata.yaml`` 里的 name / 模块名）
LIVINGMEMORY_NAMES: tuple[str, ...] = (
    "LivingMemory",
    "astrbot_plugin_livingmemory",
)

#: 重要性：日常活动偏低（不该盖过真实对话），群内交互略高
IMPORTANCE_ACTIVITY = 0.35
IMPORTANCE_DAILY_ARCHIVE = 0.5
IMPORTANCE_GROUP_EVENT = 0.45

#: ``memory_type`` 写这个，便于在 LivingMemory 的 WebUI 里筛选出来
MEMORY_TYPE = "LIFE"


@dataclass(frozen=True)
class MemoryEntry:
    """一条待写入的记忆。

    Attributes:
        content: 记忆正文（结构化行 + 自然叙述）。
        session_id: 归属 scope；``LIFE_SCOPE`` 或某个群的 umo。
        importance: 0~1 重要性。
        metadata: 额外元数据（会与该插件自己的字段合并）。
    """

    content: str
    session_id: str
    importance: float
    metadata: Mapping[str, Any]


def _clock_label(now: float, tz_offset_minutes: int) -> str:
    """本地时间标签，如 ``10-18 15:20``。"""
    stamp = time.gmtime(now + tz_offset_minutes * 60)
    return time.strftime("%m-%d %H:%M", stamp)


def mood_word(emotion: float) -> str:
    """把 0~10 的情绪值翻成一个词。

    Args:
        emotion: 情绪值。

    Returns:
        心情词，如 ``愉快`` / ``平静`` / ``低落``。
    """
    value = float(emotion)
    if value >= 8.0:
        return "很愉快"
    if value >= 6.5:
        return "挺开心"
    if value >= 5.5:
        return "还不错"
    if value >= 4.5:
        return "平静"
    if value >= 3.0:
        return "有点低落"
    if value >= 1.5:
        return "心情很差"
    return "非常难过"


def energy_word(energy: float) -> str:
    """把 0~10 的体力值翻成一个词。

    Args:
        energy: 体力值。

    Returns:
        体力词，如 ``精神很好`` / ``有点累`` / ``快撑不住了``。
    """
    value = float(energy)
    if value >= 8.0:
        return "精神很好"
    if value >= 6.0:
        return "还挺有精神"
    if value >= 4.0:
        return "有点累"
    if value >= 2.0:
        return "很累"
    return "快撑不住了"


def _date_label(now: float, tz_offset_minutes: int) -> str:
    stamp = time.gmtime(now + tz_offset_minutes * 60)
    return time.strftime("%Y-%m-%d", stamp)


def _sentence(activity_label: str, mood: str, energy_line: str, when: str) -> str:
    """把结构化字段串成一句能被人和模型都读懂的话。"""
    parts = [f"{when}她在{activity_label}"]
    if mood:
        parts.append(f"心情{mood}")
    if energy_line:
        parts.append(energy_line)
    return "，".join(parts) + "。"


def activity_memory(
    *,
    bot_name: str,
    activity_label: str,
    previous_label: str = "",
    mood_word: str = "",
    energy_word: str = "",
    health_word: str = "",
    scene_text: str = "",
    now: float,
    tz_offset_minutes: int = 480,
) -> MemoryEntry:
    """构造一条「她换了活动」的记忆（写入 ``LIFE_SCOPE``）。

    Args:
        bot_name: 她的名字。
        activity_label: 新活动的中文名（如 ``阳台看雨`` / ``睡觉``）。
        previous_label: 上一个活动的中文名，用于叙述「从……换成……」。
        mood_word: 心情词（如 ``平静``），空则省略。
        energy_word: 体力词（如 ``有点累``），空则省略。
        health_word: 健康词（如 ``有点感冒``），空则省略。
        scene_text: 模型给的场景描述，会截断后附在结构化行里。
        now: 当前时间戳。
        tz_offset_minutes: 时区偏移（分钟）。

    Returns:
        ``MemoryEntry``。
    """
    when = _clock_label(now, tz_offset_minutes)
    scene = (scene_text or "").strip()
    if len(scene) > 60:
        scene = scene[:60].rstrip() + "…"

    structured = [f"时间={when}", f"活动={activity_label}"]
    if previous_label and previous_label != activity_label:
        structured.append(f"上一项={previous_label}")
    if mood_word:
        structured.append(f"心情={mood_word}")
    if energy_word:
        structured.append(f"体力={energy_word}")
    if health_word:
        structured.append(f"身体={health_word}")
    if scene:
        structured.append(f"场景={scene}")

    # 自然叙述放在最前：向量检索对开头更敏感，也更容易被分词命中
    narration = _sentence(
        activity_label,
        mood_word,
        f"体力{energy_word}" if energy_word else "",
        when + "，",
    )
    if previous_label and previous_label != activity_label:
        narration = narration.rstrip("。") + f"（原本在{previous_label}）。"
    if scene:
        narration += f" {scene}"

    content = f"{narration}\n" + " ".join(structured)
    metadata: dict[str, Any] = {
        "memory_type": MEMORY_TYPE,
        "life_kind": "activity",
        "bot_name": bot_name,
        "activity": activity_label,
        "ts": float(now),
        "date": _date_label(now, tz_offset_minutes),
        "source": "life_frequency",
    }
    if previous_label and previous_label != activity_label:
        metadata["previous_activity"] = previous_label
    return MemoryEntry(
        content=content,
        session_id=LIFE_SCOPE,
        importance=IMPORTANCE_ACTIVITY,
        metadata=metadata,
    )


def daily_archive_memory(
    *,
    bot_name: str,
    date_label: str,
    highlights: list[str],
    mood_word: str = "",
    now: float,
    tz_offset_minutes: int = 480,
) -> MemoryEntry:
    """构造一条「这一天」的归档记忆（写入 ``LIFE_SCOPE``）。

    Args:
        bot_name: 她的名字。
        date_label: 日期，如 ``2026-10-18``。
        highlights: 当天活动的摘要行（按时间顺序）。
        mood_word: 心情词。
        now: 归档时刻。
        tz_offset_minutes: 时区偏移（分钟）。

    Returns:
        ``MemoryEntry``。
    """
    lines = [str(item).strip() for item in (highlights or []) if str(item).strip()]
    body = "；".join(lines) if lines else "没什么特别的"
    mood_part = f"，心情{mood_word}" if mood_word else ""
    narration = f"{date_label}这一天她过得很平常：{body}{mood_part}。"
    content = f"{narration}\n时间={date_label} 类型=日归档 " + " ".join(lines)
    metadata: dict[str, Any] = {
        "memory_type": MEMORY_TYPE,
        "life_kind": "daily",
        "bot_name": bot_name,
        "ts": float(now),
        "date": date_label,
        "source": "life_frequency",
    }
    return MemoryEntry(
        content=content,
        session_id=LIFE_SCOPE,
        importance=IMPORTANCE_DAILY_ARCHIVE,
        metadata=metadata,
    )


def group_event_memory(
    *,
    bot_name: str,
    umo: str,
    sender_name: str = "",
    user_text: str = "",
    reply_text: str = "",
    activity_label: str = "",
    now: float,
    tz_offset_minutes: int = 480,
) -> MemoryEntry:
    """构造一条「群里发生的事」的记忆（写入**该群自己的 umo**，天然分群）。

    Args:
        bot_name: 她的名字。
        umo: 该群的 unified_msg_origin（作为 session_id，保证按群隔离）。
        sender_name: 说话的人。
        user_text: 对方说的话（截断）。
        reply_text: 她的回复（截断）。
        activity_label: 她当时在做什么。
        now: 时间戳。
        tz_offset_minutes: 时区偏移（分钟）。

    Returns:
        ``MemoryEntry``。
    """
    when = _clock_label(now, tz_offset_minutes)
    who = (sender_name or "有人").strip()
    said = (user_text or "").strip()
    replied = (reply_text or "").strip()
    if len(said) > 80:
        said = said[:80].rstrip() + "…"
    if len(replied) > 80:
        replied = replied[:80].rstrip() + "…"

    narration_bits = [f"{when}{who}找她说话"]
    if said:
        narration_bits.append(f"说「{said}」")
    if activity_label:
        narration_bits.append(f"她当时在{activity_label}")
    if replied:
        narration_bits.append(f"她回「{replied}」")
    narration = "，".join(narration_bits) + "。"

    structured = [f"时间={when}", f"说话人={who}"]
    if activity_label:
        structured.append(f"她的活动={activity_label}")
    content = f"{narration}\n" + " ".join(structured)

    metadata: dict[str, Any] = {
        "memory_type": MEMORY_TYPE,
        "life_kind": "group_event",
        "bot_name": bot_name,
        "ts": float(now),
        "source": "life_frequency",
    }
    if activity_label:
        metadata["activity"] = activity_label
    return MemoryEntry(
        content=content,
        session_id=str(umo or "").strip(),
        importance=IMPORTANCE_GROUP_EVENT,
        metadata=metadata,
    )


def find_livingmemory_engine(context: Any) -> Any | None:
    """在已加载的插件里找 LivingMemory 的 ``memory_engine``。

    只依赖公开的 ``context.get_all_stars()``（返回 ``StarMetadata`` 列表，
    ``star_cls`` 是**插件实例**），不 import 对方的任何模块——所以它没装、
    版本不同、内部重构都不会让本插件导入失败。

    Args:
        context: AstrBot 的 ``Context``。

    Returns:
        可用的 ``MemoryEngine``，或 ``None``。
    """
    getter = getattr(context, "get_all_stars", None)
    if not callable(getter):
        return None
    try:
        stars = getter() or []
    except Exception:  # noqa: BLE001 — 宿主接口异常一律当没装
        return None

    wanted = {name.casefold() for name in LIVINGMEMORY_NAMES}
    for meta in stars:
        name = str(getattr(meta, "name", "") or "").strip()
        module_path = str(getattr(meta, "module_path", "") or "")
        if name.casefold() not in wanted and "livingmemory" not in module_path.casefold():
            continue
        instance = getattr(meta, "star_cls", None)
        if instance is None:
            continue
        initializer = getattr(instance, "initializer", None)
        engine = getattr(initializer, "memory_engine", None)
        if engine is None:
            continue
        # 初始化未完成时写入会失败，先跳过，下次再试
        ready = getattr(initializer, "is_initialized", False)
        if callable(ready):
            try:
                ready = ready()
            except Exception:  # noqa: BLE001
                ready = False
        if not ready:
            continue
        if getattr(engine, "hybrid_retriever", None) is None:
            continue
        return engine
    return None


async def write_entry(engine: Any, entry: MemoryEntry, *, persona_id: str | None = None) -> bool:
    """把一条记忆写进 LivingMemory。失败返回 ``False``，绝不抛。

    Args:
        engine: ``find_livingmemory_engine`` 的返回值。
        entry: 待写入的记忆。
        persona_id: 人格 id（一般留空，让记忆跨人格可见）。

    Returns:
        是否写入成功。
    """
    if engine is None or not entry.content.strip():
        return False
    try:
        await engine.add_memory(
            content=entry.content,
            session_id=entry.session_id,
            persona_id=persona_id,
            importance=float(entry.importance),
            metadata=dict(entry.metadata),
        )
        return True
    except Exception:  # noqa: BLE001 — 对方接口变了也不能拖垮本插件
        return False
