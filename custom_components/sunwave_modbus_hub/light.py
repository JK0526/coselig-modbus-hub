"""Placeholder entity platform; MQTT bridge is added after transport validation."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Do not create entities until channels are configured."""
    # The first UI milestone is gateway setup. Channel registration and MQTT
    # Discovery will populate this platform in the next implementation step.
    async_add_entities([])
