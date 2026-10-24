# -*- coding: utf-8 -*-
"""生活频率 · AstrBot 适配层。

这是 ``life-frequency``（MaiBot 插件，原作者 orge-8）的 AstrBot 移植版：

- ``core/`` 里是**纯模拟**（无 ctx、无 IO，测试里可以直接跑）；
- 本文件只做三件事：把 AstrBot 的配置/事件/模型/数据库接到 ``core`` 上，
  按 ``tick_seconds`` 推进她的生活，并在群聊纯闲聊上执行回复闸门。

与原版的关键差别（详见 README「与 MaiBot 版的差异」）：

1. **宿主倍率 → 自建闸门。** MaiBot 有 ``frequency.set_adjust`` 这种硬旋钮可以
   直接改回复频率；AstrBot 没有这一层，所以本插件自己挂一个优先级最高的
   handler：每条群聊纯闲聊都过一遍闸门（计数门 / 概率门），再决定要不要放行给
   默认 LLM。``@``、唤醒前缀、私聊、以及任何其它插件的指令**永不**被拦。
2. **经济维度 → AstrBot 的 token 统计。** 原版跨插件读 budget-pacer；这里改成查
   AstrBot 自己的 ``provider_stats`` 表，按「元 / 百万 token」单价折算成本月花费。
   经济只影响提示词里的语气与用词，**不参与倍率计算**（与原版立场一致）。
3. 原版的「写出倍率并回读校验」这套机制整体删除：没有宿主可写，也就没有
   写不进去的会话、不需要退避与归还。

加载流程（``Star.__init__`` 在 ``initialize()`` 之前执行）：

1. ``__init__``：读配置、建目录、载入状态、准备好各块配置对象；
2. ``initialize()``：配置坏了不阻塞插件加载，只是不启后台循环；
3. ``terminate()``：取消后台循环、状态落盘。
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import random
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star

from .core import config_adapter as CA
from .core import life_activity as A
from .core import life_economy as E
from .core import life_events as EV
from .core import life_factors as F
from .core import life_gate as G
from .core import life_context as CX
from .core import life_memory as M
from .core import life_proactive as P
from .core import life_prompt as PL
from .core import life_proactive_test as PT
from .core import life_registry as REG
from .core import life_sim as S
from .core import render as R

PLUGIN_NAME = "astrbot_plugin_lifefrequency"
STATE_FILENAME = "state.json"
STATE_BACKUP_FILENAME = "state.json.bak"

#: 闸门 handler 的优先级。AstrBot 的 handler 按 priority **降序**执行，100 保证它排
#: 在所有普通插件 handler 之前——但也意味着这里**只能读**事件状态，不能改别人。
GATE_PRIORITY = 100

#: ``/生活`` 指令组的别名。
LIFE_COMMAND_ALIASES = {"life", "频率"}

#: 沉默台账里，闸门自己的原因键前缀。
LEDGER_GATE_PREFIX = "gate_"

#: 闸门原因键 → 中文短语。``life_proactive.SKIP_REASON_LABELS`` 只认识主动开口
#: 那套原因，闸门这套由本文件补：否则 ``/生活 为什么`` 会印出 ``gate_pending``。
_GATE_REASON_LABELS: dict[str, str] = {
    "silent": "完全静默（倍率为 0）",
    "pending": "纯闲聊还没攒够条数",
    "cooldown": "距上次开口太近",
    "probability": "概率门命中",
    "dice": "概率门没掷中",
    "count": "计数门放行",
}


def _now() -> float:
    """当前时间戳（秒）。单独抽出来，测试里可以整块替换。"""
    return time.time()


class _GateOutcome:
    """闸门结论。

    Attributes:
        allow: 是否放行这条消息。
        register: 是否计入闸门计数（``@``/私聊/指令都记「对方说过话」，但不计数）。
    """

    __slots__ = ("allow", "register")

    def __init__(self, *, allow: bool, register: bool) -> None:
        self.allow = allow
        self.register = register

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"_GateOutcome(allow={self.allow}, register={self.register})"


# ================================================================ 主类


class LifeFrequencyPlugin(Star):
    """生活频率：让机器人有自己的作息、情绪、体力，并据此决定回不回复。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        """读配置、建目录、载入状态。

        Args:
            context: AstrBot 插件上下文。
            config: WebUI 注入的插件配置（``_conf_schema.json`` 的实例）。
        """
        super().__init__(context)
        self.conf: dict[str, Any] = dict(config) if config is not None else {}
        self._warned: set[str] = set()

        plugin_sec = CA.section(self.conf, "plugin")
        self.plugin_enabled = bool(CA.flag(plugin_sec, "enabled", True))
        """群聊总开关：关掉后群里完全不介入（不拦、不注入、不主动开口）。"""
        self.plugin_enabled_private = bool(CA.flag(plugin_sec, "enabled_private", True))
        """私聊总开关。两边都关才停后台循环；只开一边时循环照常跑。"""
        self.dry_run = bool(CA.flag(CA.section(self.conf, "simulation"), "dry_run", False))
        self.state_dir = Path(self._resolve_data_dir())
        self.state_path = self.state_dir / STATE_FILENAME
        self.backup_path = self.state_dir / STATE_BACKUP_FILENAME

        self._tick_task: asyncio.Task | None = None
        self._loop_lock = asyncio.Lock()
        """串行化 tick 与「被 @ 唤醒」这类会同时写状态的入口。"""

        self.rng = random.Random()
        self._last_activity_skip_log = 0.0
        self._economy_snapshot: E.EconomySnapshot | None = E.unavailable("尚未取数")
        self._last_economy_refresh = 0.0
        self.economy_thresholds = CA.economy_thresholds(self.conf)

        self._config_ok = True
        self._boot_error = ""
        self._registry = REG.StateRegistry()
        self._active: tuple[str, S.LifeState] | None = None
        """当前正在处理的状态（键, 状态）；``None`` = 用群聊共用的那份。"""
        try:
            self._load_and_build()
        except Exception as exc:  # noqa: BLE001 —— 配置写坏不许拖垮插件加载
            self._config_ok = False
            self._boot_error = f"{type(exc).__name__}: {exc}"
            self.sim_config = S.SimConfig()
            self.factor_config = F.FactorConfig()
            self.proactive_config = P.ProactiveConfig()
            self._registry = REG.StateRegistry(
                S.new_state(now=_now(), config=self.sim_config)
            )
            self._event_library = tuple(EV.BUILTIN_EVENTS)
            logger.error(
                f"[{PLUGIN_NAME}] 配置加载失败，插件将以最低配置空转：{self._boot_error}",
            )

        # 闸门/主动开口的会话记账：与状态共用同一份 dict，保证一起落盘
        self._gates: dict[str, G.SessionGate] = {}

        logger.info(
            f"[{PLUGIN_NAME}] 已加载：活动={self.state.activity}，"
            f"情绪={self.state.emotion:.1f}，体力={self.state.energy:.1f}，"
            f"状态文件={self.state_path}",
        )

    # ------------------------------------------------------------ 生命周期

    def _enabled_for(self, umo: str) -> bool:
        """这个会话是否启用插件：群聊看 ``enabled``，私聊看 ``enabled_private``。"""

        return self.plugin_enabled_private if not self._umo_is_group(umo) else self.plugin_enabled

    def _any_enabled(self) -> bool:
        """是否至少有一边开着——只有两边都关才停后台循环。"""

        return self.plugin_enabled or self.plugin_enabled_private

    async def initialize(self) -> None:
        """插件激活后启动后台生活循环（配置有误或两边都禁用时不启动）。"""
        if not self._any_enabled():
            logger.info(
                f"[{PLUGIN_NAME}] plugin.enabled 与 plugin.enabled_private 都为 false，"
                "后台循环不启动。",
            )
            return
        if not self._config_ok:
            logger.error(
                f"[{PLUGIN_NAME}] 配置有误（{self._boot_error}），后台循环不启动。"
                "修好配置后在 WebUI 里重载插件即可。",
            )
            return
        if self.dry_run:
            logger.info(
                f"[{PLUGIN_NAME}] simulation.dry_run=true：只推进生活，不拦截、不主动开口。",
            )
        await self._restart_loop()

    async def terminate(self) -> None:
        """插件被禁用/重载时停掉后台循环并把状态落盘。"""
        task = self._tick_task
        self._tick_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.exception(f"[{PLUGIN_NAME}] 后台循环退出时出错")
        try:
            self._save_state()
        except Exception:  # noqa: BLE001
            logger.exception(f"[{PLUGIN_NAME}] 退出时保存状态失败")
        logger.info(f"[{PLUGIN_NAME}] 已卸载。")

    # ------------------------------------------------------------ 配置与路径

    def _resolve_data_dir(self) -> str:
        """插件数据目录：``data/plugin_data/<插件名>``；取不到时退到临时目录。

        规范要求持久化数据写在 ``data/`` 下（不能写进插件目录，否则更新时会被覆盖）。
        """
        base: Path | None = None
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            base = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        except Exception:  # noqa: BLE001 —— 老版本/测试环境可能没有这个工具函数
            base = None
        if base is None:
            base = Path(os.path.join(os.getcwd(), "plugin_data", PLUGIN_NAME))
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            fallback = Path(os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp")
            base = fallback / PLUGIN_NAME
            base.mkdir(parents=True, exist_ok=True)
        return str(base)

    def _warn(self, key: str, message: str) -> None:
        """配置告警：同一条只写一次，避免每个 tick 刷屏。"""
        if key in self._warned:
            return
        self._warned.add(key)
        logger.warning(f"[{PLUGIN_NAME}] {message}")

    def _build_configs(self) -> None:
        """把整份插件配置编译成 ``core`` 需要的配置对象。"""
        warn = self._warn
        date_sec = CA.section(self.conf, "date")
        _, festival_warnings = S.parse_festival_lines(
            CA.str_list(date_sec, "festivals", []),
        )
        for item in festival_warnings:
            warn(f"festival:{item}", item)

        date_overrides: dict[str, float] = {}
        for line in CA.str_list(date_sec, "date_factors", []):
            if "=" not in line:
                warn(f"date_factor:{line}", f"日期倍率行 {line!r} 不是「名称=倍率」，已跳过")
                continue
            name, value = line.split("=", 1)
            try:
                date_overrides[name.strip()] = float(value.strip())
            except ValueError:
                warn(f"date_factor:{line}", f"日期倍率行 {line!r} 的倍率不是数字，已跳过")

        self.sim_config = CA.sim_config(self.conf, date_overrides=date_overrides, warn=warn)
        self.factor_config = CA.factor_config(self.conf, warn=warn)
        decay_floor = CA.num(CA.section(self.conf, "events"), "material_decay_floor", 0.25)
        self.proactive_config = CA.proactive_config(
            self.conf, decay_floor=decay_floor, warn=warn
        )

    def _load_and_build(self) -> None:
        """编译配置 + 载入状态；两边任何一边失败都抛给调用方。"""
        self._build_configs()
        self._event_library = self._build_event_library()
        now = _now()
        loaded = self._read_state_file()
        if loaded is None:
            self._registry = REG.StateRegistry(S.new_state(now=now, config=self.sim_config))
            self._save_state()
            logger.info(f"[{PLUGIN_NAME}] 没有可用状态文件，已按初始状态新建。")
        else:
            self._registry = loaded

    def _build_event_library(self) -> tuple[EV.LifeEvent, ...]:
        """内建事件库 + ``events.extra`` 追加 − ``events.disabled`` 禁用。"""
        sec = CA.section(self.conf, "events")
        merged, warnings = EV.merge_events(
            CA.str_list(sec, "extra", []),
            CA.str_list(sec, "disabled", []),
            builtin=EV.BUILTIN_EVENTS,
        )
        for item in warnings:
            self._warn(f"event:{item}", item)
        return tuple(merged)

    # ------------------------------------------------------------ 状态读写

    @property
    def state(self) -> S.LifeState:
        """**当前**正在处理的那份生活状态。

        默认是群聊共用的那份；在 ``_active_state`` 里推进私聊状态时，它会临时指向
        私聊的那一份。任何与会话相关的逻辑都应该先用 ``_state_for_event`` 选好状态。
        """
        active = getattr(self, "_active", None)
        if active is not None:
            return active[1]
        return self._registry.group

    @state.setter
    def state(self, value: S.LifeState) -> None:
        active = getattr(self, "_active", None)
        if active is not None:
            key = active[0]
            self._registry.put(key, value)
            self._active = (key, value)
        else:
            self._registry.put(REG.GROUP_STATE_KEY, value)

    def _state_key_for_umo(self, umo: str, *, is_group: bool, user_id: str = "") -> str:
        """会话 → 状态键（群聊统一，私聊按用户分开）。"""

        return REG.key_for_umo(umo, is_group=is_group, user_id=user_id)

    def _state_for_umo(self, umo: str, *, user_id: str = "") -> S.LifeState:
        """按 umo 取状态；私聊会话首次出现时懒创建。"""

        return self._registry_for_umo(umo, user_id=user_id)[1]

    def _registry_for_umo(
        self, umo: str, *, user_id: str = ""
    ) -> tuple[str, S.LifeState]:
        """按 umo 取 (状态键, 状态)，私聊首次出现时懒创建。

        群聊一律落到共用的那份；取不到 umo 时退回群聊那份（宁可读错也不崩）。
        """

        umo = str(umo or "")
        if not umo:
            return REG.GROUP_STATE_KEY, self._registry.group
        is_group = self._umo_is_group(umo)
        key = self._state_key_for_umo(
            umo, is_group=is_group, user_id="" if is_group else (user_id or umo)
        )
        state = self._registry.get(key)
        if state is None:
            state = self._registry.ensure(key, now=_now(), config=self.sim_config)
            if REG.is_private_key(key):
                logger.info(f"[{PLUGIN_NAME}] 为私聊新建独立生活状态：{key}")
        return key, state

    def _state_for_event(self, event: AstrMessageEvent) -> S.LifeState:
        """按事件取状态：私聊各自一套，群聊共用一套。"""

        return self._state_for_umo(self._safe_umo(event), user_id=self._sender_of(event))

    @staticmethod
    def _sender_of(event: AstrMessageEvent) -> str:
        """取发送者 id；拿不到就返回空串（调用方会退回用 umo 区分）。"""

        try:
            return str(getattr(event, "get_sender_id", lambda: "")() or "")
        except Exception:  # noqa: BLE001 —— 各平台实现的差异，不值得让消息处理失败
            return ""

    def _read_state_file(self) -> REG.StateRegistry | None:
        """读取状态文件：先主线，坏了再退到备份。

        注意备份的语义是「**上一次**成功保存的内容」，所以它天生比主线旧一个版本。
        这条路径只用于主线损坏（断电、磁盘满、手改成半截 JSON）时别丢她的一生。
        """
        now = _now()
        for path in (self.state_path, self.backup_path):
            if not path.exists():
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.error(f"[{PLUGIN_NAME}] 状态文件 {path.name} 读不出来（{exc}）")
                if path == self.state_path:
                    self._quarantine_state_file(exc)
                continue
            try:
                registry = REG.StateRegistry.from_payload(
                    raw, now=now, config=self.sim_config
                )
            except Exception as exc:  # noqa: BLE001 —— 结构坏了也不许拖垮加载
                logger.error(
                    f"[{PLUGIN_NAME}] 状态文件 {path.name} 结构异常（{exc}），已忽略",
                )
                continue
            if path == self.backup_path:
                logger.warning(
                    f"[{PLUGIN_NAME}] 已从备份 {path.name} 恢复"
                    f"（活动={registry.group.activity}，"
                    f"私聊状态 {len(registry.private_items())} 份）",
                )
            return registry
        return None

    def _quarantine_state_file(self, exc: Exception) -> None:
        """把坏状态文件改名留证，避免下一次保存把它覆盖掉。"""
        try:
            target = self.state_path.with_name(f"state.bad-{int(_now())}.json")
            self.state_path.replace(target)
            logger.warning(f"[{PLUGIN_NAME}] 坏状态文件已保留为 {target.name}（{exc}）")
        except OSError:
            logger.warning(f"[{PLUGIN_NAME}] 坏状态文件无法改名留证：{exc}")

    def _save_state(self) -> None:
        """原子写状态：先写 ``.tmp`` 再 ``os.replace``，并留一份上一版备份。"""
        text = json.dumps(
            self._registry.to_payload(), ensure_ascii=False, indent=2, sort_keys=True
        )
        tmp = self.state_path.with_name("state.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            if self.state_path.exists():
                try:
                    self.backup_path.write_bytes(self.state_path.read_bytes())
                except OSError:
                    pass
            os.replace(tmp, self.state_path)
        except OSError as exc:
            logger.error(f"[{PLUGIN_NAME}] 保存状态失败：{exc}")

    # ------------------------------------------------------------ 后台循环

    async def _restart_loop(self) -> None:
        """（重新）启动后台循环；旧任务先干净地取消。"""
        task = self._tick_task
        self._tick_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.exception(f"[{PLUGIN_NAME}] 旧循环退出时出错")
        self._tick_task = asyncio.create_task(self._sim_loop())

    async def _sim_loop(self) -> None:
        """按 ``tick_seconds`` 推进生活；单轮异常只记日志，绝不退出循环。"""
        tick = max(60, int(self.sim_config.tick_seconds))
        while True:
            try:
                await asyncio.sleep(tick)
                await self._sim_tick(_now())
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 —— 循环里任何异常都不许把任务打死
                logger.exception(f"[{PLUGIN_NAME}] 生活循环本轮出错，下一轮继续")

    async def _sim_tick(self, now: float) -> None:
        """一轮推进：依次推进群聊共用的那份 + 所有私聊那份。

        顺序与原版 ``_sim_tick`` 一致，唯一删掉的是「把倍率下发到宿主」——
        在 AstrBot 里这一步变成了来消息时的即时闸门判定。

        多份状态的处理见 ``_tick_one``：群聊那份每轮都推进；私聊那份只有**活跃**
        （最近说过话）才问模型，否则只用时段表/强制层推进，不花 token。
        """
        async with self._loop_lock:
            self._refresh_cheap_config()
            for key in self._tickable_state_keys(now):
                try:
                    await self._tick_one(key, now)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 —— 一份状态出问题不许拖垮其他
                    logger.exception(f"[{PLUGIN_NAME}] 推进状态 {key} 时出错，跳过")
            self._save_state()

    def _tickable_state_keys(self, now: float) -> list[str]:
        """这一轮该推进哪些状态：群聊那份 + 活跃的私聊。

        「活跃」的判据是 ``proactive.active_hours``（默认 24 小时）内说过话——
        没人会聊的会话不必每 10 分钟花一次 token 问她"在干嘛"。

        开关也按边生效：群聊关了就不停群聊那份（它反正没人看），私聊仍照常推进；
        只开一边时循环也照跑——见 ``_any_enabled``。
        """

        keys = [REG.GROUP_STATE_KEY] if self.plugin_enabled else []
        if not self.plugin_enabled_private:
            return keys
        window = max(1, int(CA.integer(CA.section(self.conf, "proactive"), "active_hours", 24)))
        cutoff = now - window * 3600
        for key, state in self._registry.private_items():
            session = self._sessions().get(key) or {}
            last_seen = float(session.get("last_user_at") or 0.0)
            if last_seen >= cutoff or last_seen <= 0:
                keys.append(key)
        return keys

    async def _tick_one(self, key: str, now: float) -> None:
        """推进指定的那份状态。"""

        state = self._registry.get(key) or self._registry.ensure(
            key, now=now, config=self.sim_config
        )
        with self._active_state(state, key):
            self.state = S.settle(
                state,
                now=now,
                config=self.sim_config,
                events=self._event_library,
                rng=self.rng,
                on_offline_gap=self._on_offline_gap,
            )
            decision = await self._ask_activity(now)
            previous_activity = self.state.activity
            self.state = S.enforce_and_apply(
                self.state, now=now, config=self.sim_config, decision=decision
            )
            self._maybe_reseed(now)
            await self._note_activity_memory(now, previous_activity)
            if key == REG.GROUP_STATE_KEY:
                # 经济与主动开口仍然按群聊那套口径跑一次，不因私聊状态翻倍
                await self._refresh_economy(now)
                await self._maybe_proactive(now)
            logger.debug(
                f"[{PLUGIN_NAME}] tick[{key}]：{R.activity_label(self.state.activity)}"
                f"（{self.state.activity_source}）情绪 {self.state.emotion:.1f}"
                f" 体力 {self.state.energy:.1f}",
            )

    @contextmanager
    def _active_state(self, state: S.LifeState, key: str):
        """临时把「当前状态」指向指定那份，退出时还原。

        这是为了**不必改一百处** ``self.state``：现有逻辑一律读写「当前状态」，
        由这里决定当前是哪一份。群聊与私聊的差异只存在于「选哪份」这一步。
        """

        previous = getattr(self, "_active", None)
        self._registry.put(key, state)
        self._active = (key, state)
        try:
            yield state
        finally:
            current = self._registry.get(key)
            if current is not None:
                self._registry.put(key, current)
            self._active = previous

    def _on_offline_gap(self, gap_minutes: int, crossed_boundary: bool) -> None:
        """停机间隙的日志回调（那段时间不记账，见 ``life_sim.settle``）。"""
        logger.info(
            f"[{PLUGIN_NAME}] 停机约 {gap_minutes / 60.0:.1f} 小时，这段时间不记账"
            + ("（跨了生活日边界）" if crossed_boundary else ""),
        )

    def _refresh_cheap_config(self) -> None:
        """每轮重读「随时会被 WebUI 改掉」的轻量字段（价格、阈值、暂停开关）。

        结构性配置（``tick_seconds``、``sleep_window`` 等）需要重载插件才生效——
        WebUI 保存配置本来就会触发重载，所以这里只兜「运行中被改」的少数情况。
        """
        try:
            self.economy_thresholds = CA.economy_thresholds(self.conf)
        except Exception as exc:  # noqa: BLE001
            self._warn("economy_thresholds", f"经济阈值解析失败，沿用上一轮：{exc}")

    # ------------------------------------------------------------ 活动决策

    async def _ask_activity(self, now: float) -> A.ActivityDecision | None:
        """问模型她现在在做什么；``None`` = 没有有效提议，由强制层保持现状。"""
        mode = CA.text(CA.section(self.conf, "activity"), "mode", "llm").lower()
        if mode == "rules":
            return self._rules_decision(now)

        llm_sec = CA.section(self.conf, "activity_llm")
        if CA.flag(llm_sec, "skip_when_forced", True):
            pointless = S.pointless_ask_reason(self.state, now=now, config=self.sim_config)
            if pointless:
                S.mark_ask_skipped(self.state, reason=pointless)
                self._log_activity_skip(now, pointless)
                return None

        min_interval = max(0, CA.integer(llm_sec, "min_interval_seconds", 600))
        last_success = float(self.state.llm_last_success_at or 0.0)
        if min_interval > 0 and last_success > 0 and (now - last_success) < min_interval:
            return None
        if float(self.state.llm_cooldown_until or 0.0) > now:
            return None

        prompt = self._build_activity_prompt(now)
        try:
            text = await self._call_activity_model(prompt, llm_sec)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 模型挂了不许影响她的生活
            self._note_activity_failure(now, f"{type(exc).__name__}: {exc}", llm_sec)
            return None

        raw = (text or "").strip()
        if not raw:
            self._note_activity_failure(now, "模型返回空内容", llm_sec)
            return None
        # 模型偶尔会回一句「她睡着了」而不是 JSON：parse_response 会返回 None，
        # 这属于「没有有效提议」，走保持现状那条路，不算故障。
        parsed = A.parse_response(raw, max_scene_chars=40)
        if parsed is None:
            self._note_activity_failure(now, raw, llm_sec)
            return None

        S.mark_llm_success(self.state, now=now, raw=raw)
        return A.ActivityDecision(
            activity=parsed.activity,
            scene=parsed.scene,
            source=A.SOURCE_LLM,
            note="模型决策",
        )

    def _log_activity_skip(self, now: float, reason: str) -> None:
        """「本轮注定不用问模型」的日志，15 分钟最多一条。"""
        if now - self._last_activity_skip_log < 15 * 60:
            return
        self._last_activity_skip_log = now
        logger.debug(f"[{PLUGIN_NAME}] 本轮未问模型：{reason}")

    def _rules_decision(self, now: float) -> A.ActivityDecision:
        """``activity.mode=rules``：完全按内置时段表取活动。"""
        local_now = S.local_datetime(now, self.sim_config.tz_offset_minutes)
        return A.rule_based_activity(
            now_minutes=local_now.hour * 60 + local_now.minute,
            energy=float(self.state.energy),
            sick=S.is_cold(self.state, now),
            sleep_energy_threshold=float(self.sim_config.sleep_energy_threshold),
            schedule=A.schedule_facts(local_now, self.sim_config.schedule),
            work_scene=str(self.sim_config.schedule.work_scene or ""),
        )

    def _note_activity_failure(
        self, now: float, raw: str, llm_sec: Mapping[str, Any]
    ) -> None:
        """记一次模型失败；到上限就进冷却（冷却期间按「无有效提议」保持现状）。"""
        limit = max(1, CA.integer(llm_sec, "fail_streak_limit", 3))
        S.mark_llm_failure(self.state, now=now, raw=raw, config_limit=limit)
        streak = int(self.state.llm_fail_streak)
        if streak >= limit:
            cooldown = max(0, CA.integer(llm_sec, "cooldown_minutes", 30)) * 60
            self.state.llm_cooldown_until = now + cooldown
            logger.warning(
                f"[{PLUGIN_NAME}] 活动决策连续失败 {streak} 次，冷却 {cooldown // 60} 分钟：{raw}",
            )
        else:
            logger.warning(f"[{PLUGIN_NAME}] 活动决策失败（第 {streak} 次）：{raw}")

    def _build_activity_prompt(self, now: float) -> str:
        """按原版口径拼活动决策提示词（近期经历分三层、人设截断、经济约束）。"""
        sim = self.sim_config
        facts = S.enforce_facts(self.state, now=now, config=sim)
        date_ctx = S.date_context(self.state, now, sim)
        can_switch, block_reason = S.can_switch(self.state, now, sim)
        llm_sec = CA.section(self.conf, "activity_llm")
        economy_hint = self._economy_hint(now)
        return A.build_prompt(
            A.PromptInput(
                bot_name=self._bot_name(),
                persona=self._persona_text(),
                now_label=date_ctx["now_label"],
                date_label=date_ctx["date_label"],
                season=date_ctx["season"],
                festival=date_ctx["festival"],
                activity=self.state.activity,
                minutes_in_activity=S.activity_minutes(self.state, now),
                can_switch=can_switch,
                switch_block_reason=block_reason,
                emotion=float(self.state.emotion),
                energy=float(self.state.energy),
                energy_cap=float(self.state.energy_cap),
                health_label=S.health_label(self.state, now, sim),
                sleep_debt_nights=int(self.state.sleep_debt_nights),
                sleep_minutes_today=int(self.state.sleep_minutes_today),
                awake_minutes_today=int(self.state.awake_minutes_today),
                sleep_window_text=sim.sleep_window_text,
                recent_event_tiers=S.recent_event_tiers(
                    self.state,
                    now=now,
                    limit=max(0, CA.integer(llm_sec, "recent_events_in_prompt", 8)),
                    near_hours=CA.num(llm_sec, "recent_near_hours", 12.0),
                    mid_hours=CA.num(llm_sec, "recent_mid_hours", 72.0),
                    far_days=CA.num(llm_sec, "recent_far_days", 14.0),
                    tz_offset_minutes=sim.tz_offset_minutes,
                    pick=CA.text(llm_sec, "recent_pick_mode", "smart"),
                    max_per_label=max(1, CA.integer(llm_sec, "recent_max_per_label", 1)),
                ),
                schedule_lines=tuple(facts.schedule.prompt_lines or ()),
                effect_lines=S.activity_effect_lines(
                    sim,
                    activity_factors=self.factor_config.activity_factors,
                    at_wake=bool(sim.wake_on_at),
                    mention_economy=bool(economy_hint),
                ),
                economy_hint=economy_hint,
                max_persona_chars=max(0, CA.integer(llm_sec, "persona_max_chars", 600)),
            ),
        )

    async def _call_activity_model(self, prompt: str, llm_sec: Mapping[str, Any]) -> str:
        """调一次活动决策模型，返回原始文本。"""
        provider_id = CA.text(llm_sec, "provider_id") or self._default_provider_id()
        if not provider_id:
            raise RuntimeError("没有可用的对话模型（未配置 activity_llm.provider_id，也没有默认模型）")
        timeout_ms = max(5000, CA.integer(llm_sec, "timeout_ms", 120000))
        response = await asyncio.wait_for(
            self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                temperature=CA.num(llm_sec, "temperature", 0.8),
                max_tokens=max(50, CA.integer(llm_sec, "max_tokens", 200)),
            ),
            timeout=timeout_ms / 1000.0,
        )
        return str(getattr(response, "completion_text", "") or "")

    def _default_provider_id(self, section: str = "activity_llm", umo: str = "") -> str:
        """插件自己发起模型调用时用哪个模型。

        顺序：

        1. 该分组的 ``provider_id``（WebUI 下拉里选的模型名）；
        2. AstrBot 当前会话在用的模型（``get_using_provider``）——就是正常回复
           会用的那个，所以只要机器人能正常回话，这里就一定拿得到；
        3. 全局 ``provider_settings.default_provider_id``（老版本兼容）。

        第 2 步是关键：``provider_settings.default_provider_id`` 在新版 AstrBot 里
        已经不存在，只靠它会永远拿到空串，主动开口也就永远发不出去。

        Args:
            section: 先查哪个配置分组的 ``provider_id``。
            umo: 会话标识；给出时优先按该会话解析模型（会话可能单独绑定了模型）。
        """
        configured = CA.text(CA.section(self.conf, section), "provider_id")
        if configured:
            return configured
        try:
            getter = getattr(self.context, "get_using_provider", None)
            if callable(getter):
                provider = getter(umo or None)
                meta = getattr(provider, "meta", None)
                provider_id = str(getattr(meta, "id", "") or "").strip()
                if provider_id:
                    return provider_id
        except Exception:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 读取当前会话模型失败", exc_info=True)
        try:
            cfg = self.context.get_config()
            settings = cfg.get("provider_settings", {}) if cfg else {}
            return str(settings.get("default_provider_id", "") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _maybe_reseed(self, now: float) -> None:
        """模型长期不交作业时按内置时段表重新取种子（安全阀）。"""
        hours = CA.num(CA.section(self.conf, "activity"), "reseed_after_hours", 8.0)
        if not S.should_reseed(self.state, now=now, hours=hours):
            return
        seed = self._rules_decision(now)
        decision = A.ActivityDecision(
            activity=seed.activity,
            scene=seed.scene,
            source=A.SOURCE_RULES,
            note=f"模型连续 {hours:g} 小时没有成功决策，按时段表重新取种子",
        )
        self.state = S.apply_activity(self.state, decision, now=now, config=self.sim_config)
        S.mark_llm_success(self.state, now=now, raw="reseed")
        logger.info(
            f"[{PLUGIN_NAME}] 已按时段表重新取活动：{R.activity_label(self.state.activity)}",
        )

    # ------------------------------------------------------------ 人设与经济

    def _bot_name(self) -> str:
        """她的名字：插件配置优先，其次 AstrBot 的人格名，最后兜底。"""
        name = CA.text(CA.section(self.conf, "persona"), "bot_name")
        if name:
            return name
        try:
            persona = self.context.persona_manager.get_persona_v3_by_id("default")
            value = str((persona or {}).get("name", "") or "").strip()
            if value:
                return value
        except Exception:  # noqa: BLE001
            pass
        return "她"

    def _persona_text(self) -> str:
        """人设文本：插件配置优先，其次 AstrBot 的默认人格。"""
        configured = CA.text(CA.section(self.conf, "persona"), "text")
        if configured:
            return configured
        try:
            persona = self.context.persona_manager.get_persona_v3_by_id("default")
            return str((persona or {}).get("prompt", "") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    async def _refresh_economy(self, now: float) -> None:
        """按 ``refresh_interval_minutes`` 从 ``provider_stats`` 折算本月花费。"""
        sec = CA.section(self.conf, "economy")
        if not CA.flag(sec, "enabled", False):
            self._economy_snapshot = E.unavailable("economy.enabled=false")
            return
        interval = max(1, CA.integer(sec, "refresh_interval_minutes", 15)) * 60
        if self._last_economy_refresh > 0 and (now - self._last_economy_refresh) < interval:
            return
        self._last_economy_refresh = now
        try:
            self._economy_snapshot = await self._fetch_economy(now, sec)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 统计表读不到就当经济不可用
            self._economy_snapshot = E.unavailable(f"{type(exc).__name__}: {exc}")
        snapshot = self._economy_snapshot
        if snapshot is not None and snapshot.ok:
            logger.debug(
                f"[{PLUGIN_NAME}] 经济：本月已花 {snapshot.spend:.3f}"
                f" / 生活费 {snapshot.budget:.3f}",
            )
        else:
            logger.info(
                f"[{PLUGIN_NAME}] 经济维度不可用："
                f"{snapshot.error if snapshot else '未知原因'}",
            )

    async def _fetch_economy(
        self, now: float, sec: Mapping[str, Any]
    ) -> E.EconomySnapshot:
        """查本月 token 用量并折成金额。"""
        tz_offset = self.sim_config.tz_offset_minutes
        totals = await self._query_token_totals(now, tz_offset)
        price_in = CA.num(sec, "price_input_per_mtok", 0.0)
        price_cached = CA.num(sec, "price_cached_input_per_mtok", 0.0)
        price_out = CA.num(sec, "price_output_per_mtok", 0.0)

        def _cost(row: Mapping[str, float]) -> float:
            return E.tokens_cost(
                row.get("input_other", 0.0),
                row.get("input_cached", 0.0),
                row.get("output", 0.0),
                price_input_per_mtok=price_in,
                price_cached_input_per_mtok=price_cached,
                price_output_per_mtok=price_out,
            )

        return E.build_snapshot_from_tokens(
            month=E.month_key(now, tz_offset_minutes=tz_offset),
            budget=CA.num(sec, "monthly_budget", 0.0),
            spend=_cost(totals.get("month", {})),
            today_spend=_cost(totals.get("today", {})),
            progress=E.month_progress(now, tz_offset_minutes=tz_offset),
            fetched_at=now,
        )

    async def _query_token_totals(
        self, now: float, tz_offset: int
    ) -> dict[str, dict[str, float]]:
        """从 ``provider_stats`` 聚合本月与今日的 token 用量。

        AstrBot 只在内部 agent 跑完一轮时写这张表，本插件自己的 ``llm_generate``
        **不在**其中（见 README「经济」一节），所以这里只统计宿主侧用量。
        """
        from sqlalchemy import func, select

        from astrbot.core.db.po import ProviderStat

        local_now = S.local_datetime(now, tz_offset)
        month_start_local = local_now.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        today_start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        # 库里存的是**不带时区**的 UTC 时间，所以这里也要去掉 tzinfo 再比较
        month_start = (month_start_local - timedelta(minutes=tz_offset)).replace(tzinfo=None)
        today_start = (today_start_local - timedelta(minutes=tz_offset)).replace(tzinfo=None)

        columns = (
            func.sum(ProviderStat.token_input_other),
            func.sum(ProviderStat.token_input_cached),
            func.sum(ProviderStat.token_output),
        )

        def _row_to_dict(row: Any) -> dict[str, float]:
            return {
                "input_other": float(row[0] or 0.0),
                "input_cached": float(row[1] or 0.0),
                "output": float(row[2] or 0.0),
            }

        db = self.context.get_db()
        async with db.get_db() as session:
            # AstrBot 的 ``get_db()`` 是 **async generator**（``async with`` 拿到的才是
            # AsyncSession），而 ``AsyncSession.execute`` 是协程。两者都要 await，
            # 少一个 await 就会拿到 ChunkedIteratorResult / 直接 AttributeError。
            month_row = (
                await session.execute(
                    select(*columns)
                    .where(ProviderStat.created_at >= month_start)
                    .where(ProviderStat.agent_type == "internal"),
                )
            ).one()
            today_row = (
                await session.execute(
                    select(*columns)
                    .where(ProviderStat.created_at >= today_start)
                    .where(ProviderStat.agent_type == "internal"),
                )
            ).one()
        return {"month": _row_to_dict(month_row), "today": _row_to_dict(today_row)}

    def _economy_hint(self, now: float) -> str:
        """手头紧时给活动提示词的一段约束；不需要时返回空串。"""
        sec = CA.section(self.conf, "economy")
        if not CA.flag(sec, "enabled", False) or not CA.flag(sec, "hint_in_prompt", True):
            return ""
        snapshot = self._economy_snapshot
        if snapshot is None or not snapshot.ok:
            return ""
        tier = E.economy_tier(
            snapshot,
            self.economy_thresholds,
            now=now,
            stale_after_seconds=CA.num(sec, "stale_after_minutes", 60) * 60,
        )
        return E.frugal_hint(snapshot, tier)

    # ------------------------------------------------------------ 长期记忆联动

    def _memory_config(self) -> tuple[bool, bool]:
        """读记忆联动的两个开关：``(记录活动, 记录群内事件)``。"""
        sec = CA.section(self.conf, "memory")
        if not CA.flag(sec, "enabled", False):
            return False, False
        return (
            CA.flag(sec, "record_activity", True),
            CA.flag(sec, "record_group_events", False),
        )

    def _memory_engine(self):
        """拿 LivingMemory 的引擎；没装/没就绪就返回 ``None``。

        每次现查而不是缓存：用户可能中途装卸插件、或者对方重载后换了实例，
        缓存会拿到已经 close 的引擎。
        """
        try:
            return M.find_livingmemory_engine(self.context)
        except Exception:  # noqa: BLE001 —— 联动失败绝不影响主流程
            # 用 warning 而不是 debug：这里出异常说明**接线错了**（比如宿主改了这个
            # 属性名），静默降级会让人以为「联动开了但没效果」却查不到原因。
            logger.warning(
                f"[{PLUGIN_NAME}] 查找长期记忆插件失败，本次跳过记忆写入", exc_info=True
            )
            return None

    async def _note_activity_memory(self, now: float, previous_activity: str) -> None:
        """活动变了就写一条记忆（活动是全局的，归属 ``LIFE_SCOPE``）。

        Args:
            now: 当前时间戳。
            previous_activity: 本轮结算前的活动 id，用来判断是否真的换了。
        """
        record_activity, _ = self._memory_config()
        if not record_activity:
            return
        if previous_activity == self.state.activity:
            return
        engine = self._memory_engine()
        if engine is None:
            return

        sim = self.sim_config
        entry = M.activity_memory(
            bot_name=self._bot_name(),
            activity_label=R.activity_label(self.state.activity),
            previous_label=R.activity_label(previous_activity),
            mood_word=M.mood_word(float(self.state.emotion)),
            energy_word=M.energy_word(float(self.state.energy)),
            health_word=S.health_label(self.state, now, sim),
            scene_text=str(getattr(self.state, "scene", "") or ""),
            now=now,
            tz_offset_minutes=sim.tz_offset_minutes,
        )
        if await M.write_entry(engine, entry):
            logger.debug(f"[{PLUGIN_NAME}] 已写入活动记忆：{entry.content.splitlines()[0]}")
        else:
            logger.debug(f"[{PLUGIN_NAME}] 写入活动记忆失败（LivingMemory 可能正忙）")

    async def _note_group_memory(
        self, event: AstrMessageEvent, now: float, reply_text: str = ""
    ) -> None:
        """群里发生的事写进**该群自己的**记忆（与对话记忆同一条召回路径）。

        Args:
            event: 消息事件。
            now: 当前时间戳。
            reply_text: 她的回复（可为空）。
        """
        _, record_events = self._memory_config()
        if not record_events:
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        # 只记群聊：私聊的记忆交给 LivingMemory 自己的对话总结更合适
        if not umo or not self._umo_is_group(umo):
            return
        engine = self._memory_engine()
        if engine is None:
            return

        entry = M.group_event_memory(
            bot_name=self._bot_name(),
            umo=umo,
            sender_name=str(getattr(event, "sender_name", "") or ""),
            user_text=str(getattr(event, "message_str", "") or ""),
            reply_text=reply_text,
            activity_label=R.activity_label(self.state.activity),
            now=now,
            tz_offset_minutes=self.sim_config.tz_offset_minutes,
        )
        if not await M.write_entry(engine, entry):
            logger.debug(f"[{PLUGIN_NAME}] 写入群内记忆失败")

    # ------------------------------------------------------------ 主动开口

    async def _maybe_proactive(self, now: float) -> None:
        """按评分与硬闸决定是否主动开一次话。

        群聊与私聊是**两个独立开关**（``proactive.enabled`` / ``enabled_private``），
        具体某个会话能不能开口由 ``P.decide`` 里的 ``enabled_for`` 判定。
        """
        config = self.proactive_config
        if self.dry_run or not config.any_enabled():
            return
        sec = CA.section(self.conf, "proactive")
        scope_mode, targets = CA.scope_config(
            sec, warn=self._warn, label="主动开口（proactive）"
        )
        local_now = S.local_datetime(now, self.sim_config.tz_offset_minutes)
        now_minutes = local_now.hour * 60 + local_now.minute
        day_key = S.day_key_of(local_now, self.sim_config.day_boundary_hour)

        for umo in list(self._sessions()):
            is_group = self._umo_is_group(umo)
            # 群聊关掉就完全不在群里开口；私聊同理。
            if not self._enabled_for(umo):
                continue
            if not CA.scope_matches(
                mode=scope_mode,
                targets=targets,
                umo=umo,
                group_id=self._group_id_of(umo),
                user_id=self._user_id_of(umo),
                is_group=is_group,
            ):
                continue
            key, state = self._registry_for_umo(umo, user_id=self._user_id_of(umo))
            materials = S.active_materials(
                state,
                now,
                floor=CA.num(CA.section(self.conf, "events"), "material_decay_floor", 0.25),
            )
            decision = P.decide(
                config=config,
                now=now,
                now_minutes=now_minutes,
                activity=state.activity,
                energy=float(state.energy),
                emotion=float(state.emotion),
                materials=materials,
                session=self._sessions().get(umo),
                day_key=day_key,
                private_chat=not is_group,
            )
            if not decision.should_send:
                P.bump_skip_ledger(state.skip_ledger, decision.reason_label)
                continue
            with self._active_state(state, key):
                sent = await self._send_proactive(umo, decision, now)
            if sent:
                P.record_proactive(
                    self._sessions(), stream_id=umo, now=now, day_key=day_key
                )

    def _sessions(self) -> dict[str, dict[str, Any]]:
        """会话记录表（与状态共用，保证一起落盘）。"""
        sessions = self.state.sessions
        if not isinstance(sessions, dict):
            sessions = {}
            self.state.sessions = sessions
        return sessions

    async def _send_proactive(
        self, umo: str, decision: P.ProactiveDecision, now: float
    ) -> bool:
        """把「想说的话」交给模型润色后主动发出去；失败只记日志。"""
        ok, reason = await self._send_proactive_detailed(umo, decision, now)
        if not ok:
            logger.warning(f"[{PLUGIN_NAME}] 主动开口未发出（{umo}）：{reason}")
        return ok

    async def _conversation_context(self, umo: str, now: float) -> CX.ContextBundle:
        """取本会话最近的对话，供主动开口「接着话头说」。

        分三段拿：

        1. 拿 ``summary_turns`` 轮对话交给**总结模型**压成几句话；
        2. 拿最近 ``context_messages`` 条原话当参照；
        3. 读 ``last_proactive_at`` / ``last_user_message_at`` 算出隔了多久。

        用 AstrBot 自己的 ``get_human_readable_context``：它已经处理了 ``history``
        的 JSON 解析、role 区分与工具调用占位，没必要自己再解析一遍。

        ⚠ 它的分页是**从最早开始**的（page=1 最旧），所以要拿最近的必须
        先取总页数再翻到最后一页。

        任何一步取不到都返回可用的部分——**没有上下文也能开口**，只是会呆一点，
        不能因为读不到历史就干脆不开这个口。
        """
        sec = CA.section(self.conf, "proactive")
        limit = CA.integer(sec, "context_messages", 10)
        turns = CA.integer(sec, "summary_turns", 25)
        gap_text = self._proactive_gap_text(umo, now)

        def _bundle(lines=None, summary="", reason="") -> CX.ContextBundle:
            return CX.ContextBundle(
                lines=list(lines or []), summary=summary, gap_text=gap_text,
                empty_reason=reason,
            )

        if limit <= 0 and turns <= 0:
            return _bundle(reason="上下文已关闭")

        try:
            manager = getattr(self.context, "conversation_manager", None)
            if manager is None:
                return _bundle(reason="拿不到 conversation_manager")
            cid = await manager.get_curr_conversation_id(umo)
            if not cid:
                return _bundle(reason="这个会话还没有建立对话记录")

            # 一次多取一些：总结要的范围比「最近几条」大
            want_lines = max(limit, turns * 2, 1)
            lines = await self._tail_context(manager, umo, cid, want_lines)
        except Exception:  # noqa: BLE001 —— 读不到历史也要能开口
            logger.debug(f"[{PLUGIN_NAME}] 读取对话上下文失败（{umo}）", exc_info=True)
            return _bundle(reason="读取对话记录失败")

        recent = CX.build_bundle(lines, limit=limit) if limit > 0 else CX.ContextBundle()
        summary, block = await self._summarize_context(umo, lines, turns)
        if block:
            # 配置要求「没总结就不发」——这是唯一会主动放弃开口的上下文原因
            return CX.ContextBundle(
                gap_text=gap_text,
                empty_reason="总结对话失败（summary_on_fail=block）",
                abort=True,
            )

        return _bundle(
            lines=recent.lines,
            summary=summary,
            reason="" if (recent.lines or summary or gap_text) else recent.empty_reason,
        )

    async def _tail_context(
        self, manager: Any, umo: str, cid: str, count: int
    ) -> list[str]:
        """取这个会话**最近** ``count`` 条，按时间正序返回。

        ``get_human_readable_context`` 的 page 从最早算起，所以要先把总页数
        试出来再翻到最后一页。页数变了（期间有人说话）就重试一次。
        """
        page_size = max(1, int(count))
        for _ in range(2):
            lines, pages = await manager.get_human_readable_context(
                umo, cid, page=1, page_size=page_size
            )
            pages = max(1, int(pages or 1))
            last, pages_now = await manager.get_human_readable_context(
                umo, cid, page=pages, page_size=page_size
            )
            if pages_now == pages:
                return last
            # 期间新增了页，用新的页数再来一次
        return last

    async def _summarize_context(
        self, umo: str, lines: list[str], turns: int
    ) -> tuple[str, bool]:
        """用小模型把最近 ``turns`` 轮对话压成几句话。

        Returns:
            ``(摘要, 是否应该因此放弃这次开口)``。第二个值为 True 只在
            ``summary_on_fail=block`` 且总结失败时出现。没配总结模型、没有历史、
            或 ``turns<=0`` 都直接返回 ``("", False)``——那不是失败，是没要求。
        """
        sec = CA.section(self.conf, "proactive")
        summary_provider = CA.text(sec, "summary_provider_id")
        if not summary_provider:
            return "", False
        if not lines or turns <= 0:
            return "", False

        # 一轮≈一问一答两条
        window = lines[-max(1, turns * 2) :]
        on_fail = CA.text(sec, "summary_on_fail", "skip") or "skip"
        prompt = (
            "把下面这段群聊压缩成 1~3 句中文，说明「他们最近在聊什么、有没有人需要被关心」。\n"
            "只输出那几句话，不要开场白、不要列表、不要引号。\n\n"
            + "\n".join(window)
        )
        try:
            response = await self.context.llm_generate(
                chat_provider_id=summary_provider,
                prompt=prompt,
                temperature=0.3,
                max_tokens=300,
            )
            summary = CX.clean_summary(
                str(getattr(response, "completion_text", "") or "")
            )
            if summary:
                return summary, False
            # 返回空也算失败（比如被内容审核拦了）
            return "", on_fail == "block"
        except Exception:  # noqa: BLE001
            logger.debug(f"[{PLUGIN_NAME}] 总结对话失败（{umo}）", exc_info=True)
            return "", on_fail == "block"

    def _proactive_gap_text(self, umo: str, now: float) -> str:
        """距上次主动搭话多久。

        取「上次真的发出去」与「上次想开口」两者中**较晚**的那个——被拦下的
        那次也算她惦记过，不该让她把十分钟说成十天。
        """
        try:
            record = self._sessions().get(umo) or {}
            if not isinstance(record, dict):
                return ""
            last_sent = record.get("last_proactive_at") or 0.0
            last_user = record.get("last_user_message_at") or 0.0
            try:
                mark = max(float(last_sent), float(last_user))
            except (TypeError, ValueError):
                return ""
            if mark <= 0:
                return ""
            return CX.format_gap(float(now) - mark)
        except Exception:  # noqa: BLE001
            return ""

    def _proactive_prompt(
        self,
        *,
        material_text: str,
        decision: P.ProactiveDecision,
        bundle: CX.ContextBundle,
        now: float,
    ) -> str:
        """拼主动开口的提示词：优先用用户模板，留空则用内置默认模板。

        模板里的 ``{占位符}`` 见 ``core/life_prompt.PLACEHOLDER_HELP``。
        未知占位符原样保留（不会抛异常），并在首次使用时记一条警告。
        """
        sec = CA.section(self.conf, "proactive")
        template = CA.text(sec, "prompt_template", "").strip()
        if not template:
            template = PL.DEFAULT_TEMPLATE

        unknown = PL.unknown_placeholders(template, PL.PLACEHOLDER_HELP)
        if unknown:
            self._warn(
                f"proactive_template:{','.join(unknown)}",
                f"主动开口模板里有不认识的占位符：{', '.join(unknown)}"
                f"（可用：{', '.join(PL.PLACEHOLDER_HELP)}）",
            )

        local = S.local_datetime(now, self.sim_config.tz_offset_minutes)
        context_block = ""
        if bundle.available:
            context_block = f"{bundle.as_prompt_block()}\n"

        values = {
            "material": material_text,
            "intent": decision.intent or "主动关心一下",
            "context": context_block,
            "activity": R.activity_label(self.state.activity),
            "emotion": f"{float(self.state.emotion):.1f}/10",
            "energy": f"{float(self.state.energy):.1f}/10",
            "mood": M.mood_word(float(self.state.emotion)),
            "energy_word": M.energy_word(float(self.state.energy)),
            "health": S.health_label(self.state, now, self.sim_config),
            "time": f"{local.hour:02d}:{local.minute:02d}",
            "date": f"{local.month}月{local.day}日",
            "gap": getattr(bundle, "gap_text", "") or "",
            "persona": self._persona_text(),
            "name": self._bot_name(),
        }
        return PL.render(template, values)

    async def _send_proactive_detailed(
        self, umo: str, decision: P.ProactiveDecision, now: float
    ) -> tuple[bool, str]:
        """同 ``_send_proactive``，但把「为什么没发出去」也返回。

        自动触发路径只关心成败，手动测试需要知道原因——否则用户只能看到一句
        「发送失败」，然后无从下手。返回的第二个值是一条给用户看的中文说明。
        """
        material = decision.material or {}
        text = str(material.get("text", "") or "").strip()
        if not text:
            return False, "这条素材没有正文"

        bundle = await self._conversation_context(umo, now)
        if bundle.abort:
            return False, bundle.empty_reason or "上下文不可用，按配置放弃这次开口"
        prompt = self._proactive_prompt(
            material_text=text, decision=decision, bundle=bundle, now=now
        )
        try:
            provider_id = self._default_provider_id("proactive", umo)
            if not provider_id:
                return False, (
                    "没有可用的模型：请在「主动开口 → 主动开口模型」里选一个，"
                    "或确认 AstrBot 里有已启用的模型"
                )
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=self._life_system_prompt(now),
                temperature=0.9,
                max_tokens=200,
            )
            content = str(getattr(response, "completion_text", "") or "").strip()
            if not content:
                return False, "模型返回了空内容（可以看看是不是被内容审核拦了）"
            await self.context.send_message(
                umo, MessageChain(chain=[Comp.Plain(content)])
            )
            logger.info(f"[{PLUGIN_NAME}] 主动开口 → {umo}")
            return True, ""
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[{PLUGIN_NAME}] 主动开口失败（{umo}）")
            return False, f"{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------ 闸门

    @filter.event_message_type(filter.EventMessageType.ALL, priority=GATE_PRIORITY)
    async def life_gate(self, event: AstrMessageEvent):
        """群聊纯闲聊的回复闸门：放行、拦截，或与闸门无关。

        Args:
            event: 消息事件。
        """
        if not self._enabled_for(self._safe_umo(event)) or not self._config_ok:
            return
        if event.is_stopped():
            return
        now = _now()
        # 私聊要用**这个人自己的**那份状态算倍率；群聊仍是共用的那份。
        key, state = self._registry_for_umo(
            self._safe_umo(event), user_id=self._sender_of(event)
        )
        with self._active_state(state, key):
            outcome = self._gate_outcome(event, now)
            if outcome is None:
                return
            if outcome.allow:
                self._note_user_message(event, now)
                return
        event.stop_event()
        yield

    def _gate_outcome(self, event: AstrMessageEvent, now: float) -> _GateOutcome | None:
        """算一次闸门结论；``None`` = 这条消息与闸门无关（放行、不计数）。"""
        sec = CA.section(self.conf, "gate")
        if self.dry_run or not CA.flag(sec, "enabled", True):
            return None

        if event.is_private_chat() or event.is_at_or_wake_command:
            # 私聊、@ 与唤醒前缀永不拦；但都算「对方刚说过话」
            return _GateOutcome(allow=True, register=False)

        if self._other_handler_acts(event):
            # 有别的插件 handler 要吃这条消息（指令、正则触发……）：不许拦
            return _GateOutcome(allow=True, register=False)

        umo = str(getattr(event, "unified_msg_origin", "") or "")
        # 走到这里一定是群聊（私聊已在上面放行），但仍从 event 取真实类型与群号，
        # 而不是用 umo 反推：get_group_id() 才是平台给的权威值。
        mode, targets = CA.scope_config(
            sec, warn=self._warn, label="回复闸门（gate）"
        )
        if not CA.scope_matches(
            mode=mode,
            targets=targets,
            umo=umo,
            group_id=str(event.get_group_id() or ""),
            user_id=str(event.get_sender_id() or ""),
            is_group=self._umo_is_group(umo),
        ):
            return None

        mode = CA.text(sec, "mode", "count")
        multiplier = self._current_multiplier(now)
        gate = self._gates.setdefault(umo, G.SessionGate())
        result = G.evaluate(
            gate,
            now=now,
            mode=mode,
            multiplier=multiplier,
            base_talk_value=CA.num(sec, "base_talk_value", 1.0),
            base_probability=CA.num(sec, "base_probability", 0.3),
            min_reply_interval_seconds=CA.num(sec, "min_reply_interval_seconds", 60),
            rng=self.rng,
        )
        if not result.reply:
            P.bump_skip_ledger(
                self.state.skip_ledger, f"{LEDGER_GATE_PREFIX}{result.reason or 'pending'}"
            )
        return _GateOutcome(allow=result.reply, register=True)

    def _other_handler_acts(self, event: AstrMessageEvent) -> bool:
        """有没有**别的**插件 handler 会对这条消息动手（有就别拦）。"""
        handlers = event.get_extra("activated_handlers", None)
        if not handlers:
            return False
        own = __name__
        return any(
            getattr(handler, "handler_module_path", "") != own for handler in handlers
        )

    def _note_user_message(self, event: AstrMessageEvent, now: float) -> None:
        """记下「对方最近一次说话」，供主动开口的静默硬闸使用。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return
        day_key = S.day_key_of(
            S.local_datetime(now, self.sim_config.tz_offset_minutes),
            self.sim_config.day_boundary_hour,
        )
        P.record_user_message(self._sessions(), stream_id=umo, now=now, day_key=day_key)

    def _ledger(self) -> dict[str, int]:
        """沉默台账 → 可读版：把 ``gate_xxx`` 换成中文短语。

        ``life_proactive.SKIP_REASON_LABELS`` 只认识主动开口那套原因键；闸门这套
        由本文件补，否则 ``/生活 为什么`` 会直接把 ``gate_pending`` 印给用户看。
        """
        raw = self.state.skip_ledger if isinstance(self.state.skip_ledger, dict) else {}
        readable: dict[str, int] = {}
        for key, value in raw.items():
            name = str(key)
            if name.startswith(LEDGER_GATE_PREFIX):
                reason = name[len(LEDGER_GATE_PREFIX):]
                label = _GATE_REASON_LABELS.get(reason, reason)
                name = f"闸门：{label}"
            try:
                readable[name] = readable.get(name, 0) + int(value)
            except (TypeError, ValueError):
                continue
        return readable

    def _current_multiplier(self, now: float) -> float:
        """当前倍率（暂停 → 1.0；睡眠/静默时段 → 静默下限）。"""
        if self.state.paused_override:
            return F.hold_baseline().adjust
        return float(self._breakdown(now).adjust)

    def _breakdown(self, now: float) -> F.AdjustBreakdown:
        """当前倍率的逐项拆解（暂停时返回 1.0 的持有值）。"""
        if self.state.paused_override:
            return F.hold_baseline()
        local_now = S.local_datetime(now, self.sim_config.tz_offset_minutes)
        activity = self.state.activity
        if activity == S.SLEEP and float(self.state.at_wake_until or 0.0) > now:
            # 被 @ 唤醒的窗口里她其实还睡着，倍率按清醒活动算（见 README「被 @ 唤醒」）
            activity = A.DAILY
        return F.compute_adjust(
            activity=activity,
            emotion=float(self.state.emotion),
            energy=float(self.state.energy),
            sick=S.is_cold(self.state, now),
            sleep_debt_nights=int(self.state.sleep_debt_nights),
            date_factor=S.date_factor(self.state, now, self.sim_config),
            material_count=S.material_effective_count(self.state, now),
            now_minutes=local_now.hour * 60 + local_now.minute,
            config=self.factor_config,
            mode="frequency",
        )

    def _wake_note(self, now: float) -> str:
        """被 @ 唤醒的窗口到几点（不在窗口里返回空串）。"""
        until = float(self.state.at_wake_until or 0.0)
        if until <= now or self.state.activity != S.SLEEP:
            return ""
        return S.local_datetime(until, self.sim_config.tz_offset_minutes).strftime("%H:%M")

    def _note_at_wake(self, event: AstrMessageEvent, now: float) -> None:
        """睡眠中被 @ 时开一个清醒窗口，让她这一次真的能回话。

        为什么要这么做：她自己还在睡（倍率 0），可对方已经 @ 了她。原版在
        ``_wake_active`` 里判断「窗口内按清醒算」，本插件同样把窗口写进状态，
        于是这次 @ 用清醒的倍率过闸门；窗口一过，只要活动仍是 ``sleep``
        就自动回到静默，不需要额外清理（见 ``LifeState.at_wake_until``）。
        """
        sim = self.sim_config
        if not sim.wake_on_at or self.state.activity != S.SLEEP:
            return
        if event.is_private_chat() or not event.is_at_or_wake_command:
            return
        # 至少留 1 分钟：配成 0 时若真写成 0，这个功能就会静默失效
        minutes = max(1, int(sim.wake_minutes))
        self.state.at_wake_until = now + minutes * 60

    # ------------------------------------------------------------ 指令

    @filter.command_group("生活", alias=LIFE_COMMAND_ALIASES)
    def life(self):
        """生活频率指令组。"""
        pass

    @life.command("状态")
    async def life_status(self, event: AstrMessageEvent):
        """看一下她此刻在做什么、为什么是这个倍率。

        Args:
            event: 消息事件。
        """
        yield event.plain_result(self._render(R.status_card, event))

    @life.command("频率")
    async def life_frequency(self, event: AstrMessageEvent):
        """逐项拆解当前倍率，以及它在闸门下的真实后果。

        Args:
            event: 消息事件。
        """
        yield event.plain_result(self._render(R.frequency_card, event))

    @life.command("活动")
    async def life_activity(self, event: AstrMessageEvent):
        """当前活动、来源、停留期与模型健康状况。

        Args:
            event: 消息事件。
        """
        yield event.plain_result(self._render(R.activity_card, event))

    @life.command("为什么", alias={"why"})
    async def life_why(self, event: AstrMessageEvent):
        """她为什么没说话：沉默台账。

        Args:
            event: 消息事件。
        """
        try:
            yield event.plain_result(R.why_card(self._ledger(), limit=10))
        except Exception:  # noqa: BLE001
            logger.exception(f"[{PLUGIN_NAME}] 渲染沉默台账失败")
            yield event.plain_result("沉默台账渲染失败，详情见日志。")

    @life.command("暂停")
    async def life_pause(self, event: AstrMessageEvent):
        """暂停：不拦截、不主动开口，她按正常频率回复。

        Args:
            event: 消息事件。
        """
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能暂停她的生活频率。")
            return
        self.state.paused_override = True
        self._save_state()
        logger.info(f"[{PLUGIN_NAME}] 已暂停（{self._safe_umo(event)}）")
        yield event.plain_result(
            "已暂停：不拦截、不主动开口，她按正常频率回复。恢复用 /生活 恢复。",
        )

    @life.command("恢复")
    async def life_resume(self, event: AstrMessageEvent):
        """恢复被暂停的生活频率。

        Args:
            event: 消息事件。
        """
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能恢复她的生活频率。")
            return
        self.state.paused_override = False
        self._save_state()
        logger.info(f"[{PLUGIN_NAME}] 已恢复（{self._safe_umo(event)}）")
        yield event.plain_result("已恢复：闸门重新按她的生活状态生效。")

    @life.command("重置")
    async def life_reset(self, event: AstrMessageEvent):
        """重置**当前会话**的生活状态（活动重新按内置时段表取种子）。

        群聊里重置的是群聊共用的那份；私聊里只重置你自己的那份，
        不会影响别人，也不会影响群聊。

        Args:
            event: 消息事件。
        """
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能重置她的生活状态。")
            return
        umo = self._safe_umo(event)
        key, _ = self._registry_for_umo(umo, user_id=self._sender_of(event))
        fresh = S.new_state(now=_now(), config=self.sim_config)
        self._registry.put(key, fresh)
        self._gates.pop(umo, None)
        self._save_state()
        scope = "群聊共用" if key == REG.GROUP_STATE_KEY else f"私聊 {key}"
        logger.info(f"[{PLUGIN_NAME}] 状态已重置（{scope}｜{umo}）")
        yield event.plain_result(
            f"已重置{scope}的那份：当前活动 {R.activity_label(fresh.activity)}，"
            f"情绪 {fresh.emotion:.1f}/10，体力 {fresh.energy:.1f}/10。",
        )

    @life.command("测试主动")
    async def life_test_proactive(self, event: AstrMessageEvent):
        """人为触发一次主动开口，用来验证配置是否有效。

        与自动触发的唯一区别是**跳过判定**：不看时段、不看配额、不看评分，
        直接按真实状态、真实素材、真实人设走同一条发送链路。因此能准确回答
        「配置通不通」这个问题，也不改动任何状态（不记次数、不推进生活）。

        Args:
            event: 消息事件。
        """
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能触发主动开口测试。")
            return

        now = _now()
        umo = self._safe_umo(event)
        if not umo:
            yield event.plain_result("拿不到当前会话标识，无法测试。")
            return

        config = self.proactive_config
        # 用副本算，避免任何写回
        session = dict(self._sessions().get(umo) or {})
        materials = S.active_materials(
            self.state,
            now,
            floor=CA.num(CA.section(self.conf, "events"), "material_decay_floor", 0.25),
        )
        local_now = S.local_datetime(now, self.sim_config.tz_offset_minutes)
        day_key = S.day_key_of(local_now, self.sim_config.day_boundary_hour)

        decision = P.decide(
            config=config,
            now=now,
            now_minutes=local_now.hour * 60 + local_now.minute,
            activity=self.state.activity,
            energy=float(self.state.energy),
            emotion=float(self.state.emotion),
            materials=materials,
            session=session,
            day_key=day_key,
            private_chat=event.is_private_chat(),
        )

        # 判定结果照常算出来给用户看，但**不拿它当门槛**：测试的目的就是
        # 「在它本来不会开口的时候，看看配置到底长什么样」。
        #
        # ⚠ 素材必须由这里自己选：``decide`` 一旦在早期闸（总开关、睡觉）短路，
        # ``decision.material`` 就是 ``None``。若直接沿用，总开关关闭时就永远
        # 测不出东西——而那恰恰是最该测的情况。
        material = decision.material or P.select_material(
            materials, now, floor=config.material_decay_floor
        )
        forced = P.ProactiveDecision(
            True,
            decision.reason,
            score=decision.score,
            material=material,
            intent=decision.intent or "主动关心一下",
            detail=decision.detail,
        )
        header = self._test_decision_header(config, decision, materials)

        if forced.material is None:
            # 连素材都没有：没有任何话可说，如实告知而不是编一句
            yield event.plain_result(
                header + "\n\n❌ 现在一条没过期的素材都没有，没法生成内容。\n"
                "请检查「随机事件与素材」的有效期与衰减设置。"
            )
            return

        ok, fail_reason = await self._send_proactive_detailed(umo, forced, now)
        if ok:
            logger.info(f"[{PLUGIN_NAME}] 手动触发主动开口成功（{umo}）")
            yield event.plain_result(header + "\n\n✅ 已按自动触发的方式发出，请查看上面的消息。")
        else:
            yield event.plain_result(
                header + f"\n\n❌ 发送失败：{fail_reason}\n（详情见日志）"
            )

    def _test_decision_header(
        self,
        config: P.ProactiveConfig,
        decision: P.ProactiveDecision,
        materials: Sequence[Mapping[str, Any]],
    ) -> str:
        """测试消息的抬头：说清楚「自动触发的话会不会开口」，以及为什么。"""

        if decision.should_send:
            auto = "自动触发的话，现在**会**开口。"
        else:
            auto = f"自动触发的话，现在**不会**开口（{decision.reason_label}）。"
        lines = [
            "🧪 主动开口测试（手动触发，已跳过判定）",
            "",
            auto,
        ]
        scope_name = "群聊" if self._umo_is_group(umo) else "私聊"
        if not config.enabled_for(not self._umo_is_group(umo)):
            lines.append(
                f"⚠ {scope_name}的主动开口开关是关的——真实情况下这里永远不会自动开口。"
            )
        if self.dry_run:
            lines.append("⚠ 当前是演算模式（dry_run），自动触发也不会真的发。")
        if decision.reason_label and not decision.should_send:
            if decision.detail:
                lines.append(f"　原因：{decision.reason_label}（{decision.detail}）")
            hint = PT.FIX_HINTS.get(decision.reason, "")
            if hint:
                lines.append(f"　怎么办：{hint}")
        lines.append(f"　可用素材：{len(materials)} 条；评分 {decision.score:.3f} / 阈值 {config.score_threshold:.3f}")
        return "\n".join(lines)

    def _scope_hit_count(self, sec: Mapping[str, Any]) -> int:
        """统计**实际命中**了 ``target_chats`` 的会话数（给状态卡用）。

        AstrBot 没有「列出全部会话」的公开接口，所以只能用插件自己见过的会话
        （``state.sessions``，即发过消息/主动开过口的那些）当样本。它是下界而不是
        精确值，但足以回答「名单到底有没有填对」这个问题——这正是它存在的意义。
        """
        mode, targets = CA.scope_config(sec, warn=self._warn, label="回复闸门（gate）")
        if mode == "all":
            return 0
        hits = 0
        for umo in self._sessions():
            is_group = self._umo_is_group(umo)
            if CA.scope_matches(
                mode=mode,
                targets=targets,
                umo=umo,
                group_id=self._group_id_of(umo),
                user_id=self._user_id_of(umo),
                is_group=is_group,
            ):
                hits += 1
        return hits

    def _render(self, renderer, event: AstrMessageEvent) -> str:
        """渲染一张状态卡；任何异常都变成一句人话，而不是抛给用户。

        卡片展示的是**这个会话看到的她**：私聊是各自的，群聊是共用的。
        """
        try:
            key, state = self._registry_for_umo(
                self._safe_umo(event), user_id=self._sender_of(event)
            )
            with self._active_state(state, key):
                return renderer(self._card_context(_now(), umo=self._safe_umo(event)))
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[{PLUGIN_NAME}] 渲染卡片失败")
            return f"卡片渲染失败：{type(exc).__name__}: {exc}"

    def _card_context(self, now: float, *, umo: str = "") -> R.CardContext:
        """把当前状态打包成 ``render`` 需要的上下文。"""
        gate_sec = CA.section(self.conf, "gate")
        economy_sec = CA.section(self.conf, "economy")
        events_sec = CA.section(self.conf, "events")
        freq_sec = CA.section(self.conf, "frequency")
        proactive_sec = CA.section(self.conf, "proactive")
        llm_sec = CA.section(self.conf, "activity_llm")
        return R.CardContext(
            state=self.state,
            now=now,
            sim_config=self.sim_config,
            breakdown=self._breakdown(now),
            schedule=A.schedule_facts(
                S.local_datetime(now, self.sim_config.tz_offset_minutes),
                self.sim_config.schedule,
            ),
            economy=self._economy_snapshot,
            economy_enabled=CA.flag(economy_sec, "enabled", False),
            economy_stale_seconds=CA.num(economy_sec, "stale_after_minutes", 60) * 60,
            gate_enabled=CA.flag(gate_sec, "enabled", True),
            gate_mode=CA.text(gate_sec, "mode", "count"),
            base_talk_value=CA.num(gate_sec, "base_talk_value", 1.0),
            base_probability=CA.num(gate_sec, "base_probability", 0.3),
            min_reply_interval_seconds=CA.num(gate_sec, "min_reply_interval_seconds", 60),
            min_adjust=CA.num(freq_sec, "min_adjust", 0.0),
            scope_mode=CA.filter_mode(gate_sec),
            target_count=self._scope_hit_count(gate_sec),
            dry_run=self.dry_run,
            proactive_enabled=self.proactive_config.enabled_for(
                bool(umo) and not self._umo_is_group(umo)
            ),
            proactive_scope=CA.scope_label(proactive_sec),
            material_ttl_hours=CA.num(events_sec, "material_ttl_hours", 72.0),
            activity_mode=CA.text(CA.section(self.conf, "activity"), "mode", "llm"),
            persona_preview=self._persona_text(),
            persona_limit=CA.integer(llm_sec, "persona_max_chars", 600),
            wake_note=self._wake_note(now),
            memory_status=self._memory_status(),
        )

    def _memory_status(self) -> str:
        """一行长期记忆联动状态；未启用时返回空串（卡片不显示）。"""
        record_activity, record_events = self._memory_config()
        if not record_activity and not record_events:
            return ""
        if self._memory_engine() is None:
            return "已开启，但没找到 LivingMemory（未安装、未启用或还没初始化完）"
        parts = []
        if record_activity:
            parts.append("活动")
        if record_events:
            parts.append("群内对话")
        return f"已连接 LivingMemory（记录：{'、'.join(parts)}）"

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """是否管理员：``event.is_admin()`` 为真，或发送者在 ``admins_id`` 里。"""
        try:
            if event.is_admin():
                return True
        except Exception:  # noqa: BLE001
            pass
        sender = str(event.get_sender_id() or "")
        try:
            admins = self.context.get_config().get("admins_id", []) or []
        except Exception:  # noqa: BLE001
            admins = []
        return sender in {str(item) for item in admins}

    @staticmethod
    def _safe_umo(event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "")

    # ------------------------------------------------------------ LLM 接入

    @filter.on_llm_request()
    async def inject_life_context(self, event: AstrMessageEvent, req: Any):
        """把她此刻的生活注入系统提示词，让语气与处境一致。

        Args:
            event: 消息事件。
            req: AstrBot 的 ``ProviderRequest``（原地修改）。
        """
        sec = CA.section(self.conf, "prompt")
        if self.dry_run or not CA.flag(sec, "inject_enabled", True):
            return
        if not self._enabled_for(self._safe_umo(event)):
            return
        now = _now()
        if event.is_at_or_wake_command and not event.is_private_chat():
            self._note_at_wake(event, now)
        # 注入的必须是**这个会话看到的她**：私聊是各自的，群聊是共用的。
        key, state = self._registry_for_umo(
            self._safe_umo(event), user_id=self._sender_of(event)
        )
        with self._active_state(state, key):
            try:
                digest = R.life_digest(self._card_context(now))
            except Exception:  # noqa: BLE001
                logger.exception(f"[{PLUGIN_NAME}] 生成生活摘要失败")
                return
            max_chars = max(0, CA.integer(sec, "max_chars", 600))
            if max_chars and len(digest) > max_chars:
                digest = digest[:max_chars].rstrip()
        req.system_prompt = f"{req.system_prompt or ''}\n\n{digest}\n"

    def _life_system_prompt(self, now: float) -> str:
        """给插件自己发起的模型调用用的系统提示词。"""
        try:
            digest = R.life_digest(self._card_context(now))
        except Exception:  # noqa: BLE001
            digest = f"活动：{R.activity_label(self.state.activity)}"
        persona = self._persona_text()
        parts = [digest]
        if persona:
            parts.append(f"【人设】{persona[:600]}")
        return "\n".join(parts)

    @filter.on_llm_response()
    async def note_life_memory_on_reply(self, event: AstrMessageEvent, resp: Any):
        """她回完话，把这次群里的互动写进该群自己的长期记忆（默认关）。

        ⚠ 这里只读取回复文本、不修改 ``resp``：改它会影响实际发出的内容。

        Args:
            event: 消息事件。
            resp: AstrBot 的 ``LLMResponse``。
        """
        _, record_events = self._memory_config()
        if not record_events or self.dry_run:
            return
        try:
            reply = str(getattr(resp, "completion_text", "") or "").strip()
            if not reply:
                return
            await self._note_group_memory(event, _now(), reply_text=reply)
        except Exception:  # noqa: BLE001 —— 联动失败不许影响回复
            logger.debug(f"[{PLUGIN_NAME}] 写入群内记忆失败", exc_info=True)

    @filter.llm_tool(name="get_life_state")
    async def get_life_state(self, event: AstrMessageEvent, detail: str = "") -> str:
        """查看她自己此刻的生活状态：在做什么、情绪体力如何、为什么回复或不回复。

        ⚠ 这里的 ``Args:`` **只能列业务参数**，绝不要写 ``event``：装饰器在导入期
        解析这个段落来生成工具的 JSON Schema，而 ``AstrMessageEvent`` 不在允许的
        类型表（string/number/object/array/boolean）里，写进去会让整个插件导入失败。
        宿主是按参数名 ``event`` 自动注入事件对象的，不需要在文档里声明。

        Args:
            detail(string): 想看哪一部分；留空=概览，frequency=倍率拆解，why=沉默原因
        """
        key = (detail or "").strip().lower()
        try:
            ctx = self._card_context(_now())
            if key in {"frequency", "频率", "倍率"}:
                return R.frequency_card(ctx)
            if key in {"why", "为什么", "沉默"}:
                return R.why_card(self._ledger(), limit=10)
            return R.status_card(ctx)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[{PLUGIN_NAME}] get_life_state 失败")
            return f"读取生活状态失败：{type(exc).__name__}: {exc}"

    # ------------------------------------------------------------ 小工具

    @staticmethod
    def _umo_parts(umo: str) -> tuple[str, str, str]:
        """拆 ``platform:message_type:session_id``。

        只按前两个冒号切分（``split(":", 2)``），因为 ``session_id`` 本身允许含
        冒号——WebChat 的 umo 就是 ``webchat:FriendMessage:webchat!user!sid``。
        """
        parts = str(umo or "").split(":", 2)
        while len(parts) < 3:
            parts.append("")
        return parts[0], parts[1], parts[2]

    @classmethod
    def _umo_is_group(cls, umo: str) -> bool:
        """判断会话是不是群聊。

        读第 2 段（``MessageType`` 的枚举值，见 ``message_type.py``：
        ``GroupMessage`` / ``FriendMessage`` / ``OtherMessage``），而不是在整条
        umo 里搜子串——只要平台名里带 "group"，子串判断就会误判。
        """
        _, message_type, _ = cls._umo_parts(umo)
        return message_type == "GroupMessage"

    @classmethod
    def _group_id_of(cls, umo: str) -> str:
        """从 umo 反推群号（用于主动开口时没有 event 可用的场合）。"""
        _, message_type, session_id = cls._umo_parts(umo)
        return session_id if message_type == "GroupMessage" else ""

    @classmethod
    def _user_id_of(cls, umo: str) -> str:
        """从 umo 反推私聊对象 id。"""
        _, message_type, session_id = cls._umo_parts(umo)
        return session_id if message_type == "FriendMessage" else ""
