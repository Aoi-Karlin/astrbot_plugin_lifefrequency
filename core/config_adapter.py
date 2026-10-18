"""Map the AstrBot plugin config (a nested dict edited in the WebUI) to the pure
dataclasses used by the simulation modules.

Every reader is defensive: WebUI users can type anything, so non-numeric,
``nan``/``inf`` and out-of-range values fall back to defaults instead of raising.
Problems are reported through a ``warn(key, message)`` callback so the plugin can
log each distinct issue once.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

from .life_activity import (
    ALLOWED_ACTIVITIES,
    ScheduleConfig,
    parse_schedule_window,
    parse_window,
    parse_workdays,
)
from .life_economy import EconomyThresholds
from .life_factors import (
    DEFAULT_HEALTH_FACTORS,
    CurveSet,
    FactorConfig,
    parse_curve_points,
    parse_factor_lines,
)
from .life_proactive import ProactiveConfig
from .life_sim import FestivalRule, SimConfig

Warn = Callable[[str, str], None]

DEFAULT_ACTIVITY_FACTOR_LINES = [
    "sleep=0.0",
    "sick_rest=1.0",
    "before_sleep=1.15",
    "commute=0.85",
    "work=0.75",
    "meeting=0.5",
    "overtime=0.45",
    "lunch=1.1",
    "off_work=1.0",
    "night_study=0.5",
    "music=0.9",
    "game=0.75",
    "anime=0.75",
    "daze=0.6",
    "daily=1.0",
]
ALLOWED_FILTER_MODES = ("all", "whitelist", "blacklist")


def _noop_warn(key: str, message: str) -> None:
    return None


def section(config: Mapping[str, Any] | None, name: str) -> Mapping[str, Any]:
    """Return a config section as a mapping (empty mapping when missing/bad).

    Args:
        config: Whole plugin config.
        name: Section key.

    Returns:
        The section mapping.
    """
    if not isinstance(config, Mapping):
        return {}
    value = config.get(name)
    return value if isinstance(value, Mapping) else {}


def num(sec: Mapping[str, Any], key: str, default: float) -> float:
    """Read a finite float from a section.

    Args:
        sec: Config section.
        key: Field key.
        default: Fallback for missing / invalid values.

    Returns:
        The finite number.
    """
    value = sec.get(key, default)
    if isinstance(value, bool):
        return float(default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def integer(sec: Mapping[str, Any], key: str, default: int) -> int:
    """Read an int (floats are truncated) from a section.

    Args:
        sec: Config section.
        key: Field key.
        default: Fallback.

    Returns:
        The integer.
    """
    return int(num(sec, key, float(default)))


def flag(sec: Mapping[str, Any], key: str, default: bool) -> bool:
    """Read a bool; accepts real bools and common string spellings.

    Args:
        sec: Config section.
        key: Field key.
        default: Fallback.

    Returns:
        The boolean.
    """
    value = sec.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def text(sec: Mapping[str, Any], key: str, default: str = "") -> str:
    """Read a stripped string.

    Args:
        sec: Config section.
        key: Field key.
        default: Fallback.

    Returns:
        The string.
    """
    value = sec.get(key, default)
    return str(value if value is not None else default).strip()


def str_list(sec: Mapping[str, Any], key: str, default: list[str] | None = None) -> list[str]:
    """Read a list of strings; a comma/newline separated string is also accepted.

    Args:
        sec: Config section.
        key: Field key.
        default: Fallback when the key is missing.

    Returns:
        Non-empty stripped strings.
    """
    if key not in sec:
        return list(default or [])
    value = sec.get(key)
    if value is None:
        return []
    if isinstance(value, str):
        normalized = (
            value.replace("，", ",").replace("；", ",").replace(";", ",").replace("\n", ",")
        )
        return [item.strip() for item in normalized.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return list(default or [])


def clamp(value: float, low: float, high: float) -> float:
    """Clamp a number into ``[low, high]``.

    Args:
        value: Number.
        low: Lower bound.
        high: Upper bound.

    Returns:
        The clamped number.
    """
    return max(low, min(high, value))


def quiet_windows(lines: list[str], warn: Warn = _noop_warn) -> tuple[tuple[int, int], ...]:
    """Parse ``HH:MM-HH:MM`` lines into minute windows, skipping bad lines.

    Args:
        lines: Window lines.
        warn: Warning callback.

    Returns:
        Parsed windows.
    """
    windows: list[tuple[int, int]] = []
    for line in lines:
        window = parse_window(line, (-1, -1))
        if window == (-1, -1):
            warn(f"quiet:{line}", f"Quiet window {line!r} cannot be parsed and is skipped")
            continue
        windows.append(window)
    return tuple(windows)


def schedule_config(config: Mapping[str, Any], warn: Warn = _noop_warn) -> ScheduleConfig:
    """``schedule`` section → ``ScheduleConfig``.

    Args:
        config: Whole plugin config.
        warn: Warning callback.

    Returns:
        The schedule config.
    """
    sec = section(config, "schedule")
    workdays, day_warnings = parse_workdays(text(sec, "workdays", "1-5"))
    work_window, window_warnings = parse_schedule_window(
        text(sec, "work_window", "09:30-18:30"), (9 * 60 + 30, 18 * 60 + 30), label="在岗时段"
    )
    lunch_window, lunch_warnings = parse_schedule_window(
        text(sec, "lunch_window", "12:00-13:00"), (12 * 60, 13 * 60), label="午休时段"
    )
    for item in day_warnings + window_warnings + lunch_warnings:
        warn(f"schedule:{item}", f"Schedule warning: {item}")
    return ScheduleConfig(
        enabled=flag(sec, "enabled", False),
        workdays=workdays,
        work_window=work_window,
        commute_minutes=int(clamp(integer(sec, "commute_minutes", 45), 0, 720)),
        lunch_window=lunch_window,
        duty=text(sec, "duty"),
        work_scene=text(sec, "work_scene"),
    )


def sim_config(
    config: Mapping[str, Any],
    *,
    festivals: tuple[FestivalRule, ...] = (),
    date_overrides: dict[str, float] | None = None,
    warn: Warn = _noop_warn,
) -> SimConfig:
    """Whole config → ``SimConfig``.

    Args:
        config: Whole plugin config.
        festivals: Pre-parsed festival rules.
        date_overrides: Name → factor overrides for festivals.
        warn: Warning callback.

    Returns:
        The simulation config.
    """
    sim = section(config, "simulation")
    act = section(config, "activity")
    llm = section(config, "activity_llm")
    emo = section(config, "emotion_energy")
    health = section(config, "health")
    events = section(config, "events")
    date = section(config, "date")

    raw_window = text(sim, "sleep_window", "03:00-11:00") or "03:00-11:00"
    window = parse_window(raw_window, (-1, -1))
    if window == (-1, -1):
        warn(
            "sleep_window",
            f"Sleep window {raw_window!r} cannot be parsed; falling back to 03:00-11:00",
        )
        window, raw_window = (3 * 60, 11 * 60), "03:00-11:00"

    cold_min = max(1, integer(health, "cold_min_days", 1))
    return SimConfig(
        tick_seconds=max(60, integer(sim, "tick_seconds", 600)),
        max_catch_up_hours=max(0.25, num(sim, "catch_up_max_hours", 72.0)),
        offline_gap_minutes=max(0, integer(sim, "offline_gap_minutes", 30)),
        tz_offset_minutes=int(clamp(integer(sim, "tz_offset_minutes", 480), -1440, 1440)),
        day_boundary_hour=int(clamp(integer(sim, "day_boundary_hour", 12), 0, 23)),
        sleep_window=window,
        sleep_window_text=raw_window,
        sleep_energy_threshold=clamp(num(sim, "sleep_energy_threshold", 3.0), 0.0, 10.0),
        max_sleep_hours=max(0.5, num(sim, "max_sleep_hours", 12.0)),
        energy_full_wake=flag(sim, "energy_full_wake", True),
        wake_on_at=flag(sim, "wake_on_at", True),
        wake_minutes=max(0, integer(sim, "wake_minutes", 10)),
        min_awake_hours_per_day=clamp(num(act, "min_awake_hours_per_day", 8.0), 0.0, 24.0),
        min_dwell_minutes=max(0, integer(act, "min_dwell_minutes", 60)),
        min_sleep_minutes=max(0, integer(act, "min_sleep_minutes", 180)),
        schedule=schedule_config(config, warn),
        inertia_minutes=max(0, integer(emo, "inertia_minutes", 40)),
        recover_per_tick=max(0.0, num(emo, "recover_per_tick", 0.2)),
        sleep_recover_multiplier=max(0.0, num(emo, "sleep_recover_multiplier", 2.0)),
        afterglow_span_hours=max(0.0, num(emo, "afterglow_span_hours", 24.0)),
        afterglow_cap=max(0.0, num(emo, "afterglow_cap", 0.6)),
        sleep_debt_threshold_minutes=max(0, integer(health, "sleep_debt_threshold_minutes", 300)),
        sleep_debt_cap_nights=max(1, integer(health, "sleep_debt_cap_nights", 3)),
        sleep_deprived_energy_cap=clamp(num(health, "sleep_deprived_energy_cap", 8.5), 1.0, 10.0),
        cold_check_hour=int(clamp(integer(health, "cold_check_hour", 2), 0, 23)),
        cold_min_days=cold_min,
        cold_max_days=max(cold_min, integer(health, "cold_max_days", 3)),
        cold_base_risk=clamp(num(health, "cold_base_risk", 0.05), 0.0, 1.0),
        cold_sleep_debt_risk=clamp(num(health, "cold_sleep_debt_risk", 0.08), 0.0, 1.0),
        fire_probability=clamp(num(events, "fire_probability", 0.4), 0.0, 1.0),
        material_ttl_hours=max(0.5, num(events, "material_ttl_hours", 6.0)),
        material_best_ratio=clamp(num(events, "material_best_ratio", 0.5), 0.0, 1.0),
        material_decay_floor=clamp(num(events, "material_decay_floor", 0.25), 0.0, 1.0),
        recent_events_keep=max(0, integer(llm, "recent_events_keep", 300)),
        birthday=text(date, "birthday"),
        birthday_factor=clamp(num(date, "birthday_factor", 1.3), 0.2, 5.0),
        birthday_emotion=clamp(num(date, "birthday_emotion", 1.5), -5.0, 5.0),
        birthday_material=text(date, "birthday_material", "今天是我生日"),
        festivals=tuple(festivals),
        date_factor_overrides=dict(date_overrides or {}),
    )


def adjust_bounds(config: Mapping[str, Any], warn: Warn = _noop_warn) -> tuple[float, float, float]:
    """Return ``(min_adjust, max_adjust, silence_floor)`` with the ceiling never below the floors.

    Args:
        config: Whole plugin config.
        warn: Warning callback.

    Returns:
        The three bounds.
    """
    freq = section(config, "frequency")
    min_adjust = max(0.0, num(freq, "min_adjust", 0.0))
    max_adjust = max(0.0, num(freq, "max_adjust", 2.0))
    silence_floor = max(0.0, num(freq, "silence_floor", 0.0))
    ceiling = max(max_adjust, min_adjust, silence_floor)
    if ceiling > max_adjust + 1e-9:
        warn(
            "adjust_bounds",
            f"max_adjust {max_adjust:g} is below min_adjust/silence_floor; raised to {ceiling:g}",
        )
    return min_adjust, ceiling, silence_floor


def factor_config(config: Mapping[str, Any], warn: Warn = _noop_warn) -> FactorConfig:
    """Whole config → ``FactorConfig`` (activity/health factors, curve, quiet hours).

    Args:
        config: Whole plugin config.
        warn: Warning callback.

    Returns:
        The factor pipeline config.
    """
    act = section(config, "activity")
    health = section(config, "health")
    emo = section(config, "emotion_energy")
    freq = section(config, "frequency")

    mode = text(act, "factors_mode", "merge").lower()
    if mode not in ("merge", "replace"):
        warn("factors_mode", f"factors_mode={mode!r} is invalid; using merge")
        mode = "merge"
    lines = str_list(act, "activity_factors", DEFAULT_ACTIVITY_FACTOR_LINES)
    factors, warnings = parse_factor_lines(lines, known_keys=ALLOWED_ACTIVITIES, label="活动")
    for item in warnings:
        warn(f"activity_factor:{item}", f"Activity factor warning: {item}")
    defaults, _ = parse_factor_lines(DEFAULT_ACTIVITY_FACTOR_LINES)
    if mode == "replace":
        if "sleep" not in factors:
            warn(
                "replace_sleep",
                "factors_mode=replace without a sleep factor: she will NOT be silent while asleep",
            )
        table = dict(factors)
    elif not factors:
        table = dict(defaults)
    else:
        missing = [key for key in defaults if key not in factors]
        if missing:
            warn(
                "activity_factor_missing",
                "Activity factors missing for " + ", ".join(missing) + "; built-in defaults used",
            )
        table = {**{key: defaults[key] for key in missing}, **factors}

    health_factors, health_warnings = parse_factor_lines(
        str_list(health, "health_factors", ["healthy=1.0", "cold=0.3", "sleep_deprived=0.9"]),
        known_keys=("healthy", "cold", "sleep_deprived"),
        label="健康",
    )
    for item in health_warnings:
        warn(f"health_factor:{item}", f"Health factor warning: {item}")

    default_curve = CurveSet()
    mood, mood_warn = parse_curve_points(
        str_list(emo, "emotion_curve", ["0=0.55", "5=0.95", "10=1.35"])
    )
    energy, energy_warn = parse_curve_points(
        str_list(emo, "energy_curve", ["0=0.50", "5=0.85", "10=1.25"])
    )
    for item in mood_warn + energy_warn:
        warn(f"curve:{item}", f"Curve warning: {item}")
    curve = CurveSet(mood=mood or default_curve.mood, energy=energy or default_curve.energy)

    min_adjust, max_adjust, silence_floor = adjust_bounds(config, warn)
    return FactorConfig(
        activity_factors=table,
        health_factors=health_factors or dict(DEFAULT_HEALTH_FACTORS),
        curves_frequency=curve,
        curves_necessity=curve,
        curves_dynamic=curve,
        quiet_hours=quiet_windows(str_list(freq, "quiet_hours", ["23:30-08:00"]), warn),
        max_adjust=max_adjust,
        min_adjust=min_adjust,
        material_bonus=max(0.0, num(freq, "material_bonus", 0.15)),
        material_bonus_cap=max(0.0, num(freq, "material_bonus_cap", 0.45)),
        sleep_debt_cap_nights=max(1, integer(health, "sleep_debt_cap_nights", 3)),
        silence_floor=silence_floor,
    )


def proactive_config(
    config: Mapping[str, Any], *, decay_floor: float, warn: Warn = _noop_warn
) -> ProactiveConfig:
    """``proactive`` section → ``ProactiveConfig``.

    Args:
        config: Whole plugin config.
        decay_floor: Material decay floor (shared with the simulation).
        warn: Warning callback.

    Returns:
        The proactive rules.
    """
    sec = section(config, "proactive")
    return ProactiveConfig(
        enabled=flag(sec, "enabled", False),
        score_threshold=num(sec, "score_threshold", 0.45),
        min_interval_minutes=max(0, integer(sec, "min_interval_minutes", 180)),
        recent_user_silence_minutes=max(0, integer(sec, "recent_user_silence_minutes", 30)),
        daily_max=max(0, integer(sec, "daily_max", 1)),
        minimum_energy=num(sec, "minimum_energy", 2.5),
        quiet_hours=quiet_windows(str_list(sec, "quiet_hours", ["00:00-08:00"]), warn),
        unanswered_backoff_factor=max(1.0, num(sec, "unanswered_backoff_factor", 2.0)),
        unanswered_backoff_max_streak=max(0, integer(sec, "unanswered_backoff_max_streak", 4)),
        material_decay_floor=clamp(decay_floor, 0.0, 1.0),
    )


def economy_thresholds(config: Mapping[str, Any]) -> EconomyThresholds:
    """``economy`` section → normalized tier thresholds.

    Args:
        config: Whole plugin config.

    Returns:
        The thresholds.
    """
    sec = section(config, "economy")
    return EconomyThresholds(
        broke_ratio=num(sec, "broke_ratio", 0.10),
        tight_ratio=num(sec, "tight_ratio", 0.30),
        roomy_ratio=num(sec, "roomy_ratio", 0.60),
        tight_pacing=num(sec, "tight_pacing", 1.20),
        pacing_from_spend_ratio=num(sec, "pacing_from_spend_ratio", 0.50),
    ).normalized()


def filter_mode(sec: Mapping[str, Any], key: str = "filter_mode", warn: Warn = _noop_warn) -> str:
    """Read a scope filter mode; invalid values fall back to the conservative ``whitelist``.

    Args:
        sec: Config section.
        key: Field key.
        warn: Warning callback.

    Returns:
        ``all`` / ``whitelist`` / ``blacklist``.
    """
    mode = text(sec, key, "all").lower() or "all"
    if mode not in ALLOWED_FILTER_MODES:
        warn(f"filter_mode:{mode}", f"filter_mode={mode!r} is invalid; treated as whitelist")
        return "whitelist"
    return mode


def _strip_thread(value: str) -> set[str]:
    """``-100123#45`` → ``{-100123#45, -100123}``（话题/线程后缀会被去掉）。"""
    out = {value}
    if "#" in value:
        head = value.split("#", 1)[0].strip()
        if head:
            out.add(head)
    return out


