from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components import websocket_api
from homeassistant.components.frontend import DATA_PANELS
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.coselig_modbus_hub.const import DOMAIN
from custom_components.coselig_modbus_hub.hub import Hub
from custom_components.coselig_modbus_hub.models import Device, Model
from custom_components.coselig_modbus_hub.monitor import (
    async_register_monitor,
    websocket_clear,
    websocket_list,
    websocket_poll,
    websocket_subscribe,
)


@pytest.fixture
async def monitored_hub(hass, entry):
    entry.runtime_data = Hub.create(
        hass,
        "private-host",
        502,
        (Device(51, Model.P404), Device(63, Model.R410)),
        2,
    )
    yield entry.runtime_data
    await entry.runtime_data.stop()


@pytest.fixture
async def ws(hass, hass_ws_client, entry, monitored_hub):
    assert await async_setup_component(hass, "websocket_api", {})
    for handler in (
        websocket_list,
        websocket_subscribe,
        websocket_clear,
        websocket_poll,
    ):
        websocket_api.async_register_command(hass, handler)
    client = await hass_ws_client(hass)
    yield client
    await client.close()


async def test_live_trace_clear_and_unsubscribe_without_requests(
    hass, entry, monitored_hub, ws
):
    monitored_hub.coordinator.states[51].available = True
    monitored_hub.coordinator.states[63].consecutive_failures = 2
    monitored_hub.coordinator._round_times.extend((200.0, 230.0))
    statistics = monitored_hub.transport.statistics
    statistics.successful_requests = 15
    statistics.failed_requests = 4
    statistics.timeout_count = 3
    statistics.crc_error_count = 1
    statistics.reconnect_count = 2
    statistics.queue_peak = 24
    monitored_hub.transport.submit = AsyncMock(
        side_effect=AssertionError("Monitor must not submit Modbus requests")
    )
    await ws.send_json({"id": 1, "type": f"{DOMAIN}/monitor/list"})
    result = await ws.receive_json()
    assert result["result"] == [{"entry_id": entry.entry_id, "title": entry.title}]
    monitored_hub.transport.trace.record(
        "TX", bytes.fromhex("0103082a0002e790"), source="poll"
    )
    await ws.send_json(
        {"id": 2, "type": f"{DOMAIN}/monitor/subscribe", "entry_id": entry.entry_id}
    )
    assert (await ws.receive_json())["success"]
    snapshot = (await ws.receive_json())["event"]
    assert snapshot["poll_interval"] == 2
    assert not snapshot["polling_paused"]
    assert not snapshot["runtime_poll_paused"]
    assert snapshot["polling_state"] == "running"
    assert snapshot["coordinator"] == {
        "configured_slaves": 2,
        "available_slaves": 1,
        "last_poll_round_ms": 230.0,
        "average_poll_round_ms": 215.0,
        "queue_depth": 0,
        "queue_peak": 24,
    }
    assert snapshot["statistics"] == {
        "successful_requests": 15,
        "failed_requests": 4,
        "timeout_count": 3,
        "crc_error_count": 1,
        "reconnect_count": 2,
    }
    assert snapshot["devices"] == [
        {
            "slave": 51,
            "model": "p404",
            "group_a": "single",
            "group_b": "single",
            "available": True,
            "consecutive_failures": 0,
        },
        {
            "slave": 63,
            "model": "r410",
            "group_a": None,
            "group_b": None,
            "available": False,
            "consecutive_failures": 2,
        },
    ]
    assert snapshot["entries"][0]["raw_hex"] == "01 03 08 2A 00 02 E7 90"
    assert statistics.successful_requests == 15
    monitored_hub.transport.submit.assert_not_awaited()
    await ws.send_json(
        {"id": 3, "type": f"{DOMAIN}/monitor/clear", "entry_id": entry.entry_id}
    )
    assert (await ws.receive_json())["success"]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert (await ws.receive_json())["event"]["entries"] == []
    await ws.send_json({"id": 4, "type": "unsubscribe_events", "subscription": 2})
    assert (await ws.receive_json())["success"]
    assert statistics.successful_requests == 15
    monitored_hub.transport.submit.assert_not_awaited()


@pytest.mark.parametrize("command", ["subscribe", "clear", "poll"])
async def test_unloaded_or_other_entry_rejected(ws, command):
    message = {"id": 1, "type": f"{DOMAIN}/monitor/{command}", "entry_id": "missing"}
    if command == "poll":
        message["paused"] = True
    await ws.send_json(message)
    result = await ws.receive_json()
    assert not result["success"]
    assert result["error"]["code"] == "not_loaded"


