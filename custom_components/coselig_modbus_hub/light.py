"""Native optimistic light entities with authoritative coordinator correction."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
)
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import ChildDeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import CoseligConfigEntry, ensure_parent_device
from .const import DOMAIN, MIRED_MAX, MIRED_MIN
from .coordinator import Coordinator
from .models import (
    Device,
    brightness_to_raw,
    kelvin_to_raw,
    raw_to_brightness,
    raw_to_kelvin,
)
from .profiles import OutputSpec
from .protocol import ProtocolError


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CoseligConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    entities = []
    for device in coordinator.devices:
        parent = ensure_parent_device(hass, entry, device)
        entities.extend(
            CoseligLight(
                coordinator,
                entry.entry_id,
                device,
                output,
                parent.id,
            )
            for output in device.outputs
            if output.platform == Platform.LIGHT
        )
    async_add_entities(entities)


class CoseligLight(CoordinatorEntity[Coordinator], LightEntity):
    _attr_has_entity_name = True

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
        # The child device carries the complete user-facing default name.  Leaving
        # the entity unnamed lets HA render that name once instead of appending a
        # second ``CH1``/``雙色溫 A`` suffix.
        self._attr_name = None
        self._attr_unique_id = output.unique_id(entry_id, device.slave)
        self._attr_device_info: ChildDeviceInfo = ChildDeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            name=f"{device.model.value.upper()} #{device.slave} · {output.name}",
            parent_device_id=parent_device_id,
        )
        mode = ColorMode.COLOR_TEMP if output.cct else ColorMode.BRIGHTNESS
        self._attr_supported_color_modes = {mode}
        self._attr_color_mode = mode
        self._attr_min_color_temp_kelvin = round(1_000_000 / MIRED_MAX)
        self._attr_max_color_temp_kelvin = round(1_000_000 / MIRED_MIN)
        self._optimistic_at = -1.0
        self._last_poll = -1.0
        self._last_brightness = 255
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
            not state.holding_registers
            or state.poll_started <= self._last_poll
            or state.poll_started < self._optimistic_at
        ):
            return
        self._last_poll = state.poll_started
        raw = state.holding_registers[self.output.registers[0]]
        self._attr_brightness = raw_to_brightness(raw)
        self._attr_is_on = raw > 0
        if raw:
            self._last_brightness = self._attr_brightness
        if self.output.cct:
            self._attr_color_temp_kelvin = raw_to_kelvin(
                state.holding_registers[self.output.registers[1]]
            )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._apply_poll()
        self.async_write_ha_state()

    async def _command(self, register: int, value: int, command_type: str) -> None:
        try:
            await self.coordinator.async_command(
                self.device.slave,
                self.device.profile.command(self.device.slave, register, value),
                (self.device.slave, self.output.key, command_type),
            )
        except (
            ConnectionError,
            TimeoutError,
            ProtocolError,
            asyncio.QueueFull,
        ) as error:
            # Keep optimistic values until a genuine later poll; never replay.
            raise HomeAssistantError(
                "Coselig command failed; awaiting device polling"
            ) from error

    async def async_turn_on(self, **kwargs: Any) -> None:
        if not self.available or self.coordinator.scanning:
            raise HomeAssistantError("Coselig device unavailable or scanning")
        brightness = kwargs.get(ATTR_BRIGHTNESS, self._last_brightness)
        raw = brightness_to_raw(brightness)
        temperature = kwargs.get(ATTR_COLOR_TEMP_KELVIN)
        self._optimistic_at = asyncio.get_running_loop().time()
        self._attr_brightness = raw_to_brightness(raw)
        self._attr_is_on = raw > 0
        if raw:
            self._last_brightness = self._attr_brightness
        if temperature is not None and self.output.cct:
            self._attr_color_temp_kelvin = raw_to_kelvin(kelvin_to_raw(temperature))
        self.async_write_ha_state()  # Before yielding to any bus I/O.
        commands = [
            asyncio.create_task(
                self._command(self.output.registers[0], raw, "brightness"),
                eager_start=True,
            )
        ]
        if temperature is not None and self.output.cct:
            commands.append(
                asyncio.create_task(
                    self._command(
                        self.output.registers[1],
                        kelvin_to_raw(temperature),
                        "temperature",
                    ),
                    eager_start=True,
                )
            )
        # Eager enqueue preserves action order, including ON then OFF in one loop tick.
        # Both CCT fields are pending before yielding to the next HA action.
        results = await asyncio.gather(*commands, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def async_turn_off(self, **kwargs: Any) -> None:
        if not self.available or self.coordinator.scanning:
            raise HomeAssistantError("Coselig device unavailable or scanning")
        self._optimistic_at = asyncio.get_running_loop().time()
        self._attr_is_on = False
        self._attr_brightness = 0
        self.async_write_ha_state()
        await self._command(self.output.registers[0], 0, "brightness")