def _scope_keys(umo: str, group_id: str, user_id: str, is_group: bool) -> set[str]:
    """把一个会话展开成**一组可用于匹配的键**。

    为什么要一组而不是一个：``unified_msg_origin`` 的格式是
    ``platform:message_type:session_id``（``MessageSession.__str__``），而
    ``session_id`` 本身**可以包含冒号**（WebChat 就是
    ``webchat:FriendMessage:webchat!user!sid``）。所以「按 ``:`` 切分后取最后
    一段」这类猜测在真实平台上会出错。

    群号本身也可能带后缀：Telegram 的 ``group_id`` 形如 ``-100123#45``
    （``#`` 后是话题 id）。用户只会填纯群号，所以去后缀的形式也作为候选键，
    否则白名单永远匹配不上。

    Args:
        umo: Session unified_msg_origin.
        group_id: Group id (empty for private chats).
        user_id: Sender / user id.
        is_group: Whether the session is a group chat.

    Returns:
        Candidate keys for scope matching.
    """

    keys: set[str] = set()
    umo = str(umo or "").strip()
    parts = umo.split(":", 2)
    if umo:
        keys.add(umo)
        if len(parts) == 3 and parts[2]:
            keys.add(parts[2])
        if len(parts) >= 2:
            keys.add(":".join(parts[:2]))

    raw_group = str(group_id or "").strip()
    raw_user = str(user_id or "").strip()
    tail = parts[2] if len(parts) == 3 else ""
    if is_group:
        if raw_group:
            keys.update(_strip_thread(raw_group))
            keys.add(f"group:{raw_group}")
        if tail:
            # 群聊里 session_id 通常就是群号：补上，防 get_group_id() 拿不到
            keys.update(_strip_thread(tail))
    else:
        if raw_user:
            keys.add(raw_user)
            keys.add(f"private:{raw_user}")
        if tail:
            keys.add(tail)
            keys.add(f"private:{tail}")
    return {key for key in keys if key}


