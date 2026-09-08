"""Runtime state and polling coordination for one configured gateway."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .const import DEFAULT_POLL_INTERVAL
from .devices import Channel, channel_from_mapping, validate_channels
from .mqtt_bridge import entity_base
from .protocol import ProtocolError, read
from .transport import Transport

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


@dataclass
class RuntimeState:
    connected: bool = False
    polling_enabled: bool = True
    poll_interval: float = DEFAULT_POLL_INTERVAL
    queue_depth: int = 0
    last_error: str | None = None
    success_count: int = 0
    timeout_count: int = 0
    crc_error_count: int = 0


@dataclass
class HubRuntime:
    """Owns transport lifetime; entity code never opens sockets directly."""

    host: str
    port: int
    timeout: float
    polling_enabled: bool = True
    poll_interval: float = DEFAULT_POLL_INTERVAL
    slave_ids: list[int] = field(default_factory=list)
    channels: list[dict[str, object]] = field(default_factory=list)
    hass: "HomeAssistant | None" = None
    hub_id: str = "default"

    def __post_init__(self):
        self.state = RuntimeState(
            connected=False,
            polling_enabled=self.polling_enabled,
            poll_interval=self.poll_interval,
        )
        self.transport = Transport(self.host, self.port, self.timeout)
        self._channel_objects = [channel_from_mapping(value) for value in self.channels]
        validate_channels(self._channel_objects)
        self.slave_ids = sorted({channel.slave for channel in self._channel_objects}) or list(self.slave_ids)
        self.mqtt_bridge = None
        self._poll_task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def async_start(self):
        if self.hass is not None and self._channel_objects:
            from .mqtt_runtime import MqttBridge

            self.mqtt_bridge = MqttBridge(
                self.hass,
                self.hub_id,
                self.channels,
                self.async_handle_command,
            )
            await self.mqtt_bridge.async_start()
        if self.state.polling_enabled and self.slave_ids:
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self):
        while not self._stop.is_set():
            for slave in tuple(dict.fromkeys(self.slave_ids)):
                if self._stop.is_set() or not self.state.polling_enabled:
                    break
                try:
                    self.state.queue_depth = self.transport.queue.qsize()
                    count = max(
                        (channel.poll_count for channel in self._channel_objects if channel.slave == slave),
                        default=4,
                    )
                    response = await self.transport.submit(read(slave, count), poll=True)
                    self.state.queue_depth = self.transport.queue.qsize()
                    self.state.connected = True
                    self.state.success_count += 1
                    self.state.last_error = None
                    registers = [
                        int.from_bytes(response[index : index + 2], "big")
                        for index in range(3, 3 + response[2], 2)
                    ]
                    if self.mqtt_bridge is not None:
                        for channel in self._channel_objects:
                            if channel.slave != slave:
                                continue
                            mapping = {
                                "model": channel.model,
                                "slave": channel.slave,
                                "channel": channel.channel,
                                "kind": channel.kind,
                                "name": channel.name,
                                "minimum": channel.minimum,
                                "mired_min": channel.mired_min,
                                "mired_max": channel.mired_max,
                            }
                            states = channel.states(registers)
                            old_base = channel.topic
                            new_base = entity_base(mapping, self.hub_id)
                            await self.mqtt_bridge.async_publish_state(
                                channel,
                                {topic.replace(old_base, new_base): value for topic, value in states.items()},
                            )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.state.connected = False
                    self.state.last_error = str(error)
                    if isinstance(error, TimeoutError):
                        self.state.timeout_count += 1
                    elif isinstance(error, ProtocolError):
                        self.state.crc_error_count += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.state.poll_interval)
            except TimeoutError:
                continue

    async def async_set_polling(self, enabled: bool):
        self.state.polling_enabled = enabled
        if enabled and self._poll_task is None:
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def async_set_poll_interval(self, interval: float):
        if interval < 0.5:
            raise ValueError("Poll interval must be at least 0.5 seconds")
        self.state.poll_interval = interval

    async def async_handle_command(
        self, channel_data: dict[str, object], action: str, payload: object
    ) -> None:
        """Convert one MQTT command into a validated Modbus write."""
        channel = channel_from_mapping(channel_data)
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="strict")
        if action == "state":
            state = str(payload).upper()
            if state not in {"ON", "OFF"}:
                raise ValueError("State must be ON or OFF")
            request = channel.brightness_command(100 if state == "ON" else 0)
        elif action == "brightness":
            request = channel.brightness_command(int(str(payload)))
        elif action == "colortemp":
            request = channel.temperature_command(int(str(payload)))
        else:
            raise ValueError("Unsupported MQTT action")
        await self.transport.submit(request)
        self.state.queue_depth = self.transport.queue.qsize()

    async def async_stop(self):
        self._stop.set()
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        if self.mqtt_bridge is not None:
            await self.mqtt_bridge.async_stop()
            self.mqtt_bridge = None
        await self.transport.close()
