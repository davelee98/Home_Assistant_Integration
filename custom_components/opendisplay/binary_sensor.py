"""Binary sensor platform for OpenDisplay devices."""

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import OpenDisplayConfigEntry
from .const import SIGNAL_PENDING_STATE
from .delivery import DeliveryManager, DeliverySnapshot

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OpenDisplayConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the OpenDisplay update-pending binary sensor."""
    manager = entry.runtime_data.delivery
    if manager is None:
        return
    async_add_entities(
        [OpenDisplayUpdatePendingSensor(entry.unique_id or "", manager)]
    )


def _to_iso(epoch: float | None) -> str | None:
    """Convert an epoch timestamp to an ISO string, or None."""
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class OpenDisplayUpdatePendingSensor(BinarySensorEntity):
    """On while content is queued for delivery at the next wake.

    Backed entirely by the delivery manager's state, so it stays available and
    meaningful even while the device is dark.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "update_pending"

    def __init__(self, address: str, manager: DeliveryManager) -> None:
        """Initialize the binary sensor from the manager's current state."""
        self._address = address
        self._attr_unique_id = f"{address}-update_pending"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, address)},
        )
        self._apply(manager.state)

    @callback
    def _apply(self, snapshot: DeliverySnapshot) -> None:
        """Store the latest delivery snapshot on the entity."""
        self._attr_is_on = snapshot.pending
        self._queued_at = snapshot.queued_at
        self._expires_at = snapshot.expires_at
        self._attempts = snapshot.attempts
        self._last_error = snapshot.last_error

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose queue timing details for automations/dashboards."""
        return {
            "queued_at": _to_iso(self._queued_at),
            "expires_at": _to_iso(self._expires_at),
            "attempts": self._attempts,
            "last_error": self._last_error,
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to delivery state updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{SIGNAL_PENDING_STATE}_{self._address}",
                self._handle_pending_state,
            )
        )

    @callback
    def _handle_pending_state(self, snapshot: DeliverySnapshot) -> None:
        """Handle a delivery-state change."""
        self._apply(snapshot)
        self.async_write_ha_state()
