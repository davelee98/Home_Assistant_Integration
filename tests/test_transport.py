"""Unit tests for the WiFi/BLE transport resolver.

No Home Assistant test harness: the resolver's connection touchpoints
(``async_ble_device_from_address`` and ``OpenDisplayDevice``) are patched in the
transport module namespace, and the config entry is a plain ``MagicMock`` with a
``SimpleNamespace`` runtime — matching the other test modules' style.
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from opendisplay import OpenDisplayConnectionError, OpenDisplayTimeoutError
import pytest

from custom_components.opendisplay import transport as transport_mod
from custom_components.opendisplay.const import (
    CONF_HOST,
    CONF_PORT,
    CONF_TLS,
    DEFAULT_LAN_PORT,
    MDNS_FRESHNESS_WINDOW_S,
)
from custom_components.opendisplay.transport import (
    TRANSPORT_BLE,
    TRANSPORT_WIFI,
    async_run_with_fallback,
    note_mdns_seen,
    resolve_transport,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"


def _entry(data=None, mdns_last_seen=None, delivery=None):
    """Build a fake config entry with a mutable SimpleNamespace runtime."""
    runtime = SimpleNamespace(
        mdns_last_seen=mdns_last_seen,
        delivery=delivery,
        last_transport=None,
    )
    entry = MagicMock()
    entry.unique_id = ADDRESS
    entry.data = data if data is not None else {}
    entry.runtime_data = runtime
    return entry


def _device_ctx(device):
    """Async context manager yielding ``device`` (mirrors OpenDisplayDevice())."""

    class _Ctx:
        async def __aenter__(self):
            return device

        async def __aexit__(self, *exc):
            return False

    return _Ctx()


# -- resolve_transport ------------------------------------------------------


def test_resolve_no_host_is_ble():
    """An entry with no CONF_HOST (every pre-WiFi entry) resolves to BLE."""
    resolved = resolve_transport(_entry(data={}))
    assert resolved.use_wifi is False
    assert resolved.host is None


def test_resolve_host_fresh_mdns_prefers_wifi():
    """Host present + a recent mDNS sighting -> WiFi, carrying host/port/tls."""
    entry = _entry(
        data={CONF_HOST: "1.2.3.4", CONF_PORT: 2447, CONF_TLS: True},
        mdns_last_seen=time.monotonic(),
    )
    resolved = resolve_transport(entry)
    assert resolved.use_wifi is True
    assert resolved.host == "1.2.3.4"
    assert resolved.port == 2447
    assert resolved.tls is True


def test_resolve_host_stale_mdns_falls_back_to_ble():
    """A host known but not seen via mDNS within the window -> BLE."""
    entry = _entry(
        data={CONF_HOST: "1.2.3.4"},
        mdns_last_seen=time.monotonic() - MDNS_FRESHNESS_WINDOW_S - 60,
    )
    resolved = resolve_transport(entry)
    assert resolved.use_wifi is False
    # host is still carried (a caller may log it) but WiFi is not chosen.
    assert resolved.host == "1.2.3.4"
    assert resolved.port == DEFAULT_LAN_PORT


def test_resolve_host_never_seen_is_ble():
    """A host with no mDNS sighting yet -> BLE (no premature WiFi)."""
    entry = _entry(data={CONF_HOST: "1.2.3.4"}, mdns_last_seen=None)
    assert resolve_transport(entry).use_wifi is False


# -- note_mdns_seen ---------------------------------------------------------


def test_note_mdns_seen_records_timestamp_and_wakes_delivery():
    """A sighting stamps the freshness timestamp and triggers a wake."""
    delivery = MagicMock()
    entry = _entry(data={CONF_HOST: "1.2.3.4"}, delivery=delivery)
    assert entry.runtime_data.mdns_last_seen is None

    note_mdns_seen(entry)

    assert entry.runtime_data.mdns_last_seen is not None
    delivery.notify_device_seen.assert_called_once_with("mdns")


def test_note_mdns_seen_without_runtime_is_noop():
    """An unloaded entry (no runtime_data) is tolerated silently."""
    entry = MagicMock()
    entry.runtime_data = None
    note_mdns_seen(entry)  # must not raise


# -- async_run_with_fallback ------------------------------------------------


@pytest.mark.asyncio
async def test_wifi_preferred_when_fresh():
    """A fresh mDNS host connects over WiFi; BLE is never touched."""
    entry = _entry(
        data={CONF_HOST: "1.2.3.4", CONF_PORT: 2446, CONF_TLS: False},
        mdns_last_seen=time.monotonic(),
    )
    device = MagicMock()
    action = AsyncMock()
    calls: list[dict] = []

    def _factory(**kwargs):
        calls.append(kwargs)
        return _device_ctx(device)

    with (
        patch.object(transport_mod, "OpenDisplayDevice", side_effect=_factory),
        patch.object(transport_mod, "async_ble_device_from_address") as ble,
    ):
        result = await async_run_with_fallback(
            MagicMock(),
            entry,
            action,
            base_kwargs={"config": None},
            ble_unavailable=lambda: AssertionError("BLE must not be reached"),
        )

    assert result == TRANSPORT_WIFI
    action.assert_awaited_once_with(device)
    assert "host" in calls[0] and "mac_address" not in calls[0]
    ble.assert_not_called()
    assert entry.runtime_data.last_transport == TRANSPORT_WIFI


@pytest.mark.asyncio
async def test_wifi_failure_falls_back_to_ble_same_delivery():
    """A WiFi connection failure retries the same action over BLE."""
    entry = _entry(
        data={CONF_HOST: "1.2.3.4", CONF_PORT: 2446, CONF_TLS: False},
        mdns_last_seen=time.monotonic(),
    )
    device = MagicMock()
    action = AsyncMock()

    def _factory(**kwargs):
        if "host" in kwargs:  # the WiFi attempt
            raise OpenDisplayConnectionError("unreachable")
        return _device_ctx(device)  # the BLE fallback

    with (
        patch.object(transport_mod, "OpenDisplayDevice", side_effect=_factory),
        patch.object(
            transport_mod, "async_ble_device_from_address", return_value=MagicMock()
        ),
    ):
        result = await async_run_with_fallback(
            MagicMock(),
            entry,
            action,
            base_kwargs={"config": None},
            ble_unavailable=lambda: AssertionError("BLE device was available"),
        )

    assert result == TRANSPORT_BLE
    # The same action ran once — over BLE, after the WiFi attempt raised.
    action.assert_awaited_once_with(device)
    assert entry.runtime_data.last_transport == TRANSPORT_BLE


@pytest.mark.asyncio
async def test_wifi_tls_oserror_falls_back_to_ble():
    """A raw socket/TLS error (OSError) also triggers the BLE fallback."""
    entry = _entry(
        data={CONF_HOST: "1.2.3.4", CONF_TLS: True},
        mdns_last_seen=time.monotonic(),
    )
    device = MagicMock()
    action = AsyncMock()

    def _factory(**kwargs):
        if "host" in kwargs:
            raise OSError("TLS handshake failed")
        return _device_ctx(device)

    with (
        patch.object(transport_mod, "OpenDisplayDevice", side_effect=_factory),
        patch.object(
            transport_mod, "async_ble_device_from_address", return_value=MagicMock()
        ),
    ):
        result = await async_run_with_fallback(
            MagicMock(), entry, action, base_kwargs={}, ble_unavailable=RuntimeError
        )

    assert result == TRANSPORT_BLE


@pytest.mark.asyncio
async def test_stale_host_uses_ble_directly():
    """No fresh mDNS -> BLE with no WiFi attempt at all."""
    entry = _entry(data={CONF_HOST: "1.2.3.4"}, mdns_last_seen=None)
    device = MagicMock()
    action = AsyncMock()
    calls: list[dict] = []

    def _factory(**kwargs):
        calls.append(kwargs)
        return _device_ctx(device)

    with (
        patch.object(transport_mod, "OpenDisplayDevice", side_effect=_factory),
        patch.object(
            transport_mod, "async_ble_device_from_address", return_value=MagicMock()
        ),
    ):
        result = await async_run_with_fallback(
            MagicMock(), entry, action, base_kwargs={}, ble_unavailable=RuntimeError
        )

    assert result == TRANSPORT_BLE
    assert len(calls) == 1 and "mac_address" in calls[0] and "host" not in calls[0]


@pytest.mark.asyncio
async def test_ble_unavailable_raises_supplied_exception():
    """When BLE is selected but no connectable device exists, the caller's
    exception factory decides the outcome (e.g. _DeviceUnavailable)."""
    entry = _entry(data={}, mdns_last_seen=None)  # no host -> BLE
    action = AsyncMock()

    class _Sentinel(Exception):
        pass

    with (
        patch.object(transport_mod, "OpenDisplayDevice"),
        patch.object(
            transport_mod, "async_ble_device_from_address", return_value=None
        ),
    ):
        with pytest.raises(_Sentinel):
            await async_run_with_fallback(
                MagicMock(),
                entry,
                action,
                base_kwargs={},
                ble_unavailable=_Sentinel,
            )

    action.assert_not_awaited()


@pytest.mark.asyncio
async def test_wifi_timeout_falls_back_to_ble():
    """A neutral OpenDisplayTimeoutError from WiFi also falls back to BLE."""
    entry = _entry(data={CONF_HOST: "1.2.3.4"}, mdns_last_seen=time.monotonic())
    device = MagicMock()
    action = AsyncMock()

    def _factory(**kwargs):
        if "host" in kwargs:
            raise OpenDisplayTimeoutError("read timeout")
        return _device_ctx(device)

    with (
        patch.object(transport_mod, "OpenDisplayDevice", side_effect=_factory),
        patch.object(
            transport_mod, "async_ble_device_from_address", return_value=MagicMock()
        ),
    ):
        result = await async_run_with_fallback(
            MagicMock(), entry, action, base_kwargs={}, ble_unavailable=RuntimeError
        )

    assert result == TRANSPORT_BLE
