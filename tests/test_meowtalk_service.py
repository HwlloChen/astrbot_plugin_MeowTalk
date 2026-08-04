from datetime import datetime, time, timezone

from meowtalk_service import (
    build_group_configs,
    is_quiet_period,
    parse_schedule_times,
    parse_sentences,
    scheduled_due,
)


def test_parse_line_corpus_and_yaml_fallback_shape():
    assert parse_sentences("a\n\nb\n") == ("a", "b")
    assert parse_sentences("sentences:\n  - a\n  - b") == ("a", "b")


def test_parse_schedule_times_ignores_invalid_lines():
    assert parse_schedule_times("09:00\n25:00\n09:00\n18:30") == (
        time(9, 0),
        time(18, 30),
    )


def test_build_group_config_uses_default_corpus_and_rejects_missing_umo():
    groups, errors = build_group_configs(
        {
            "default_sentences": "default",
            "groups": [
                {"__template_key": "group", "umo": "aiocqhttp:GroupMessage:1"},
                {"__template_key": "group"},
            ],
        }
    )
    assert groups["aiocqhttp:GroupMessage:1"].sentences == ("default",)
    assert len(errors) == 1


def test_quiet_period_supports_cross_midnight_and_all_day():
    group, _ = build_group_configs(
        {
            "groups": [
                {
                    "umo": "x",
                    "quiet_enabled": True,
                    "quiet_start": "23:00",
                    "quiet_end": "07:00",
                }
            ]
        }
    )
    config = group["x"]
    assert is_quiet_period(config, datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc))
    assert is_quiet_period(config, datetime(2026, 1, 1, 6, 30, tzinfo=timezone.utc))
    assert not is_quiet_period(config, datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc))

    all_day, _ = build_group_configs(
        {"groups": [{"umo": "x", "quiet_start": "00:00", "quiet_end": "00:00"}]}
    )
    assert is_quiet_period(all_day["x"], datetime(2026, 1, 1, 12, tzinfo=timezone.utc))


def test_scheduled_due_is_stable_and_within_jitter():
    slot = scheduled_due(
        "x", datetime(2026, 1, 1).date(), time(12, 0), 30, timezone.utc
    )
    again = scheduled_due(
        "x", datetime(2026, 1, 1).date(), time(12, 0), 30, timezone.utc
    )
    assert slot == again
    assert (
        -30
        <= (slot - datetime(2026, 1, 1, 12, tzinfo=timezone.utc)).total_seconds()
        <= 30
    )
