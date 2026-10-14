import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.coselig_modbus_hub.config_flow import (
    ConfigFlow,
    CoseligOptionsFlow,
    valid_host,
    validate_gateway,
)
from custom_components.coselig_modbus_hub.const import DOMAIN
from custom_components.coselig_modbus_hub.hub import Hub
from custom_components.coselig_modbus_hub.models import Device, Model
from custom_components.coselig_modbus_hub.transport import GatewayError


@pytest.fixture
def flow(hass):
    flow = ConfigFlow()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.flow_id = "test-flow"
    flow.context = {"source": "user"}
    return flow


@pytest.fixture
def options(hass, entry):
    flow = CoseligOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    flow.flow_id = "test-options"
    return flow


@pytest.mark.parametrize(
    "host,expected",
    [
        ("  GATEWAY.local. ", "gateway.local"),
        ("192.168.1.2", "192.168.1.2"),
        ("::1", "::1"),
    ],
)
def test_host(host, expected):
    assert valid_host(host) == expected


@pytest.mark.parametrize(
    "host", ["", "bad host", "-bad", "a" * 64 + ".local", "x" * 254]
)
def test_invalid_host(host):
    with pytest.raises(ValueError):
        valid_host(host)


async def test_tcp_validation_real(gateway):
    assert await validate_gateway({"host": "127.0.0.1", "port": gateway.port}) == {
        "host": "127.0.0.1",
        "port": gateway.port,
    }


async def test_user_error_retry_and_progress(flow):
    result = await flow.async_step_user()
    assert result["step_id"] == "user"
    assert set(key.schema for key in result["data_schema"].schema) == {"host", "port"}
    with patch(
        "custom_components.coselig_modbus_hub.config_flow.validate_gateway",
        new=AsyncMock(side_effect=ValueError),
    ):
        assert (await flow.async_step_user({"host": "bad host", "port": 502}))[
            "errors"
        ] == {"host": "invalid_host"}
    with patch(
        "custom_components.coselig_modbus_hub.config_flow.validate_gateway",
        new=AsyncMock(side_effect=GatewayError),
    ):
        assert (await flow.async_step_user({"host": "test", "port": 502}))[
            "errors"
        ] == {"base": "cannot_connect"}
    release = asyncio.Event()

    async def scan(*args):
        await release.wait()
        return [1, 3]

    with (
        patch(
            "custom_components.coselig_modbus_hub.config_flow.validate_gateway",
            new=AsyncMock(return_value={"host": "test", "port": 502}),
        ),
        patch("custom_components.coselig_modbus_hub.config_flow.scan", new=scan),
    ):
        result = await flow.async_step_user({"host": "test", "port": 502})
        assert result["step_id"] == "scan_range"
        assert flow._scan_task is None
        result = await flow.async_step_scan_range({"scan_start": 1, "scan_end": 247})
        assert result["type"] == FlowResultType.SHOW_PROGRESS
        assert (await flow.async_step_scan())["progress_task"] is flow._scan_task
        flow._scan_progress(2, 32, 1)
        assert flow._progress == {"current": "2", "total": "32", "found": "1"}
        release.set()
        await flow._scan_task
        assert (await flow.async_step_scan())["step_id"] == "scan_result"
    assert (await flow.async_step_scan_result())["step_id"] == "scan_result"
    result = await flow.async_step_scan_result({"devices": ["1", "3"]})
    assert result["step_id"] == "model"
    assert result["description_placeholders"]["slave"] == "01"
    assert (await flow.async_step_model({"model": "p404"}))["step_id"] == "model"
    assert (await flow.async_step_model({"model": "p210"}))["step_id"] == "groups"
    result = await flow.async_step_groups({"group_a": "single", "group_b": "single"})
    assert result["step_id"] == "groups"
    result = await flow.async_step_groups({"group_a": "single"})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == {"host": "test", "port": 502}
    assert result["options"]["devices"] == [
        {"slave": 1, "model": "p404", "group_a": "single", "group_b": "single"},
        {"slave": 3, "model": "p210", "group_a": "single"},
    ]
    assert result["version"] == 2
    assert result["minor_version"] == 1