@pytest.mark.parametrize("command", ["list", "subscribe", "clear", "poll"])
async def test_monitor_is_admin_only(
    hass, hass_ws_client, hass_read_only_access_token, command
):
    assert await async_setup_component(hass, "websocket_api", {})
    for handler in (
        websocket_list,
        websocket_subscribe,
        websocket_clear,
        websocket_poll,
    ):
        websocket_api.async_register_command(hass, handler)
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)
    message = {"id": 1, "type": f"{DOMAIN}/monitor/{command}"}
    if command != "list":
        message["entry_id"] = "any"
    if command == "poll":
        message["paused"] = True
    await client.send_json(message)
    assert (await client.receive_json())["error"]["code"] == "unauthorized"
    await client.close()


async def test_runtime_poll_control_is_per_entry_and_does_not_touch_options(
    hass, entry, monitored_hub, ws
):
    second = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        minor_version=1,
        data={"host": "other-gateway", "port": 502},
        options={"poll_interval": 2.0, "devices": [], "s402_devices": [{"id": 12}]},
        title="Second gateway",
    )
    second.add_to_hass(hass)
    second_hub = Hub.create(hass, "other-gateway", 502, (), 2.0, (12,))
    second.runtime_data = second_hub
    first_options, second_options = dict(entry.options), dict(second.options)
    first_stats = monitored_hub.transport.statistics
    second_stats = second_hub.transport.statistics

    await ws.send_json(
        {
            "id": 20,
            "type": f"{DOMAIN}/monitor/poll",
            "entry_id": entry.entry_id,
            "paused": True,
        }
    )
    result = await ws.receive_json()
    assert result["success"]
    assert result["result"] == {"runtime_poll_paused": True, "polling_state": "paused"}
    assert monitored_hub.coordinator.runtime_poll_paused
    assert not second_hub.coordinator.runtime_poll_paused

    await ws.send_json(
        {
            "id": 21,
            "type": f"{DOMAIN}/monitor/poll",
            "entry_id": second.entry_id,
            "paused": True,
        }
    )
    assert (await ws.receive_json())["success"]
    assert second_hub.coordinator.runtime_poll_paused
    assert monitored_hub.coordinator.runtime_poll_paused

    await ws.send_json(
        {
            "id": 22,
            "type": f"{DOMAIN}/monitor/poll",
            "entry_id": entry.entry_id,
            "paused": False,
        }
    )
    assert (await ws.receive_json())["success"]
    assert not monitored_hub.coordinator.runtime_poll_paused
    assert second_hub.coordinator.runtime_poll_paused
    assert dict(entry.options) == first_options
    assert dict(second.options) == second_options
    assert first_stats.successful_requests == second_stats.successful_requests == 0
    assert first_stats.failed_requests == second_stats.failed_requests == 0
    await second_hub.stop()


async def test_runtime_poll_control_cannot_override_poll_interval_zero(
    ws, entry, monitored_hub
):
    monitored_hub.coordinator.set_poll_interval(0)
    await ws.send_json(
        {
            "id": 23,
            "type": f"{DOMAIN}/monitor/poll",
            "entry_id": entry.entry_id,
            "paused": False,
        }
    )
    result = await ws.receive_json()
    assert not result["success"]
    assert result["error"]["code"] == "polling_disabled"
    assert not monitored_hub.coordinator.runtime_poll_paused


async def test_command_cannot_write_register(ws, entry):
    await ws.send_json(
        {
            "id": 1,
            "type": f"{DOMAIN}/monitor/clear",
            "entry_id": entry.entry_id,
            "register": 2090,
            "value": 80,
        }
    )
    assert not (await ws.receive_json())["success"]


async def test_register_versioned_static_panel(hass):
    hass.http = Mock(async_register_static_paths=AsyncMock())
    with patch(
        "custom_components.coselig_modbus_hub.monitor.websocket_api.async_register_command"
    ) as register:
        await async_register_monitor(hass)
    assert register.call_count == 4
    paths = hass.http.async_register_static_paths.call_args.args[0]
    assert paths[0].url_path == "/coselig_modbus_hub/monitor-1.4.0-beta.2.js"
    panel = hass.data[DATA_PANELS]["coselig-modbus-monitor"]
    assert panel.require_admin
    assert panel.config["_panel_custom"]["module_url"].endswith(
        "monitor-1.4.0-beta.2.js"
    )


async def test_subscription_survives_runtime_reload_and_no_change(
    hass, entry, monitored_hub, ws
):
    await ws.send_json(
        {"id": 1, "type": f"{DOMAIN}/monitor/subscribe", "entry_id": entry.entry_id}
    )
    await ws.receive_json()
    await ws.receive_json()
    # A no-change timer tick sends nothing; replacing runtime produces a bounded fresh snapshot.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    del entry.runtime_data
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2))
    await hass.async_block_till_done()
    unloaded = (await ws.receive_json())["event"]
    assert unloaded["poll_interval"] is None
    assert unloaded["coordinator"]["configured_slaves"] == 0
    assert unloaded["statistics"]["successful_requests"] == 0
    assert unloaded["devices"] == []
    entry.runtime_data = monitored_hub
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
    await hass.async_block_till_done()
    assert (await ws.receive_json())["event"]["poll_interval"] == 2
