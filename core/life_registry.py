# -*- coding: utf-8 -*-
"""多份生活状态的管理（v1.8.0）。

**为什么需要它**：原先整个插件只有一份 ``LifeState``，所有会话看到同一个她。
用户要求「私聊每人单独、群聊统一」——所以现在按会话分派：

* **群聊**：全部共用同一份状态（``GROUP_STATE_KEY``），行为与旧版完全一致；
* **私聊**：每个用户一份，第一次出现时懒创建。

这一层只负责「哪份状态归谁」「怎么存盘」，不碰任何生活逻辑——真正的计算仍然
在 ``core.life_sim`` / ``core.life_activity`` 里，保持可测。
"""

from __future__ import annotations

from typing import Any, Iterable

from . import life_sim as S

#: 群聊共用的那套状态在存储里的键。用固定字符串而不是 umo，是为了让**所有**
#: 群的会话都落到同一份，同时把存盘格式里的历史数据直接当作它。
GROUP_STATE_KEY = "__group__"

#: 私聊状态键前缀。完整键形如 ``"private:12345"``。
PRIVATE_KEY_PREFIX = "private:"


def is_private_key(key: str) -> bool:
    """这个存储键是不是私聊状态。"""

    return str(key).startswith(PRIVATE_KEY_PREFIX)


def private_key(user_id: str) -> str:
    """私聊状态键：按**用户**而不是按会话。

    同一个人在不同平台/不同私聊入口下都该是同一个她，所以用平台内的用户 id。
    取不到 id 时退回 umo 本身，至少不会把所有人混成一个。
    """

    uid = str(user_id or "").strip()
    return f"{PRIVATE_KEY_PREFIX}{uid}" if uid else f"{PRIVATE_KEY_PREFIX}unknown"


def key_for_umo(umo: str, *, is_group: bool, user_id: str = "") -> str:
    """会话 → 状态键：群聊统一，私聊按用户分开。"""

    if is_group:
        return GROUP_STATE_KEY
    return private_key(user_id or umo)


class StateRegistry:
    """一份状态集合：群聊共用的 + 若干私聊的。

    刻意做成普通对象而不是插件属性集合，方便单测直接构造。
    """

    def __init__(self, group_state: S.LifeState | None = None) -> None:
        self.states: dict[str, S.LifeState] = {}
        if group_state is not None:
            self.states[GROUP_STATE_KEY] = group_state

    # -------------------------------------------------------------- 读

    @property
    def group(self) -> S.LifeState:
        """群聊共用的那份；没有就建一份空的（保证永不返回 None）。"""

        state = self.states.get(GROUP_STATE_KEY)
        if state is None:
            state = S.LifeState()
            self.states[GROUP_STATE_KEY] = state
        return state

    def get(self, key: str) -> S.LifeState | None:
        return self.states.get(str(key))

    def __contains__(self, key: object) -> bool:
        return str(key) in self.states

    def __len__(self) -> int:
        return len(self.states)

    def keys(self) -> Iterable[str]:
        return tuple(self.states)

    def private_items(self) -> list[tuple[str, S.LifeState]]:
        """只取私聊的那些（不含群聊共用）。"""

        return [(k, v) for k, v in self.states.items() if is_private_key(k)]

    # -------------------------------------------------------------- 写

    def ensure(self, key: str, *, now: float, config: S.SimConfig) -> S.LifeState:
        """取一份状态，没有就懒创建（私聊第一次出现时）。"""

        state = self.states.get(str(key))
        if state is None:
            state = S.new_state(now=now, config=config)
            self.states[str(key)] = state
        return state

    def put(self, key: str, state: S.LifeState) -> None:
        self.states[str(key)] = state

    def drop(self, key: str) -> bool:
        """删掉一份私聊状态（用于「重置这个人」）。群聊那份不许删。"""

        if str(key) == GROUP_STATE_KEY:
            return False
        return self.states.pop(str(key), None) is not None

    # -------------------------------------------------------------- 序列化

    def to_payload(self) -> dict[str, Any]:
        """打包成可落盘的结构。

        ``version=2`` 才带 ``private`` 段；旧的单状态文件仍能读（见 ``from_payload``）。
        """

        return {
            "version": 2,
            "group": self.group.to_dict(),
            "private": {
                key: state.to_dict() for key, state in self.private_items()
            },
        }

    @classmethod
    def from_payload(cls, raw: object, *, now: float, config: S.SimConfig) -> "StateRegistry":
        """从落盘内容恢复。

        兼容三种历史形态：

        * ``version=2`` 的多状态结构；
        * **旧版单状态**（顶层就是 ``LifeState`` 的字段，没有 ``group``/``version``）；
        * 完全损坏 → 只留一份新建的群聊状态，绝不抛异常。
        """

        registry = cls()
        if not isinstance(raw, dict):
            registry.states[GROUP_STATE_KEY] = S.new_state(now=now, config=config)
            return registry

        has_group = isinstance(raw.get("group"), dict)
        private_raw = raw.get("private")

        if has_group or isinstance(private_raw, dict):
            if has_group:
                registry.states[GROUP_STATE_KEY] = S.LifeState.from_dict(raw["group"])
            if isinstance(private_raw, dict):
                for key, value in private_raw.items():
                    name = str(key)
                    # 只接受 private: 前缀，避免损坏的键把群聊那份覆盖掉
                    if is_private_key(name) and isinstance(value, dict):
                        registry.states[name] = S.LifeState.from_dict(value)
        else:
            # 旧版：整个文件就是一份状态 → 当作群聊共用的那份（数据不丢）
            registry.states[GROUP_STATE_KEY] = S.LifeState.from_dict(raw)

        if GROUP_STATE_KEY not in registry.states:
            registry.states[GROUP_STATE_KEY] = S.new_state(now=now, config=config)
        return registry