@pytest.mark.parametrize("failure", [False, True])
async def test_empty_or_disconnected_scan_retry(flow, failure):
    flow._gateway = {"host": "test", "port": 502}
    with patch(
        "custom_components.coselig_modbus_hub.config_flow.scan",
        new=AsyncMock(side_effect=GatewayError() if failure else None, return_value=[]),
    ):
        await flow.async_step_scan()
        await asyncio.gather(flow._scan_task, return_exceptions=True)
        assert (await flow.async_step_scan())["step_id"] == "scan_failed"
    result = await flow.async_step_scan_failed()
    assert result["menu_options"] == ["rescan", "scan_range", "cancel"]
    with patch(
        "custom_components.coselig_modbus_hub.config_flow.scan",
        new=AsyncMock(return_value=[7]),
    ):
        assert (await flow.async_step_rescan())["step_id"] == "scan_range"
        assert flow._scan_task is None
        await flow.async_step_scan_range({"scan_start": 1, "scan_end": 247})
        await flow._scan_task
        assert (await flow.async_step_scan())["step_id"] == "scan_result"
    await flow.async_step_scan_result({"devices": ["7"]})
    result = await flow.async_step_model({"model": "p210"})
    result = await flow.async_step_groups({"group_a": "single"})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["options"]["devices"] == [
        {"slave": 7, "model": "p210", "group_a": "single"}
    ]


async def test_range_validation_and_cancellation(flow):
    assert (await flow.async_step_scan_range())["step_id"] == "scan_range"
    assert (await flow.async_step_scan_range({"scan_start": 20, "scan_end": 1}))[
        "errors"
    ] == {"base": "invalid_range"}
    flow._gateway = {"host": "test", "port": 502}
    release = asyncio.Event()
    with patch(
        "custom_components.coselig_modbus_hub.config_flow.scan",
        new=AsyncMock(side_effect=lambda *args: None),
    ):

        async def scan(*args):
            await release.wait()

        with patch("custom_components.coselig_modbus_hub.config_flow.scan", new=scan):
            assert (
                await flow.async_step_scan_range({"scan_start": 33, "scan_end": 247})
            )["type"] == FlowResultType.SHOW_PROGRESS
            await asyncio.sleep(0)
            task = flow._scan_task
            flow.async_remove()
            await asyncio.gather(task, return_exceptions=True)
            assert task.cancelled()
    assert flow._options["scan_start"] == 33
    assert (await flow.async_step_cancel())["reason"] == "user_cancelled"
    flow._scan_task = None
    flow.async_remove()


async def test_options_polling_only(options):
    assert (await options.async_step_init())["menu_options"] == [
        "polling",
        "manage_devices",
        "scan_range",
    ]
    form = await options.async_step_polling()
    assert form["data_schema"]({"poll_interval": 0.5})["poll_interval"] == 0.5
    assert (await options.async_step_polling({"poll_interval": 0.4}))["errors"] == {
        "poll_interval": "invalid_poll_interval"
    }
    result = await options.async_step_polling({"poll_interval": 1.0})
    assert result["data"]["poll_interval"] == 1
    assert len(result["data"]["devices"]) == 2


async def test_manage_preserve_missing_add_new(options):
    await options.async_step_init()
    options._found = [1, 7]
    form = await options.async_step_scan_result()
    choices = next(iter(form["data_schema"].schema.values())).config["options"]
    assert [choice["label"] for choice in choices] == [
        "Slave 01 P404 — Existing",
        "Slave 03 P210 — Missing",
        "Slave 07  — New",
    ]
    assert form["data_schema"]({})["devices"] == ["1", "3"]
    await options.async_step_scan_result({"devices": ["1", "3", "7"]})
    await options.async_step_model({"model": "p404"})
    await options.async_step_model({"model": "p210"})
    await options.async_step_model({"model": "p404"})
    await options.async_step_groups({"group_a": "cct", "group_b": "single"})
    await options.async_step_groups({"group_a": "single"})
    result = await options.async_step_groups({"group_a": "single", "group_b": "cct"})
    assert [d["slave"] for d in result["data"]["devices"]] == [1, 3, 7]
    assert result["data"]["devices"][0]["group_a"] == "cct"
    assert result["data"]["devices"][2]["group_b"] == "cct"


async def test_manage_edit_or_remove_all(options):
    await options.async_step_init()
    assert (await options.async_step_manage_devices())["step_id"] == "scan_result"
    await options.async_step_manage_devices({"devices": ["3"]})
    assert (await options.async_step_model())["data_schema"]({})["model"] == "p210"
    await options.async_step_model({"model": "p404"})
    assert (await options.async_step_groups())["data_schema"]({})["group_a"] == "single"
    result = await options.async_step_groups({"group_a": "single", "group_b": "single"})
    assert result["data"]["devices"][0]["model"] == "p404"
    result = await options.async_step_manage_devices({"devices": []})
    assert result["data"]["devices"] == []


