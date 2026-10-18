"""Reply gate for AstrBot: decides whether plain (non-woken) group chatter gets a reply.

The count gate reproduces MaiBot's ``frequency`` trigger mode, which the original
life-frequency plugin targeted:

* threshold ``T = max(1, ceil(1 / effective_frequency))`` messages;
* when fewer than ``T`` messages are pending, silence is converted into
  equivalent messages (``idle / average_interval``), capped at ``T - 1`` so pure
  silence can never trigger a reply on its own;
* ``effective_frequency <= 0`` is a hard silence.

The probability gate is a simpler alternative: each message passes with
probability ``base_probability * multiplier`` (clamped to ``[0, 1]``).
"""

from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass, field

MODE_COUNT = "count"
MODE_PROBABILITY = "probability"
GATE_MODES = (MODE_COUNT, MODE_PROBABILITY)

#: Number of recent message timestamps kept per session for the average interval.
INTERVAL_SAMPLES = 20


def normalize_mode(value: object) -> str:
    """Normalize a configured gate mode.

    Args:
        value: Raw configuration value.

    Returns:
        ``"count"`` or ``"probability"``; unknown values fall back to ``"count"``.
    """
    text = str(value or "").strip().lower()
    return text if text in GATE_MODES else MODE_COUNT


def _finite(value: object, default: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def is_silent(effective_frequency: float) -> bool:
    """Whether the effective frequency means hard silence (exactly zero or below).

    Args:
        effective_frequency: Base talk value multiplied by the life multiplier.

    Returns:
        True when no plain chatter may trigger a reply.
    """
    return _finite(effective_frequency, 0.0) <= 0.0


def trigger_threshold(effective_frequency: float) -> int:
    """Messages needed before the count gate fires.

    Args:
        effective_frequency: Base talk value multiplied by the life multiplier.

    Returns:
        ``0`` for hard silence, otherwise ``max(1, ceil(1 / f))``.
    """
    freq = _finite(effective_frequency, 0.0)
    if freq <= 0:
        return 0
    return max(1, math.ceil(1.0 / freq))


def idle_equivalent_count(
    *, idle_seconds: float, average_message_interval: float, threshold: int
) -> float:
    """Convert idle time into equivalent messages, capped at ``threshold - 1``.

    Args:
        idle_seconds: Seconds since the previous message in the session.
        average_message_interval: Average seconds between recent messages.
        threshold: Count gate threshold.

    Returns:
        Equivalent message count (never enough to trigger by itself).
    """
    interval = _finite(average_message_interval, 0.0)
    if interval <= 0:
        return 0.0
    cap = float(max(0, int(threshold) - 1))
    return min(max(0.0, _finite(idle_seconds, 0.0)) / interval, cap)


def triggers_by_count(
    *,
    effective_frequency: float,
    pending_count: int,
    idle_seconds: float = 0.0,
    average_message_interval: float = 0.0,
) -> bool:
    """Whether the count gate lets the pending batch through.

    Args:
        effective_frequency: Base talk value multiplied by the life multiplier.
        pending_count: Plain messages accumulated since the last reply.
        idle_seconds: Silence before the newest message.
        average_message_interval: Average gap between recent messages.

    Returns:
        True when the bot should reply now.
    """
    threshold = trigger_threshold(effective_frequency)
    if threshold <= 0 or int(pending_count) < 1:
        return False
    if int(pending_count) >= threshold:
        return True
    equivalent = idle_equivalent_count(
        idle_seconds=idle_seconds,
        average_message_interval=average_message_interval,
        threshold=threshold,
    )
    return int(pending_count) + equivalent >= threshold


def reply_probability(base_probability: float, multiplier: float) -> float:
    """Per-message reply probability for the probability gate.

    Args:
        base_probability: Configured probability at multiplier 1.0.
        multiplier: Current life multiplier.

    Returns:
        Probability clamped to ``[0, 1]``.
    """
    value = _finite(base_probability, 0.0) * _finite(multiplier, 0.0)
    return min(1.0, max(0.0, value))


@dataclass
class SessionGate:
    """Per-session gate bookkeeping (in memory only)."""

    pending: int = 0
    last_message_at: float = 0.0
    last_reply_at: float = 0.0
    stamps: deque = field(default_factory=lambda: deque(maxlen=INTERVAL_SAMPLES))

    def average_interval(self) -> float:
        """Average seconds between the recorded message timestamps.

        Returns:
            ``0.0`` when fewer than two samples exist.
        """
        if len(self.stamps) < 2:
            return 0.0
        span = self.stamps[-1] - self.stamps[0]
        return max(0.0, span / (len(self.stamps) - 1))


@dataclass(frozen=True)
class GateDecision:
    """Outcome of one gate evaluation."""

    reply: bool
    reason: str
    threshold: int = 0
    pending: int = 0
    probability: float = 0.0


def evaluate(
    gate: SessionGate,
    *,
    now: float,
    mode: str,
    multiplier: float,
    base_talk_value: float,
    base_probability: float,
    min_reply_interval_seconds: float,
    rng: random.Random | None = None,
) -> GateDecision:
    """Register one plain group message and decide whether to reply.

    Mutates ``gate`` (pending counter, timestamps, last reply time).

    Args:
        gate: Session bookkeeping.
        now: Current epoch seconds.
        mode: ``"count"`` or ``"probability"``.
        multiplier: Current life multiplier.
        base_talk_value: Count-gate base frequency (multiplier 1.0 → this value).
        base_probability: Probability-gate base probability.
        min_reply_interval_seconds: Minimum seconds between two gate replies.
        rng: Random source (for tests).

    Returns:
        The decision; ``reply=True`` means the caller should request an LLM reply.
    """
    rng = rng or random
    idle = (now - gate.last_message_at) if gate.last_message_at > 0 else 0.0
    gate.pending += 1
    gate.last_message_at = now
    gate.stamps.append(now)

    effective = _finite(base_talk_value, 1.0) * _finite(multiplier, 0.0)
    if is_silent(effective) and normalize_mode(mode) == MODE_COUNT:
        return GateDecision(False, "silent", 0, gate.pending)

    cooldown = _finite(min_reply_interval_seconds, 0.0)
    in_cooldown = gate.last_reply_at > 0 and now - gate.last_reply_at < cooldown

    if normalize_mode(mode) == MODE_PROBABILITY:
        probability = reply_probability(base_probability, multiplier)
        if probability <= 0:
            return GateDecision(False, "silent", 0, gate.pending, 0.0)
        if in_cooldown:
            return GateDecision(False, "cooldown", 0, gate.pending, probability)
        if rng.random() < probability:
            gate.pending = 0
            gate.last_reply_at = now
            return GateDecision(True, "probability", 0, 0, probability)
        return GateDecision(False, "dice", 0, gate.pending, probability)

    threshold = trigger_threshold(effective)
    if not triggers_by_count(
        effective_frequency=effective,
        pending_count=gate.pending,
        idle_seconds=idle,
        average_message_interval=gate.average_interval(),
    ):
        return GateDecision(False, "pending", threshold, gate.pending)
    if in_cooldown:
        return GateDecision(False, "cooldown", threshold, gate.pending)
    gate.pending = 0
    gate.last_reply_at = now
    return GateDecision(True, "count", threshold, 0)
