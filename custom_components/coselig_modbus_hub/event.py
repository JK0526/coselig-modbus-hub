"""Home Assistant event entities for S402 momentary scene inputs."""

from __future__ import annotations

from homeassistant.components.event import (
    ButtonEventType,
    EventDeviceClass,
    EventEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import CoseligConfigEntry
from .const import DOMAIN
from .coordinator import Coordinator
from .hub import Hub
from .s402 import S402Event


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CoseligConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create four stateless event entities for each configured S402."""
    hub = entry.runtime_data
    entities = [
        S402DIEvent(hub, entry.entry_id, device_id, di)
        for device_id in hub.s402_ids
        for di in range(1, 5)
    ]
    async_add_entities(entities)


class S402DIEvent(CoordinatorEntity[Coordinator], EventEntity):
    """One single-click event entity for a physical S402 DI terminal."""

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = [ButtonEventType.PRESS_END]
    _attr_has_entity_name = True

    def __init__(self, hub: Hub, entry_id: str, device_id: int, di: int) -> None:
        super().__init__(hub.coordinator)
        self._hub = hub
        self._device_id = device_id
        self._di = di
        self._attr_name = f"DI{di}"
        self._attr_unique_id = f"{entry_id}_s402_{device_id}_di{di}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_s402_{device_id}")},
            name=f"Coselig S402 #{device_id}",
            manufacturer="Coselig",
            model="S402",
        )

    @property
    def available(self) -> bool:
        """An event input is available exactly while its gateway is connected."""
        return self.coordinator.gateway_available

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._hub.subscribe_s402(self._device_id, self._di, self._handle_event)
        )

    @callback
    def _handle_event(self, event: S402Event) -> None:
        if event.device_id != self._device_id or event.di != self._di:
            return
        self._trigger_event(ButtonEventType.PRESS_END)
        self.async_write_ha_state()
