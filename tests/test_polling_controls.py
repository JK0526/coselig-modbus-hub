import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import entity_registry as er

from custom_components.coselig_modbus_hub.hub import Hub
from custom_components.coselig_modbus_hub.models import Device, Model
from custom_components.coselig_modbus_hub.protocol import frame
from tests.dimmer_packets import write


@pytest.fixture
async def hub(hass, gateway):
    hub = Hub.create(hass, "127.0.0.1", gateway.port, (Device(1, Model.P404),), 0)
    await hub.start()  # one bootstrap read, not background polling
    yield hub
    await hub.stop()


async def test_zero_no_background_reads_no_failure_and_scan_available(hub, gateway):
    gateway.requests.clear()
    before = hub.coordinator.states[1].consecutive_failures
    await asyncio.sleep(0.06)
    assert gateway.requests == []
    assert hub.coordinator.states[1].available
    assert hub.coordinator.states[1].consecutive_failures == before
    assert hub.transport.connected
    assert [r.slave for r in await hub.scan(1, 2)] == [1, 2]
    await asyncio.sleep(0.02)
    assert [r[0] for r in gateway.requests] == [1, 1, 2, 2]
    assert all(e["source"] == "scan" for e in hub.transport.trace.snapshot()[-6:])


async def test_resume_immediate_poll_and_state_correction(hub, gateway):
    gateway.registers[1][0] = 65
    gateway.requests.clear()
    gateway.arrived.clear()
    hub.coordinator.set_poll_interval(2)
    await asyncio.wait_for(gateway.arrived.wait(), 1)
    async with hub.coordinator.pause():
        assert hub.coordinator.states[1].holding_registers[0x082A] == 65
    assert len(gateway.requests) == 1
    hub.coordinator.set_poll_interval(0)
    await asyncio.sleep(0.03)
    assert len(gateway.requests) == 1


async def test_native_options_zero_control_and_resume_no_reload(hass, entry, gateway):
    hass.config_entries.async_update_entry(
        entry,
        data={"host": "127.0.0.1", "port": gateway.port},
        options={
            **entry.options,
            "devices": [
                {"slave": 1, "model": "p404", "group_a": "cct", "group_b": "single"}
            ],
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    initial_hub = entry.runtime_data
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    target = next(e for e in entities if e.unique_id.endswith("group_a"))

    async def set_interval(interval):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "communication"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "polling"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"poll_interval": interval}
        )
        await hass.async_block_till_done()
        assert entry.runtime_data is initial_hub

    with patch.object(hass.config_entries, "async_reload", new=AsyncMock()) as reload:
        await set_interval(0)
        # Drain an already-issued poll; pausing does not cancel hardware I/O.
        async with initial_hub.coordinator.pause():
            pass
        gateway.requests.clear()
        await hass.services.async_call(
            "light",
            "turn_on",
            {
                "entity_id": target.entity_id,
                "brightness": 204,
                "color_temp_kelvin": 4000,
            },
            blocking=True,
        )
        await hass.services.async_call(
            "light", "turn_off", {"entity_id": target.entity_id}, blocking=True
        )
        await asyncio.sleep(0.03)
        assert [r[1] for r in gateway.requests] == [6, 6, 6]
        assert hass.states.get(target.entity_id).state == "off"
        assert initial_hub.coordinator.states[1].available
        gateway.registers[1][0] = 65
        gateway.arrived.clear()
        await set_interval(2)
        await asyncio.wait_for(gateway.arrived.wait(), 1)
        async with initial_hub.coordinator.pause():
            pass
        assert hass.states.get(target.entity_id).attributes["brightness"] == 166
        reload.assert_not_awaited()
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("interval", [-1, 0.1, 0.2, 0.49, float("nan"), float("inf")])
def test_reject_high_speed_and_invalid_interval(hass, interval):
    with pytest.raises(ValueError):
        Hub.create(hass, "test", 502, (), interval)
    hub = Hub.create(hass, "test", 502, (), 0)
    with pytest.raises(ValueError):
        hub.coordinator.set_poll_interval(interval)


async def test_round_timing_excludes_wait_and_rolling_average(hass):
    hub = Hub.create(hass, "test", 502, (Device(1, Model.P210),), 2)
    coordinator = hub.coordinator
    assert coordinator.last_poll_round_ms == 0
    assert coordinator.average_poll_round_ms == 0
    with patch.object(coordinator, "_poll_devices", new=AsyncMock()):
        for duration in range(1, 26):
            with patch.object(
                hass.loop, "time", side_effect=[100, 100 + duration / 1000]
            ):
                await coordinator.async_poll_round()
    assert coordinator.last_poll_round_ms == pytest.approx(25)
    assert coordinator.average_poll_round_ms == pytest.approx(15.5)
    assert len(coordinator._round_times) == 20
    await hub.stop()


async def test_paused_reconnect_does_not_issue_read(hub, gateway):
    hub.coordinator.gateway_lost()
    gateway.requests.clear()

    async def sleep(_delay):
        pass

    async def wait(_timeout=None):
        hub.coordinator._closed = True

    # Stop the parked scheduler so this deterministic exercise has one owner.
    await hub.coordinator.stop()
    hub.coordinator._closed = False
    with (
        patch(
            "custom_components.coselig_modbus_hub.coordinator.asyncio.sleep", new=sleep
        ),
        patch.object(hub.coordinator, "_wait_for_schedule", new=wait),
    ):
        await hub.coordinator._loop()
    assert hub.coordinator.gateway_available
    assert hub.coordinator.states[1].available
    assert gateway.requests == []


