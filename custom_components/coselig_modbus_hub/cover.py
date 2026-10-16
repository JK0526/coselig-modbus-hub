"""Command-only native covers using R410 hardware momentary relays."""

from __future__ import annotations

import asyncio

from homeassistant.components.cover import CoverEntity, CoverEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import CoseligConfigEntry
from .coordinator import Coordinator
from .protocol import ProtocolError
from .r410_cover import R410CoverConfig, cover_device_identifier


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CoseligConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Expose configured Covers directly on their physical R410 device."""
    async_add_entities(
        CoseligR410Cover(entry.runtime_data.coordinator, entry.entry_id, config)
        for config in entry.runtime_data.r410_covers
    )


class CoseligR410Cover(CoordinatorEntity[Coordinator], CoverEntity):
    """A momentary-command Cover with assumed command state and no feedback."""

    _attr_has_entity_name = True
    _attr_assumed_state = True
    _attr_supported_features = (
        CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
    )
    _attr_current_cover_position = None
    _attr_is_closed = None
    _attr_is_opening = None
    _attr_is_closing = None

    def __init__(
        self, coordinator: Coordinator, entry_id: str, config: R410CoverConfig
    ) -> None:
        super().__init__(coordinator)
        self.config = config
        self._attr_unique_id = config.unique_id(entry_id)
        self._attr_name = config.name
        self._attr_device_class = config.device_class
        self._attr_device_info = DeviceInfo(
            identifiers={cover_device_identifier(entry_id, config.slave)}
        )
        self._last_available = self.available

    @property
    def available(self) -> bool:
        return (
            self.coordinator.gateway_available
            and self.coordinator.states[self.config.slave].available
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Drop assumed state across any loss and recovery of availability."""
        available = self.available
        if not available or not self._last_available:
            self._attr_is_closed = None
        self._last_available = available
        self.async_write_ha_state()

    async def _action(self, action: str) -> None:
        if not self.available or self.coordinator.scanning:
            raise HomeAssistantError("Coselig R410 unavailable or scanning")
        try:
            await self.coordinator.async_action_command(
                self.config.slave, self.config.request(action)
            )
        except (
            ConnectionError,
            TimeoutError,
            ProtocolError,
            asyncio.QueueFull,
        ) as error:
            raise HomeAssistantError(
                "Coselig R410 momentary command failed; no automatic retry was sent"
            ) from error
        if action == "open":
            self._attr_is_closed = False
        elif action == "close":
            self._attr_is_closed = True
        else:
            self._attr_is_closed = None
        self.async_write_ha_state()

    async def async_open_cover(self, **kwargs: object) -> None:
        await self._action("open")

    async def async_close_cover(self, **kwargs: object) -> None:
        await self._action("close")

    async def async_stop_cover(self, **kwargs: object) -> None:
        await self._action("stop")