def scope_matches(
    *,
    mode: str,
    targets: list[str],
    umo: str,
    group_id: str,
    user_id: str,
    is_group: bool,
) -> bool:
    """Whether a session falls inside a ``filter_mode`` / ``target_chats`` scope.

    Targets may be a full unified_msg_origin, ``group:<id>``, ``private:<id>`` or a
    bare id. A bare group id also matches when the platform reports a threaded id
    (``-100123#45`` matches ``-100123``).

    Args:
        mode: ``all`` / ``whitelist`` / ``blacklist``.
        targets: Target entries.
        umo: Session unified_msg_origin.
        group_id: Group id (empty for private chats).
        user_id: Sender id.
        is_group: Whether the session is a group chat.

    Returns:
        True when the session is in scope.
    """
    if mode == "all":
        return True
    if mode not in ("whitelist", "blacklist"):
        # 未知模式按白名单处理：写错的人立刻发现，而不是悄悄全量生效
        mode = "whitelist"
    keys = _scope_keys(umo, group_id, user_id, is_group)
    wanted = {str(item).strip() for item in (targets or []) if str(item).strip()}
    hit = bool(keys & wanted)
    return hit if mode == "whitelist" else not hit


def scope_config(
    sec: Mapping[str, Any],
    *,
    key: str = "filter_mode",
    targets_key: str = "target_chats",
    warn: Warn = _noop_warn,
    label: str = "生效范围",
) -> tuple[str, list[str]]:
    """读一对「范围模式 + 名单」，并在组合无意义时给出**一次**告警。

    两种常见配错都会导致「名单看着填了，却什么也没发生」：

    * ``whitelist`` 但名单为空 → 谁都不生效（闸门形同关闭）；
    * ``blacklist`` 但名单为空 → 与 ``all`` 等价（多半不是本意）。

    这两种都只是「不符合预期」，不是错误，所以只告警不纠正。

    Args:
        sec: Config section holding the pair.
        key: Filter-mode field name.
        targets_key: Target-list field name.
        warn: Warning callback.
        label: Human readable name for messages.

    Returns:
        ``(mode, targets)``.
    """

    mode = filter_mode(sec, key, warn)
    targets = str_list(sec, targets_key, [])
    if mode != "all" and not targets:
        if mode == "whitelist":
            warn(
                f"{key}:empty-whitelist",
                f"{label}设为「仅白名单」，但名单是空的——等于谁都不生效。"
                "请在 target_chats 里填群号，或把模式改成「全部会话」。",
            )
        else:
            warn(
                f"{key}:empty-blacklist",
                f"{label}设为「排除黑名单」，但名单是空的——等于全部会话都生效。",
            )
    return mode, targets


def scope_label(
    sec: Mapping[str, Any],
    *,
    key: str = "filter_mode",
    targets_key: str = "target_chats",
) -> str:
    """把「模式 + 名单」渲染成一句人话，供状态卡展示。

    例：``all`` → ``全部会话``；``whitelist`` + 2 条 → ``白名单 2 个``。
    只做展示，不做校验（校验在 ``scope_config`` 里）。

    Args:
        sec: Config section holding the pair.
        key: Filter-mode field name.
        targets_key: Target-list field name.

    Returns:
        Human readable scope description.
    """

    mode = text(sec, key, "all").strip().lower() or "all"
    count = len(str_list(sec, targets_key, []))
    if mode == "all":
        return "全部会话"
    name = "白名单" if mode != "blacklist" else "黑名单"
    return f"{name} {count} 个" if count else f"{name}（空）"
