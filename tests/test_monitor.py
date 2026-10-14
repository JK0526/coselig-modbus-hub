from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components import websocket_api
from homeassistant.components.frontend import DATA_PANELS
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.coselig_modbus_hub.const import DOMAIN
from custom_components.coselig_modbus_hub.hub import Hub
from custom_components.coselig_modbus_hub.monitor import (
    async_register_monitor,
    websocket_clear,
    websocket_list,
    websocket_subscribe,
)


@pytest.fixture
async def monitored_hub(hass, entry):
    entry.runtime_data = Hub.create(hass, "private-host", 502, (), 0)
    yield entry.runtime_data
    await entry.runtime_data.stop()


@pytest.fixture
async def ws(hass, hass_ws_client, entry, monitored_hub):
    assert await async_setup_component(hass, "websocket_api", {})
    for handler in (websocket_list, websocket_subscribe, websocket_clear):
        websocket_api.async_register_command(hass, handler)
    client = await hass_ws_client(hass)
    yield client
    await client.close()


async def test_live_trace_clear_and_unsubscribe_without_requests(
    hass, entry, monitored_hub, ws
):
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
    assert snapshot["poll_interval"] == 0
    assert snapshot["polling_paused"]
    assert snapshot["entries"][0]["raw_hex"] == "01 03 08 2A 00 02 E7 90"
    assert monitored_hub.transport.statistics.successful_requests == 0
    await ws.send_json(
        {"id": 3, "type": f"{DOMAIN}/monitor/clear", "entry_id": entry.entry_id}
    )
    assert (await ws.receive_json())["success"]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert (await ws.receive_json())["event"]["entries"] == []
    await ws.send_json({"id": 4, "type": "unsubscribe_events", "subscription": 2})
    assert (await ws.receive_json())["success"]
    assert monitored_hub.transport.statistics.successful_requests == 0


@pytest.mark.parametrize("command", ["subscribe", "clear"])
async def test_unloaded_or_other_entry_rejected(ws, command):
    await ws.send_json(
        {"id": 1, "type": f"{DOMAIN}/monitor/{command}", "entry_id": "missing"}
    )
    result = await ws.receive_json()
    assert not result["success"]
    assert result["error"]["code"] == "not_loaded"


@pytest.mark.parametrize("command", ["list", "subscribe", "clear"])
async def test_monitor_is_admin_only(
    hass, hass_ws_client, hass_read_only_access_token, command
):
    assert await async_setup_component(hass, "websocket_api", {})
    for handler in (websocket_list, websocket_subscribe, websocket_clear):
        websocket_api.async_register_command(hass, handler)
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)
    message = {"id": 1, "type": f"{DOMAIN}/monitor/{command}"}
    if command != "list":
        message["entry_id"] = "any"
    await client.send_json(message)
    assert (await client.receive_json())["error"]["code"] == "unauthorized"
    await client.close()


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
    assert register.call_count == 3
    paths = hass.http.async_register_static_paths.call_args.args[0]
    assert paths[0].url_path == "/coselig_modbus_hub/monitor-1.2.0.js"
    panel = hass.data[DATA_PANELS]["coselig-modbus-monitor"]
    assert panel.require_admin
    assert panel.config["_panel_custom"]["module_url"].endswith("monitor-1.2.0.js")


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
    assert (await ws.receive_json())["event"]["poll_interval"] is None
    entry.runtime_data = monitored_hub
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3))
    await hass.async_block_till_done()
    assert (await ws.receive_json())["event"]["poll_interval"] == 0
