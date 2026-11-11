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
from .optimistic import OptimisticValue, wait_controls
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
        self._brightness_state = OptimisticValue(0)
        self._remembered_brightness = OptimisticValue(255)
        self._temperature_state = OptimisticValue[int | None](None)
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
        brightness = raw_to_brightness(raw)
        self._brightness_state.observe(brightness)
        if brightness:
            self._remembered_brightness.observe(brightness)
        if self.output.cct:
            self._temperature_state.observe(
                raw_to_kelvin(state.holding_registers[self.output.registers[1]])
            )
        self._apply_optimistic_values()

    def _apply_optimistic_values(self) -> None:
        self._attr_brightness = self._brightness_state.value
        self._last_brightness = self._remembered_brightness.value
        self._attr_is_on = self._attr_brightness > 0
        self._attr_color_temp_kelvin = self._temperature_state.value

    def _publish_optimistic(self) -> None:
        self._apply_optimistic_values()
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._apply_poll()
        self.async_write_ha_state()

    async def _set_output(self, raw: int, temperature: int | None = None) -> None:
        if not self.coordinator.can_control(self.device.slave):
            raise HomeAssistantError("Coselig device unavailable or scanning")
        slave = self.device.slave
        fields = [(self.output.registers[0], raw, "brightness")]
        if temperature is not None and self.output.cct:
            fields.append(
                (self.output.registers[1], kelvin_to_raw(temperature), "temperature")
            )
        try:
            items = await self.coordinator.async_queue_controls(
                slave,
                tuple(
                    (
                        self.device.profile.command(slave, register, value),
                        (slave, self.output.key, field),
                    )
                    for register, value, field in fields
                ),
            )
            self._optimistic_at = asyncio.get_running_loop().time()
            brightness = raw_to_brightness(raw)
            self._brightness_state.stage(brightness, items[0], self._publish_optimistic)
            if brightness:
                self._remembered_brightness.stage(
                    brightness, items[0], self._apply_optimistic_values
                )
            if len(items) == 2:
                self._temperature_state.stage(
                    raw_to_kelvin(fields[1][1]), items[1], self._publish_optimistic
                )
            self._publish_optimistic()
            await wait_controls(items)
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
        brightness = kwargs.get(ATTR_BRIGHTNESS, self._last_brightness)
        await self._set_output(
            brightness_to_raw(brightness), kwargs.get(ATTR_COLOR_TEMP_KELVIN)
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_output(0)
