from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

try:
    import yaml
except ImportError:  # pragma: no cover - AstrBot normally provides PyYAML.
    yaml = None


@dataclass(frozen=True)
class GroupConfig:
    """Validated configuration for one subscribed group."""

    umo: str
    group_id: str
    group_name: str
    enabled: bool
    idle_enabled: bool
    idle_timeout_seconds: float
    idle_jitter_seconds: float
    quiet_enabled: bool
    quiet_start: time
    quiet_end: time
    schedule_enabled: bool
    schedule_times: tuple[time, ...]
    schedule_labels: tuple[str, ...]
    schedule_jitter_seconds: int
    sentences: tuple[str, ...]


@dataclass
class GroupRuntime:
    """Mutable scheduler state for one group."""

    quiet_active: bool
    schedule_started_at: datetime
    idle_due: datetime | None = None
    idle_sent: bool = False


def parse_sentences(raw: object) -> tuple[str, ...]:
    """Parse line-oriented corpus text or the documented YAML format.

    Args:
        raw: Text area value or a list of sentence values.

    Returns:
        Non-empty, trimmed sentences in their configured order.
    """
    if isinstance(raw, list):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    if not isinstance(raw, str):
        return ()
    text = raw.strip()
    if not text:
        return ()
    if text.startswith("sentences:"):
        parsed = None
        if yaml is not None:
            try:
                parsed = yaml.safe_load(text)
            except Exception:
                parsed = None
        if isinstance(parsed, Mapping):
            values = parsed.get("sentences", [])
            if isinstance(values, list):
                return tuple(str(item).strip() for item in values if str(item).strip())
        values = []
        in_sentences = False
        for line in text.splitlines():
            if line.strip() == "sentences:":
                in_sentences = True
                continue
            if in_sentences and line.lstrip().startswith("-"):
                value = line.lstrip()[1:].strip().strip("'\"")
                if value:
                    values.append(value)
        return tuple(values)
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def parse_clock(value: object, default: time) -> time:
    """Parse a `HH:MM` value, returning a safe default on invalid input."""
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    if isinstance(value, str):
        try:
            hour, minute = (int(part) for part in value.strip().split(":", 1))
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                return time(hour, minute)
        except (TypeError, ValueError):
            pass
    return default


def parse_schedule_times(raw: object) -> tuple[time, ...]:
    """Parse one `HH:MM` per line, ignoring invalid entries."""
    if not isinstance(raw, str):
        return ()
    result: list[time] = []
    for line in raw.splitlines():
        parsed = parse_clock(line, time(0, 0))
        if line.strip() == parsed.strftime("%H:%M"):
            result.append(parsed)
    return tuple(sorted(set(result)))


def build_group_configs(
    config: Mapping,
) -> tuple[dict[str, GroupConfig], tuple[str, ...]]:
    """Normalize plugin configuration into validated subscribed groups.

    Args:
        config: AstrBot plugin configuration mapping.

    Returns:
        A mapping keyed by unified message origin and validation warnings.
    """
    default_sentences = parse_sentences(config.get("default_sentences", ""))
    groups = config.get("groups", [])
    result: dict[str, GroupConfig] = {}
    errors: list[str] = []
    if not isinstance(groups, list):
        return {}, ("groups 必须是列表。",)
    for index, raw in enumerate(groups):
        if not isinstance(raw, Mapping):
            errors.append(f"第 {index + 1} 个订阅条目不是对象。")
            continue
        umo = str(raw.get("umo", "")).strip()
        if not umo:
            errors.append(f"第 {index + 1} 个订阅条目缺少 unified message origin。")
            continue
        if umo in result:
            errors.append(f"重复的订阅目标：{umo}。")
            continue
        timeout_minutes = _positive_float(raw.get("idle_timeout_minutes"), 30.0)
        idle_jitter = _non_negative_float(raw.get("idle_jitter_seconds"), 0.0)
        schedule_jitter = int(
            _non_negative_float(raw.get("schedule_jitter_seconds"), 0.0)
        )
        schedule_times = parse_schedule_times(raw.get("schedule_times", ""))
        sentences = parse_sentences(raw.get("sentences", "")) or default_sentences
        result[umo] = GroupConfig(
            umo=umo,
            group_id=str(raw.get("group_id", "")).strip(),
            group_name=str(raw.get("group_name", "")).strip(),
            enabled=bool(raw.get("enabled", True)),
            idle_enabled=bool(raw.get("idle_enabled", True)),
            idle_timeout_seconds=timeout_minutes * 60,
            idle_jitter_seconds=idle_jitter,
            quiet_enabled=bool(raw.get("quiet_enabled", True)),
            quiet_start=parse_clock(raw.get("quiet_start"), time(23, 0)),
            quiet_end=parse_clock(raw.get("quiet_end"), time(7, 0)),
            schedule_enabled=bool(raw.get("schedule_enabled", False)),
            schedule_times=schedule_times,
            schedule_labels=tuple(item.strftime("%H:%M") for item in schedule_times),
            schedule_jitter_seconds=schedule_jitter,
            sentences=sentences,
        )
    return result, tuple(errors)


def is_quiet_period(group: GroupConfig, current: datetime) -> bool:
    """Return whether the current local time is inside the quiet period."""
    if not group.quiet_enabled:
        return False
    current_time = current.timetz().replace(tzinfo=None)
    if group.quiet_start == group.quiet_end:
        return True
    if group.quiet_start < group.quiet_end:
        return group.quiet_start <= current_time < group.quiet_end
    return current_time >= group.quiet_start or current_time < group.quiet_end


def scheduled_due(
    umo: str,
    slot_date: date,
    schedule_time: time,
    jitter_seconds: int,
    timezone: tzinfo,
) -> datetime:
    """Return a stable jittered datetime for a daily schedule slot."""
    seed = f"{umo}|{slot_date.isoformat()}|{schedule_time.strftime('%H:%M')}".encode()
    digest = hashlib.sha256(seed).digest()
    span = max(1, jitter_seconds * 2 + 1)
    offset = int.from_bytes(digest[:8], "big") % span - jitter_seconds
    return datetime.combine(slot_date, schedule_time, tzinfo=timezone) + timedelta(
        seconds=offset
    )


def _positive_float(value: object, default: float) -> float:
    try:
        return max(0.1, float(value))
    except (TypeError, ValueError):
        return default


def _non_negative_float(value: object, default: float) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default
