"""Unit tests for the options-flow schema (sliding-window transfer options).

Mirrors the other test modules: no Home Assistant test harness; the schema is
exercised directly via ``_options_schema()`` — a plain voluptuous schema — so a
round-trip through it is exactly what the options flow persists.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol

from homeassistant.data_entry_flow import AbortFlow

from custom_components.opendisplay.config_flow import (
    OpenDisplayConfigFlow,
    _options_schema,
)
from custom_components.opendisplay.const import (
    CONF_BLOCKS_PER_ACK,
    CONF_HOST,
    CONF_MAX_QUEUE_SIZE,
    CONF_MISSED_CYCLES,
    CONF_PORT,
    CONF_PROBE_BEFORE_QUEUE,
    CONF_QUEUE_TIMEOUT_HOURS,
    CONF_SLEEP_MODE,
    CONF_TLS,
    DEFAULT_BLOCKS_PER_ACK,
    DEFAULT_MAX_QUEUE_SIZE,
    DEFAULT_MISSED_CYCLES,
    DEFAULT_PROBE_BEFORE_QUEUE,
    DEFAULT_QUEUE_TIMEOUT_HOURS,
    DEFAULT_SLEEP_MODE,
)


def test_options_schema_defaults():
    """An empty submission resolves every option to its default."""
    result = _options_schema()({})

    assert result[CONF_SLEEP_MODE] == DEFAULT_SLEEP_MODE
    assert result[CONF_MISSED_CYCLES] == DEFAULT_MISSED_CYCLES
    assert result[CONF_QUEUE_TIMEOUT_HOURS] == DEFAULT_QUEUE_TIMEOUT_HOURS
    assert result[CONF_PROBE_BEFORE_QUEUE] == DEFAULT_PROBE_BEFORE_QUEUE
    assert result[CONF_BLOCKS_PER_ACK] == DEFAULT_BLOCKS_PER_ACK
    assert result[CONF_MAX_QUEUE_SIZE] == DEFAULT_MAX_QUEUE_SIZE


def test_options_schema_custom_values_persist():
    """Custom values round-trip, coerced to int (NumberSelector yields floats)."""
    result = _options_schema()(
        {
            CONF_SLEEP_MODE: DEFAULT_SLEEP_MODE,
            CONF_MISSED_CYCLES: DEFAULT_MISSED_CYCLES,
            CONF_QUEUE_TIMEOUT_HOURS: DEFAULT_QUEUE_TIMEOUT_HOURS,
            CONF_PROBE_BEFORE_QUEUE: DEFAULT_PROBE_BEFORE_QUEUE,
            CONF_BLOCKS_PER_ACK: 4.0,
            CONF_MAX_QUEUE_SIZE: 1,
        }
    )

    assert result[CONF_BLOCKS_PER_ACK] == 4
    assert isinstance(result[CONF_BLOCKS_PER_ACK], int)
    assert result[CONF_MAX_QUEUE_SIZE] == 1  # 1 == fast transfer disabled
    assert isinstance(result[CONF_MAX_QUEUE_SIZE], int)


@pytest.mark.parametrize("key", [CONF_BLOCKS_PER_ACK, CONF_MAX_QUEUE_SIZE])
@pytest.mark.parametrize("value", [0, 33])
def test_options_schema_rejects_out_of_range(key, value):
    """Both options are bounded to the protocol's 1..32 window range."""
    with pytest.raises(vol.Invalid):
        _options_schema()({key: value})


# -- zeroconf (WiFi/mDNS) discovery ----------------------------------------
#
# These drive the config-flow steps directly with the base-class HA touchpoints
# mocked (no flow manager), mirroring the mocked-namespace style of the other
# test modules. The unique_id is the uppercase-colon BLE MAC so a WiFi discovery
# dedups onto an existing BLE entry.

MAC_LOWER = "aa:bb:cc:dd:ee:ff"
MAC_UPPER = "AA:BB:CC:DD:EE:FF"


def _zeroconf_info(properties, *, host="1.2.3.4", port=2446, name=None):
    """A ZeroconfServiceInfo stand-in (only the attributes the step reads)."""
    return SimpleNamespace(
        host=host,
        port=port,
        name=name if name is not None else "tag._opendisplay._tcp.local.",
        properties=properties,
    )


def _flow(entries=()):
    """A config flow with base-class HA methods mocked out."""
    flow = OpenDisplayConfigFlow()
    flow.hass = MagicMock()
    flow.hass.config_entries.async_entries = MagicMock(return_value=list(entries))
    flow.context = {}
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    flow.async_abort = MagicMock(side_effect=lambda **kw: {"type": "abort", **kw})
    flow.async_show_form = MagicMock(side_effect=lambda **kw: {"type": "form", **kw})
    flow.async_create_entry = MagicMock(
        side_effect=lambda **kw: {"type": "create_entry", **kw}
    )
    flow._set_confirm_only = MagicMock()
    return flow