async def test_p210_group_a_mode_and_legacy_default(options):
    await options.async_step_init()
    await options.async_step_manage_devices({"devices": ["3"]})
    assert (await options.async_step_model())["data_schema"]({})["model"] == "p210"
    result = await options.async_step_model({"model": "p210"})
    assert result["step_id"] == "groups"
    schema = result["data_schema"]
    assert schema({})["group_a"] == "single"
    assert "group_b" not in schema.schema
    result = await options.async_step_groups({"group_a": "cct"})
    assert result["data"]["devices"] == [
        {"slave": 3, "model": "p210", "group_a": "cct"}
    ]


async def test_p210_rescan_preserves_selected_group_mode(options, entry):
    hass_options = {
        **entry.options,
        "devices": [{"slave": 3, "model": "p210", "group_a": "cct"}],
    }
    options.hass.config_entries.async_update_entry(entry, options=hass_options)
    await options.async_step_init()
    await options.async_step_manage_devices({"devices": ["3"]})
    await options.async_step_model({"model": "p210"})
    form = await options.async_step_groups()
    assert form["data_schema"]({})["group_a"] == "cct"
    result = await options.async_step_groups({"group_a": "cct"})
    assert result["data"]["devices"] == [
        {"slave": 3, "model": "p210", "group_a": "cct"}
    ]


async def test_options_runtime_scan_pauses(options, entry, hass):
    await options.async_step_init()
    hub = Hub.create(hass, "test", 502, (Device(1, Model.P404),), 2)
    entry.runtime_data = hub
    with patch.object(hub, "scan", new=AsyncMock(return_value=[1])) as scan:
        await options.async_step_scan()
        await options._scan_task
        await options.async_step_scan()
    scan.assert_awaited_once()
    assert scan.call_args.args[:2] == (1, 32)
    await hub.stop()


async def test_full_native_flow_manager(hass):
    with (
        patch(
            "custom_components.coselig_modbus_hub.config_flow.validate_gateway",
            new=AsyncMock(return_value={"host": "test", "port": 502}),
        ),
        patch(
            "custom_components.coselig_modbus_hub.config_flow.scan",
            new=AsyncMock(return_value=[1, 3]),
        ),
        patch(
            "custom_components.coselig_modbus_hub.async_setup_entry",
            new=AsyncMock(return_value=True),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        flow_id = result["flow_id"]
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"host": "test", "port": 502}
        )
        assert result["step_id"] == "scan_range"
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"scan_start": 1, "scan_end": 247}
        )
        assert result["type"] == FlowResultType.SHOW_PROGRESS
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(flow_id)
        assert result["step_id"] == "scan_result"
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"devices": ["1", "3"]}
        )
        assert result["step_id"] == "model"
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"model": "p404"}
        )
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"model": "p210"}
        )
        assert result["step_id"] == "groups"
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"group_a": "single", "group_b": "single"}
        )
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"group_a": "single"}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert len(hass.config_entries.async_entries(DOMAIN)) == 1
        await hass.async_block_till_done()


async def test_native_cancel_closes_scan(hass):
    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow_scan(*args):
        try:
            await release.wait()
        finally:
            cancelled.set()

    with (
        patch(
            "custom_components.coselig_modbus_hub.config_flow.validate_gateway",
            new=AsyncMock(return_value={"host": "test", "port": 502}),
        ),
        patch("custom_components.coselig_modbus_hub.config_flow.scan", new=slow_scan),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}, data={"host": "test", "port": 502}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"scan_start": 1, "scan_end": 247}
        )
        await asyncio.sleep(0)
        hass.config_entries.flow.async_abort(result["flow_id"])
        await cancelled.wait()
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_duplicate_gateway(hass, entry, flow):
    with patch(
        "custom_components.coselig_modbus_hub.config_flow.validate_gateway",
        new=AsyncMock(return_value=dict(entry.data)),
    ):
        from homeassistant.data_entry_flow import AbortFlow

        with pytest.raises(AbortFlow):
            await flow.async_step_user(dict(entry.data))


async def test_native_options_manager(hass, entry):
    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
    ) as reload:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == FlowResultType.MENU
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "polling"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"poll_interval": 0.5}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
    assert entry.options["poll_interval"] == 0.5
    assert len(entry.options["devices"]) == 2
    reload.assert_not_awaited()


