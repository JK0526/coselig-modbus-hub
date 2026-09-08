"""Home Assistant MQTT adapter for the pure Discovery/topic layer."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant

from .mqtt_bridge import (
    availability_message,
    clear_message,
    discovery_message,
    entity_base,
)


CommandHandler = Callable[[Mapping[str, object], str, object], Awaitable[None]]


class MqttBridge:
    """Publish Discovery/state and route the three supported command topics."""

    def __init__(
        self,
        hass: HomeAssistant,
        hub_id: str,
        channels: list[Mapping[str, object]],
        command_handler: CommandHandler,
        *,
        topic_prefix: str = "coselig",
    ) -> None:
        self.hass = hass
        self.hub_id = hub_id
        self.channels = channels
        self.command_handler = command_handler
        self.topic_prefix = topic_prefix
        self._by_topic = {
            entity_base(channel, hub_id, topic_prefix=topic_prefix): channel
            for channel in channels
        }
        self._unsubscribers: list[Callable[[], None]] = []

    async def async_start(self) -> None:
        """Wait for MQTT, publish retained configuration and subscribe."""
        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            raise RuntimeError("Home Assistant MQTT client is not available")
        for channel in self.channels:
            topic, payload = discovery_message(
                channel,
                self.hub_id,
                topic_prefix=self.topic_prefix,
            )
            await mqtt.async_publish(self.hass, topic, payload, qos=0, retain=True)
            base = entity_base(
                channel,
                self.hub_id,
                topic_prefix=self.topic_prefix,
            )
            for suffix in ("set", "set/brightness", "set/colortemp"):
                self._unsubscribers.append(
                    await mqtt.async_subscribe(
                        self.hass, f"{base}/{suffix}", self._async_message_received, qos=0
                    )
                )
        await self._publish_availability(True)

    async def _publish_availability(self, online: bool) -> None:
        topic, payload = availability_message(self.hub_id, online, topic_prefix=self.topic_prefix)
        await mqtt.async_publish(self.hass, topic, payload, qos=0, retain=True)

    async def async_publish_state(self, channel: Mapping[str, object], states: Mapping[str, object]) -> None:
        """Publish state values as retained messages, never as commands."""
        for topic, payload in states.items():
            await mqtt.async_publish(self.hass, topic, str(payload), qos=0, retain=True)

    async def _async_message_received(self, message: Any) -> None:
        """Translate an MQTT command message into a runtime callback."""
        topic = str(message.topic)
        for base, channel in self._by_topic.items():
            if topic == f"{base}/set":
                await self.command_handler(channel, "state", message.payload)
                return
            if topic == f"{base}/set/brightness":
                await self.command_handler(channel, "brightness", message.payload)
                return
            if topic == f"{base}/set/colortemp":
                await self.command_handler(channel, "colortemp", message.payload)
                return

    async def async_remove_channel(self, channel: Mapping[str, object]) -> None:
        """Remove retained Discovery for a channel."""
        topic, payload = clear_message(
            channel, self.hub_id, topic_prefix=self.topic_prefix
        )
        await mqtt.async_publish(self.hass, topic, payload, qos=0, retain=True)

    async def async_stop(self) -> None:
        """Publish offline and unsubscribe all command listeners."""
        await self._publish_availability(False)
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
