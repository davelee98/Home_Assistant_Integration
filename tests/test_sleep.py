"""Unit tests for the pure SleepProfile logic."""

import pytest

from custom_components.opendisplay.sleep import (
    AVAILABILITY_SLACK_S,
    DEFAULT_WAKE_WINDOW_MS,
    FRESHNESS_SLACK_S,
    SleepProfile,
)


def _profile(**overrides):
    """Build a SleepProfile from sensible defaults overridden per test."""
    params = {
        "sleep_mode": "auto",
        "power_mode": 1,  # BATTERY
        "sleep_timeout_ms": 0,
        "deep_sleep_time_seconds": 300,
        "missed_cycles": 3,
        "queue_timeout_hours": 24,
    }
    params.update(overrides)
    return SleepProfile.create(**params)


def test_deep_sleep_enabled_requires_battery_and_interval():
    assert _profile(power_mode=1, deep_sleep_time_seconds=300).deep_sleep_enabled
    # USB power → not a deep-sleeper even with an interval set.
    assert not _profile(power_mode=2, deep_sleep_time_seconds=300).deep_sleep_enabled
    # Battery but no interval → not a deep-sleeper.
    assert not _profile(power_mode=1, deep_sleep_time_seconds=0).deep_sleep_enabled


def test_sleep_mode_auto_follows_device():
    assert _profile(sleep_mode="auto", power_mode=1, deep_sleep_time_seconds=300).is_sleepy
    assert not _profile(
        sleep_mode="auto", power_mode=2, deep_sleep_time_seconds=300
    ).is_sleepy


def test_sleep_mode_force_on_and_off_override_device():
    # Force on even for a USB device with no deep sleep.
    assert _profile(sleep_mode="on", power_mode=2, deep_sleep_time_seconds=0).is_sleepy
    # Force off even for a battery device configured for deep sleep.
    forced_off = _profile(sleep_mode="off", power_mode=1, deep_sleep_time_seconds=300)
    assert not forced_off.is_sleepy
    # deep_sleep_enabled still reflects the device, independent of the override.
    assert forced_off.deep_sleep_enabled


def test_wake_window_uses_firmware_default_when_zero():
    assert _profile(sleep_timeout_ms=0).wake_window_s == DEFAULT_WAKE_WINDOW_MS / 1000.0
    assert _profile(sleep_timeout_ms=5000).wake_window_s == 5.0


def test_availability_interval_formula():
    profile = _profile(
        sleep_timeout_ms=0, deep_sleep_time_seconds=300, missed_cycles=3
    )
    # 300 * 3 + 10 (default window) + 60 slack = 970
    expected = 300 * 3 + DEFAULT_WAKE_WINDOW_MS / 1000.0 + AVAILABILITY_SLACK_S
    assert profile.availability_interval == expected == 970.0


def test_queue_timeout_seconds():
    assert _profile(queue_timeout_hours=24).queue_timeout_s == 86400
    assert _profile(queue_timeout_hours=1).queue_timeout_s == 3600


def test_probably_asleep_never_seen_is_true():
    assert _profile().probably_asleep(None) is True


def test_probably_asleep_recent_advert_is_false():
    profile = _profile(sleep_timeout_ms=10000)  # 10 s window
    now = 1_000_000.0
    # Seen 1 s ago: still inside the wake window -> may be awake.
    assert profile.probably_asleep(now - 1.0, now=now) is False


def test_probably_asleep_stale_advert_is_true():
    profile = _profile(sleep_timeout_ms=10000)
    now = 1_000_000.0
    # Seen well beyond window + slack -> back asleep.
    stale = now - (10.0 + FRESHNESS_SLACK_S + 1.0)
    assert profile.probably_asleep(stale, now=now) is True


def test_probably_asleep_boundary():
    profile = _profile(sleep_timeout_ms=10000)
    now = 1_000_000.0
    threshold = 10.0 + FRESHNESS_SLACK_S
    # Exactly at the threshold is not yet "asleep" (strict greater-than).
    assert profile.probably_asleep(now - threshold, now=now) is False
    assert profile.probably_asleep(now - threshold - 0.01, now=now) is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
