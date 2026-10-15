"""Native R410 relays using the shared scheduler and authoritative poll state."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import ChildDeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import CoseligConfigEntry, ensure_parent_device
from .const import DOMAIN
from .coordinator import Coordinator
from .models import Device
from .profiles import OutputSpec
from .protocol import ProtocolError
from .r410_cover import occupied_relays


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CoseligConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entities = []
    for device in entry.runtime_data.coordinator.devices:
        if Platform.SWITCH not in device.profile.supported_platforms:
            continue
        parent = ensure_parent_device(hass, entry, device)
        cover_relays = occupied_relays(entry.runtime_data.r410_covers, device.slave)
        entities.extend(
            CoseligSwitch(
                entry.runtime_data.coordinator,
                entry.entry_id,
                device,
                output,
                parent.id,
            )
            for output in device.outputs
            if output.platform == Platform.SWITCH
            and output.registers[0] + 1 not in cover_relays
        )
    async_add_entities(entities)


class CoseligSwitch(CoordinatorEntity[Coordinator], SwitchEntity):
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self,
        coordinator: Coordinator,
        entry_id: str,
        device: Device,
        output: OutputSpec,
        parent_device_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.device, self.output = device, output
        self._attr_unique_id = output.unique_id(entry_id, device.slave)
        self._attr_device_info = ChildDeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            name=f"{device.model.value.upper()} #{device.slave} · {output.name}",
            parent_device_id=parent_device_id,
        )
        self._optimistic_at = -1.0
        self._last_poll = -1.0
        self._apply_poll()

    @property
    def available(self) -> bool:
        return (
            self.coordinator.gateway_available
            and self.coordinator.states[self.device.slave].available
        )

    def _apply_poll(self) -> None:
        state = self.coordinator.states[self.device.slave]
        if (
            not state.coils
            or state.poll_started <= self._last_poll
            or state.poll_started < self._optimistic_at
        ):
            return
        self._last_poll = state.poll_started
        self._attr_is_on = state.coils[self.output.registers[0]]

    @callback
    def _handle_coordinator_update(self) -> None:
        self._apply_poll()
        self.async_write_ha_state()

    async def _set_state(self, value: bool) -> None:
        if not self.available or self.coordinator.scanning:
            raise HomeAssistantError("Coselig device unavailable or scanning")
        self._optimistic_at = asyncio.get_running_loop().time()
        self._attr_is_on = value
        self.async_write_ha_state()
        try:
            await self.coordinator.async_command(
                self.device.slave,
                self.device.profile.command(
                    self.device.slave, self.output.registers[0], value
                ),
                (self.device.slave, self.output.key, "relay"),
            )
        except (
            ConnectionError,
            TimeoutError,
            ProtocolError,
            asyncio.QueueFull,
        ) as error:
            raise HomeAssistantError(
                "Coselig command failed; awaiting device polling"
            ) from error

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_state(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_state(False)