async def test_rescan_all_existing_missing_still_manageable(options):
    await options.async_step_init()
    with patch(
        "custom_components.coselig_modbus_hub.config_flow.scan",
        new=AsyncMock(return_value=[]),
    ):
        await options.async_step_scan()
        await options._scan_task
        result = await options.async_step_scan()
    assert result["step_id"] == "scan_result"
    form = await options.async_step_scan_result()
    assert form["data_schema"]({})["devices"] == ["1", "3"]
    choices = next(iter(form["data_schema"].schema.values())).config["options"]
    assert all("Missing" in choice["label"] for choice in choices)


async def test_forms_use_actual_ha_frontend_serializer(flow, options):
    import json

    from homeassistant.components.config.config_entries import (
        ConfigManagerFlowResourceView,
    )
    from homeassistant.helpers.data_entry_flow import FlowManagerResourceView

    config_view = ConfigManagerFlowResourceView(flow.hass.config_entries.flow)
    options_view = FlowManagerResourceView(options.hass.config_entries.options)
    # Every user-editable schema must pass HA's actual JSON serialization path.
    config_forms = [await flow.async_step_user(), await flow.async_step_scan_range()]
    flow._selected = [1]
    config_forms.append(await flow.async_step_model())
    await flow.async_step_model({"model": "p404"})
    config_forms.append(await flow.async_step_groups())
    for form in config_forms:
        result = config_view._prepare_result_json(form)
        assert result["data_schema"]
        json.dumps(result)
    await options.async_step_init()
    option_forms = [
        await options.async_step_polling(),
        await options.async_step_manage_devices(),
    ]
    for form in option_forms:
        result = options_view._prepare_result_json(form)
        assert result["data_schema"]
        json.dumps(result)


@pytest.mark.parametrize("start,end", [(1, 247), (51, 80), (1, 10)])
async def test_tcp_success_never_scans_until_range_submitted(flow, gateway, start, end):
    with patch(
        "custom_components.coselig_modbus_hub.config_flow.scan",
        new=AsyncMock(return_value=[start]),
    ) as scanner:
        result = await flow.async_step_user({"host": "127.0.0.1", "port": gateway.port})
        assert result["step_id"] == "scan_range"
        assert result["data_schema"]({}) == {"scan_start": 1, "scan_end": 247}
        await flow.hass.async_block_till_done()
        scanner.assert_not_awaited()
        assert gateway.requests == []
        assert (await flow.async_step_scan_range())["step_id"] == "scan_range"
        scanner.assert_not_awaited()
        await flow.async_step_scan_range({"scan_start": start, "scan_end": end})
        await flow._scan_task
        scanner.assert_awaited_once()
        assert scanner.call_args.args[1:3] == (start, end)
        await flow.async_step_scan()
        result = await flow.async_step_scan_result()
        assert result["data_schema"]({})["devices"] == []
        assert (await flow.async_step_scan_result({"devices": []}))["errors"] == {
            "devices": "no_devices"
        }
        await flow.async_step_scan_result({"devices": [str(start)]})
        result = await flow.async_step_model({"model": "p210"})
        result = await flow.async_step_groups({"group_a": "single"})
        assert result["options"]["scan_start"] == start
        assert result["options"]["scan_end"] == end


@pytest.mark.parametrize(
    "start,end", [(0, 10), (1, 248), (-1, 32), (248, 248), (1.5, 10), (1, 10.5)]
)
async def test_range_bounds_rejected_before_requests(flow, start, end):
    schema = (await flow.async_step_scan_range())["data_schema"]
    with pytest.raises(Exception):
        schema({"scan_start": start, "scan_end": end})
    assert flow._scan_task is None


async def test_rescan_remembers_range_then_persists_new_range(options, entry, hass):
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "scan_start": 51, "scan_end": 80}
    )
    await options.async_step_init()
    result = await options.async_step_rescan()
    assert result["data_schema"]({}) == {"scan_start": 51, "scan_end": 80}
    assert options._scan_task is None
    with patch(
        "custom_components.coselig_modbus_hub.config_flow.scan",
        new=AsyncMock(return_value=[60]),
    ) as scanner:
        await options.async_step_scan_range({"scan_start": 60, "scan_end": 74})
        await options._scan_task
        assert scanner.call_args.args[1:3] == (60, 74)
        await options.async_step_scan()
    await options.async_step_scan_result({"devices": ["1", "3", "60"]})
    await options.async_step_model({"model": "p404"})
    await options.async_step_model({"model": "p210"})
    await options.async_step_model({"model": "p210"})
    await options.async_step_groups({"group_a": "single", "group_b": "single"})
    await options.async_step_groups({"group_a": "cct"})
    result = await options.async_step_groups({"group_a": "single"})
    assert result["data"]["scan_start"] == 60
    assert result["data"]["scan_end"] == 74