@pytest.mark.asyncio
async def test_zeroconf_missing_mac_aborts():
    """A record without a mac TXT key cannot be correlated -> abort no_mac."""
    flow = _flow()
    result = await flow.async_step_zeroconf(_zeroconf_info({"tls": "0"}))

    assert result["reason"] == "no_mac"
    flow.async_set_unique_id.assert_not_called()


@pytest.mark.asyncio
async def test_zeroconf_new_device_shows_confirm_and_uppercases_id():
    """A never-configured device: unique_id uppercased, confirm form shown."""
    flow = _flow(entries=[])
    result = await flow.async_step_zeroconf(
        _zeroconf_info({"mac": MAC_LOWER, "tls": "0"})
    )

    flow.async_set_unique_id.assert_awaited_once_with(MAC_UPPER)
    flow._abort_if_unique_id_configured.assert_called_once()
    assert result["type"] == "form"
    assert result["step_id"] == "zeroconf_confirm"


@pytest.mark.asyncio
async def test_zeroconf_existing_ble_entry_gains_host_and_aborts():
    """An existing (BLE) entry: merge host/port/tls, then abort already_configured."""
    flow = _flow()
    flow._abort_if_unique_id_configured.side_effect = AbortFlow("already_configured")

    with pytest.raises(AbortFlow):
        await flow.async_step_zeroconf(
            _zeroconf_info(
                {"mac": MAC_LOWER, "tls": "1"}, host="10.0.0.9", port=2447
            )
        )

    # unique_id reconciled to uppercase, and the existing entry is updated with
    # the live LAN endpoint (tls "1" -> True).
    flow.async_set_unique_id.assert_awaited_once_with(MAC_UPPER)
    updates = flow._abort_if_unique_id_configured.call_args.kwargs["updates"]
    assert updates == {CONF_HOST: "10.0.0.9", CONF_PORT: 2447, CONF_TLS: True}


@pytest.mark.asyncio
async def test_zeroconf_feeds_mdns_presence_to_configured_entry():
    """A sighting of a configured entry records mDNS presence + wakes delivery."""
    delivery = MagicMock()
    entry = SimpleNamespace(
        unique_id=MAC_UPPER,
        runtime_data=SimpleNamespace(mdns_last_seen=None, delivery=delivery),
    )
    flow = _flow(entries=[entry])
    flow._abort_if_unique_id_configured.side_effect = AbortFlow("already_configured")

    with pytest.raises(AbortFlow):
        await flow.async_step_zeroconf(
            _zeroconf_info({"mac": MAC_LOWER, "tls": "0"})
        )

    assert entry.runtime_data.mdns_last_seen is not None
    delivery.notify_device_seen.assert_called_once_with("mdns")


@pytest.mark.asyncio
async def test_zeroconf_confirm_probes_tcp_and_creates_entry():
    """Confirming a new WiFi device probes TCP then creates a host/port/tls entry."""
    flow = _flow(entries=[])
    await flow.async_step_zeroconf(
        _zeroconf_info({"mac": MAC_LOWER, "tls": "0"}, host="1.2.3.4", port=2446)
    )
    flow._async_test_connection_tcp = AsyncMock()

    result = await flow.async_step_zeroconf_confirm(user_input={})

    flow._async_test_connection_tcp.assert_awaited_once_with("1.2.3.4", 2446)
    assert result["type"] == "create_entry"
    assert result["data"] == {
        CONF_HOST: "1.2.3.4",
        CONF_PORT: 2446,
        CONF_TLS: False,
    }


@pytest.mark.asyncio
async def test_zeroconf_confirm_tcp_unreachable_shows_error():
    """A failed TCP probe re-shows the confirm form with cannot_connect."""
    flow = _flow(entries=[])
    await flow.async_step_zeroconf(
        _zeroconf_info({"mac": MAC_LOWER, "tls": "0"})
    )
    flow._async_test_connection_tcp = AsyncMock(side_effect=OSError("refused"))

    result = await flow.async_step_zeroconf_confirm(user_input={})

    assert result["type"] == "form"
    assert result["step_id"] == "zeroconf_confirm"
    assert result["errors"] == {"base": "cannot_connect"}
    flow.async_create_entry.assert_not_called()


@pytest.mark.asyncio
async def test_zeroconf_tls_port_default_when_port_absent():
    """When the SRV record omits a port, tls TXT selects the derived TLS port."""
    flow = _flow()
    flow._abort_if_unique_id_configured.side_effect = AbortFlow("already_configured")

    with pytest.raises(AbortFlow):
        await flow.async_step_zeroconf(
            _zeroconf_info({"mac": MAC_LOWER, "tls": "1"}, port=None)
        )

    updates = flow._abort_if_unique_id_configured.call_args.kwargs["updates"]
    assert updates[CONF_PORT] == 2447
    assert updates[CONF_TLS] is True