async def test_pause_while_round_inflight_does_not_submit_next_slave(hass, gateway):
    hub = Hub.create(
        hass,
        "127.0.0.1",
        gateway.port,
        (Device(1, Model.P404), Device(2, Model.P210)),
        2,
    )
    gateway.block = True
    with patch.object(
        type(hub.coordinator.devices[0].profile),
        "poll_specs",
        side_effect=lambda device: (device.profile.poll, device.profile.poll),
    ):
        task = asyncio.create_task(hub.coordinator.async_poll_round(background=True))
        await gateway.arrived.wait()
        hub.coordinator.set_runtime_poll_paused(True)
        gateway.release.set()
        await task
    await hub.coordinator.async_poll_round(background=True)
    assert [r[0] for r in gateway.requests] == [1]
    assert hub.coordinator.poll_interval == 2
    assert hub.coordinator.runtime_poll_paused
    await hub.stop()


async def test_runtime_pause_parks_scheduler_and_resume_has_no_backlog(hass, gateway):
    hub = Hub.create(hass, "127.0.0.1", gateway.port, (Device(1, Model.P404),), 0.5)
    await hub.start()
    gateway.requests.clear()
    gateway.arrived.clear()
    gateway.release.clear()
    gateway.block = True
    try:
        await asyncio.wait_for(gateway.arrived.wait(), 1)
        hub.coordinator.set_runtime_poll_paused(True)
        gateway.release.set()
        await asyncio.sleep(0.65)
        assert (
            len(gateway.requests) == 1
        )  # Current request completed; next spec/poll parked.
        assert hub.coordinator.poll_interval == 0.5

        gateway.block = True
        gateway.release.clear()
        gateway.arrived.clear()
        before_resume = len(gateway.requests)
        hub.coordinator.set_runtime_poll_paused(False)
        await asyncio.wait_for(gateway.arrived.wait(), 1)
        hub.coordinator.set_runtime_poll_paused(True)
        gateway.release.set()
        await asyncio.sleep(0.65)
        assert len(gateway.requests) == before_resume + 1
    finally:
        await hub.stop()


async def test_control_scanner_and_s402_events_remain_live_while_poll_paused(
    hass, gateway
):
    hub = Hub.create(
        hass,
        "127.0.0.1",
        gateway.port,
        (Device(1, Model.P404),),
        2,
        s402_ids=(7,),
    )
    await hub.start()
    seen = []
    event_received = asyncio.Event()
    hub.subscribe_s402(7, 1, lambda event: (seen.append(event), event_received.set()))
    hub.coordinator.set_runtime_poll_paused(True)
    try:
        gateway.requests.clear()
        await hub.coordinator.async_command(
            1, write(1, 0x082A, 88), (1, "ch1", "brightness")
        )
        assert [request[1] for request in gateway.requests] == [6]

        scan_results = await hub.scan(51, 51)
        assert [result.slave for result in scan_results] == [51]
        assert not hub.coordinator.scanning

        writer = next(writer for writer in gateway.writers if not writer.is_closing())
        writer.write(frame(bytes([1, 6, 1, 0xA0, 7, 1])))
        await writer.drain()
        await asyncio.wait_for(event_received.wait(), 1)
        assert len(seen) == 1
        assert hub.coordinator.runtime_poll_paused
    finally:
        await hub.stop()


async def test_schedule_wait_timeout_does_not_change_health(hub):
    await hub.coordinator._wait_for_schedule(0.001)
    assert hub.coordinator.states[1].consecutive_failures == 0
    assert hub.coordinator.states[1].available


async def test_options_scan_cancel_keeps_paused_runtime_and_control(
    hass, entry, gateway
):
    hass.config_entries.async_update_entry(
        entry,
        data={"host": "127.0.0.1", "port": gateway.port},
        options={**entry.options, "poll_interval": 0, "scan_start": 51, "scan_end": 80},
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hub = entry.runtime_data
    gateway.requests.clear()
    gateway.arrived.clear()
    gateway.block = True
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "device_management"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_device"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "search_modbus"}
    )
    assert result["data_schema"]({}) == {"scan_start": 51, "scan_end": 80}
    assert not gateway.requests
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"scan_start": 60, "scan_end": 74}
    )
    await asyncio.wait_for(gateway.arrived.wait(), 1)
    hass.config_entries.options.async_abort(result["flow_id"])
    gateway.release.set()
    await hass.async_block_till_done()
    assert entry.runtime_data is hub
    assert not hub.coordinator.scanning
    assert entry.options["scan_start"] == 51
    target = next(
        e
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.unique_id.endswith("_slave_1_ch1")
    )
    await hass.services.async_call(
        "light",
        "turn_on",
        {"entity_id": target.entity_id, "brightness": 204},
        blocking=True,
    )
    assert [(r[0], r[1]) for r in gateway.requests] == [(60, 3), (1, 6)]
    assert await hass.config_entries.async_unload(entry.entry_id)
