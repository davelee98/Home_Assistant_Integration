"""Unit tests for the resolution sensor.

Like test_last_seen, these avoid the full Home Assistant test harness. Real
``DisplayConfig`` instances are used rather than stubs, so rotation and colour
scheme resolve as they do in production.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from opendisplay.models.config import DisplayConfig

from custom_components.opendisplay.sensor import (
    _RESOLUTION_DESCRIPTION,
    OpenDisplayResolutionSensor,
)


def _display(rotation=2, color_scheme=4, **overrides):
    """Return a real DisplayConfig, matching a 7.3" six-colour panel."""
    fields = {
        "instance_number": 0,
        "display_technology": 1,
        "panel_ic_type": 35,
        "pixel_width": 800,
        "pixel_height": 480,
        "active_width_mm": 160,
        "active_height_mm": 96,
        "tag_type": 0,
        "rotation": rotation,
        "reset_pin": 12,
        "busy_pin": 13,
        "dc_pin": 8,
        "cs_pin": 9,
        "data_pin": 11,
        "partial_update_support": 0,
        "color_scheme": color_scheme,
        "transmission_modes": 9,
        "clk_pin": 10,
        "reserved_pins": b"\x00" * 7,
        "full_update_mC": 400,
        "reserved": b"\x00" * 13,
    }
    fields.update(overrides)
    return DisplayConfig(**fields)


def _make_sensor(*displays):
    """Return a resolution sensor reading from a mutable runtime_data."""
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            device_config=SimpleNamespace(displays=list(displays))
        )
    )
    coordinator = MagicMock()
    coordinator.address = "AA:BB:CC:DD:EE:FF"
    return OpenDisplayResolutionSensor(coordinator, _RESOLUTION_DESCRIPTION, entry)


def test_unrecognised_values_do_not_masquerade_as_valid_ones():
    attrs = _make_sensor(_display(rotation=99, color_scheme=99)).extra_state_attributes
    assert attrs["rotation"] is None
    assert attrs["color_scheme"] == 99


def test_config_is_re_read_so_a_wake_time_resync_is_picked_up():
    sensor = _make_sensor(_display())
    assert sensor.native_value == "800x480"
    sensor._entry.runtime_data.device_config = SimpleNamespace(
        displays=[_display(pixel_width=960, pixel_height=640)]
    )
    assert sensor.native_value == "960x640"
