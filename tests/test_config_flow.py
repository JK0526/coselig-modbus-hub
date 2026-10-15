"""Config Flow intent separation and confirmation regression tests."""

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
from custom_components.coselig_modbus_hub.models import Device, GroupMode, Model
from custom_components.coselig_modbus_hub.profiles import DeviceFamily
from custom_components.coselig_modbus_hub.scanner import ScanResult
from custom_components.coselig_modbus_hub.transport import GatewayError


@pytest.fixture
def flow(hass):
    result = ConfigFlow()
    result.hass = hass
    result.handler = DOMAIN
    result.flow_id = "test-flow"
    result.context = {"source": "user"}
    return result


@pytest.fixture
def options(hass, entry):
    result = CoseligOptionsFlow()
    result.hass = hass
    result.handler = entry.entry_id
    result.flow_id = "test-options"
    return result


def _choices(result, key):
    return next(
        item.config["options"]
        for schema_key, item in result["data_schema"].schema.items()
        if schema_key.schema == key
    )


async def _options_ready(options):
    result = await options.async_step_init()
    assert result["menu_options"] == ["device_management", "communication"]
    return result


async def _add_p404(options, slave):
    options._found = [slave]
    options._detections = {
        slave: ScanResult(
            slave, DeviceFamily.DIMMER, (Model.P404, Model.P210), Model.P404
        )
    }
    await options.async_step_scan_result()
    result = await options.async_step_scan_result({"devices": [str(slave)]})
    assert result["step_id"] == "model"
    assert result["data_schema"]({})["model"] == "p404"
    result = await options.async_step_model({"model": "p404"})
    assert result["step_id"] == "groups"
    await options.async_step_groups({"group_a": "single", "group_b": "single"})
    return await options.async_step_groups({"group_a": "single", "group_b": "single"})


@pytest.mark.parametrize(
    "host,expected",
    [
        ("  GATEWAY.local. ", "gateway.local"),
        ("192.168.1.2", "192.168.1.2"),
        ("::1", "::1"),
    ],
)
def test_valid_host(host, expected):
    assert valid_host(host) == expected


@pytest.mark.parametrize(
    "host", ["", "bad host", "-bad", "a" * 64 + ".local", "x" * 254]
)
def test_invalid_host(host):
    with pytest.raises(ValueError):
        valid_host(host)


async def test_validate_gateway_closes_on_success_and_connection_error(gateway):
    assert await validate_gateway({"host": "127.0.0.1", "port": gateway.port}) == {
        "host": "127.0.0.1",
        "port": gateway.port,
    }
    with (
        patch(
            "custom_components.coselig_modbus_hub.config_flow.Transport.connect",
            new=AsyncMock(side_effect=GatewayError),
        ),
        patch(
            "custom_components.coselig_modbus_hub.config_flow.Transport.close",
            new=AsyncMock(),
        ) as close,
    ):
        with pytest.raises(GatewayError):
            await validate_gateway({"host": "gateway", "port": 502})
        close.assert_awaited_once()


async def test_initial_add_flow_has_summary_before_create_and_p404_default(flow):
    scan_results = [
        ScanResult(63, DeviceFamily.RELAY, (Model.R410,), Model.R410),
        ScanResult(70, DeviceFamily.DIMMER, (Model.P404, Model.P210), Model.P404),
    ]
    with (
        patch(
            "custom_components.coselig_modbus_hub.config_flow.validate_gateway",
            new=AsyncMock(return_value={"host": "test", "port": 502}),
        ),
        patch(
            "custom_components.coselig_modbus_hub.config_flow.scan",
            new=AsyncMock(return_value=scan_results),
        ) as scanner,
    ):
        result = await flow.async_step_user({"host": "test", "port": 502})
        assert result["step_id"] == "scan_range"
        scanner.assert_not_awaited()
        result = await flow.async_step_scan_range({"scan_start": 51, "scan_end": 80})
        assert result["type"] == FlowResultType.SHOW_PROGRESS
        await flow._scan_task
        assert (await flow.async_step_scan())["step_id"] == "scan_result"
        assert _choices(await flow.async_step_scan_result(), "devices") == [
            {"value": "63", "label": "Slave 63 · New"},
            {"value": "70", "label": "Slave 70 · New"},
        ]
        result = await flow.async_step_scan_result({"devices": ["63", "70"]})
        assert result["step_id"] == "model"
        assert [device.model for device in flow._working_devices] == [Model.R410]
        assert result["data_schema"]({})["model"] == "p404"
        labels = [item["label"] for item in _choices(result, "model")]
        assert "P404 — 4 路 PWM 調光器（SUNWAVE L10）" in labels
        assert "P210 — 2 路 PWM 調光器（SUNWAVE L2）" in labels
        assert (
            "P404／P210 無法由協議可靠自動區分"
            in result["description_placeholders"]["model_reason"]
        )
        result = await flow.async_step_model({"model": "p404"})
        assert result["step_id"] == "groups"
        result = await flow.async_step_groups({"group_a": "single", "group_b": "cct"})
        assert result["step_id"] == "add_summary"
        assert result["description_placeholders"]["count"] == "2"
        result = await flow.async_step_confirm_add()
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["options"]["scan_start"] == 51
        assert result["options"]["devices"] == [
            {"slave": 63, "model": "r410"},
            {"slave": 70, "model": "p404", "group_a": "single", "group_b": "cct"},
        ]
        assert result["version"] == 2 and result["minor_version"] == 1


async def test_initial_p210_can_be_selected_and_canceled_without_create(flow):
    flow._gateway = {"host": "test", "port": 502}
    flow._detections = {62: ScanResult(62, DeviceFamily.DIMMER)}
    flow._found = [62]
    await flow.async_step_scan_result({"devices": ["62"]})
    result = await flow.async_step_model({"model": "p210"})
    assert result["data_schema"]({}) == {"group_a": "single"}
    result = await flow.async_step_groups({"group_a": "cct"})
    assert result["step_id"] == "add_summary"
    assert (await flow.async_step_cancel_add())["reason"] == "user_cancelled"


@pytest.mark.parametrize("start,end", [(1, 247), (51, 80), (1, 10)])
async def test_config_flow_custom_range_waits_for_submit(flow, start, end):
    flow._gateway = {"host": "test", "port": 502}
    with patch(
        "custom_components.coselig_modbus_hub.config_flow.scan",
        new=AsyncMock(return_value=[]),
    ) as scanner:
        assert (await flow.async_step_scan_range())["step_id"] == "scan_range"
        scanner.assert_not_awaited()
        result = await flow.async_step_scan_range(
            {"scan_start": start, "scan_end": end}
        )
        assert result["type"] == FlowResultType.SHOW_PROGRESS
        await flow._scan_task
        scanner.assert_awaited_once()
        assert scanner.call_args.args[1:3] == (start, end)
        assert (await flow.async_step_scan())["step_id"] == "scan_failed"


@pytest.mark.parametrize(
    "start,end", [(0, 10), (1, 248), (-1, 32), (248, 248), (1.5, 10), (1, 10.5)]
)
async def test_scan_range_bounds_reject_before_creating_task(flow, start, end):
    schema = (await flow.async_step_scan_range())["data_schema"]
    with pytest.raises(Exception):
        schema({"scan_start": start, "scan_end": end})
    assert flow._scan_task is None


@pytest.mark.parametrize(
    "family,candidates,recommended,expect_model",
    [
        (DeviceFamily.DIMMER, (Model.P404, Model.P210), Model.P404, True),
        (DeviceFamily.RELAY, (Model.R410,), Model.R410, False),
        (DeviceFamily.UNKNOWN, tuple(Model), None, True),
    ],
)
async def test_model_flow_recommendations_and_single_r410_skip(
    flow, family, candidates, recommended, expect_model
):
    flow._gateway = {"host": "test", "port": 502}
    flow._found = [51]
    flow._detections = {51: ScanResult(51, family, candidates, recommended)}
    result = await flow.async_step_scan_result({"devices": ["51"]})
    if expect_model:
        assert result["step_id"] == "model"
        default = recommended or Model.P404
        assert result["data_schema"]({})["model"] == default.value
        assert [item["value"] for item in _choices(result, "model")] == [
            model.value for model in candidates
        ]
        assert result["description_placeholders"]["recommended"] == (
            recommended.value.upper() if recommended else "—"
        )
        result = await flow.async_step_model({"model": default.value})
    else:
        assert result["step_id"] == "add_summary"
    assert result["step_id"] == (
        "add_summary" if family == DeviceFamily.RELAY else "groups"
    )
    if family == DeviceFamily.RELAY:
        assert flow._working_devices == [Device(51, Model.R410)]


async def test_scan_error_no_new_and_range_validation(flow, options):
    assert (await flow.async_step_scan_range())["step_id"] == "scan_range"
    assert (await flow.async_step_scan_range({"scan_start": 80, "scan_end": 51}))[
        "errors"
    ] == {"base": "invalid_range"}
    with pytest.raises(Exception):
        (await flow.async_step_scan_range())["data_schema"](
            {"scan_start": 0, "scan_end": 247}
        )
    flow._gateway = {"host": "test", "port": 502}
    with patch(
        "custom_components.coselig_modbus_hub.config_flow.scan",
        new=AsyncMock(side_effect=GatewayError),
    ):
        await flow.async_step_scan_range({"scan_start": 51, "scan_end": 80})
        await asyncio.gather(flow._scan_task, return_exceptions=True)
        assert (await flow.async_step_scan())["step_id"] == "scan_failed"
        assert (await flow.async_step_scan_failed())["menu_options"] == [
            "rescan",
            "scan_range",
            "cancel",
        ]
    options._existing = {1: Device(1, Model.P404), 3: Device(3, Model.P210)}
    options._gateway = {"host": "test", "port": 502}
    with patch(
        "custom_components.coselig_modbus_hub.config_flow.scan",
        new=AsyncMock(return_value=[ScanResult(1)]),
    ):
        await options.async_step_scan_range({"scan_start": 1, "scan_end": 32})
        await options._scan_task
        assert (await options.async_step_scan())["step_id"] == "scan_no_new"
    result = await options.async_step_scan_no_new()
    assert result["menu_options"] == [
        "rescan",
        "scan_range",
        "back_to_device_management",
    ]
    assert "Slave 3 · P210 · 未在線" in result["description_placeholders"]["missing"]
    assert "Slave 1 · P404" in result["description_placeholders"]["existing"]


async def test_progress_rescan_and_summary_entrypoints(flow, options):
    flow._scan_progress(2, 10, 1)
    assert flow._progress == {"current": "2", "total": "10", "found": "1"}
    assert (await flow.async_step_rescan())["step_id"] == "scan_range"
    options._existing = {1: Device(1, Model.P404)}
    options._found = [1]
    assert (await options.async_step_scan_result())["step_id"] == "scan_no_new"
    options._working_devices = [Device(1, Model.P404)]
    assert (await options.async_step_add_summary())["step_id"] == "add_summary"
    options._flow_action = "edit"
    options._edit_old = Device(1, Model.P404)
    assert (await options.async_step_edit_summary())["step_id"] == "edit_summary"
    flow._working_devices = [Device(1, Model.R410)]
    assert (await flow.async_step_confirm_edit())["reason"] == "user_cancelled"


async def test_cancel_scan_task_and_cleanly_returns_to_menu(flow, options):
    flow._gateway = {"host": "test", "port": 502}
    gate = asyncio.Event()
    with patch(
        "custom_components.coselig_modbus_hub.config_flow.scan",
        new=AsyncMock(side_effect=lambda *args: gate.wait()),
    ):
        result = await flow.async_step_scan_range({"scan_start": 33, "scan_end": 247})
        assert result["type"] == FlowResultType.SHOW_PROGRESS
        task = flow._scan_task
        flow.async_remove()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
    assert (await flow.async_step_cancel())["reason"] == "user_cancelled"
    assert (await options.async_step_cancel())["step_id"] == "device_management"
    options.async_remove()


async def test_options_menus_and_add_only_new_with_confirmation(options, entry, hass):
    original = {
        **entry.options,
        "future_option": {"keep": True},
        "devices": [
            {"slave": 1, "model": "p404", "group_a": "cct", "group_b": "single"},
            {"slave": 3, "model": "p210", "group_a": "cct"},
        ],
    }
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    assert (await options.async_step_device_management())["menu_options"] == [
        "add_devices",
        "edit_devices",
        "remove_devices",
        "back_to_options",
    ]
    assert (await options.async_step_communication())["menu_options"] == [
        "polling",
        "back_to_options",
    ]
    options._found = [1, 3, 63]
    options._detections = {
        63: ScanResult(63, DeviceFamily.DIMMER, (Model.P404, Model.P210), Model.P404)
    }
    result = await options.async_step_scan_result()
    assert _choices(result, "devices") == [{"value": "63", "label": "Slave 63 · New"}]
    assert "Slave 1 · P404" in result["description_placeholders"]["existing"]
    # Deselecting all New devices raises a validation error; it cannot remove existing.
    empty = await options.async_step_scan_result({"devices": []})
    assert empty["errors"] == {"devices": "no_devices"}
    assert entry.options == original
    result = await options.async_step_scan_result({"devices": ["63"]})
    assert result["step_id"] == "model"
    assert [device.slave for device in options._working_devices] == []
    result = await options.async_step_model({"model": "p404"})
    await options.async_step_groups({"group_a": "single", "group_b": "cct"})
    result = await options.async_step_groups({"group_a": "single", "group_b": "cct"})
    assert result["step_id"] == "add_summary"
    assert entry.options == original
    result = await options.async_step_confirm_add()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert [device["slave"] for device in result["data"]["devices"]] == [1, 3, 63]
    assert result["data"]["devices"][0]["group_a"] == "cct"
    assert result["data"]["devices"][1]["group_a"] == "cct"
    assert result["data"]["future_option"] == {"keep": True}
    assert result["data"]["poll_interval"] == original["poll_interval"]
    assert result["data"]["scan_start"] == original["scan_start"]
    assert result["data"]["scan_end"] == original["scan_end"]


async def test_add_r410_jumps_to_summary_and_does_not_replace_existing(options):
    await _options_ready(options)
    options._found = [63]
    options._detections = {
        63: ScanResult(63, DeviceFamily.RELAY, (Model.R410,), Model.R410)
    }
    await options.async_step_scan_result()
    result = await options.async_step_scan_result({"devices": ["63"]})
    assert result["step_id"] == "add_summary"
    assert [device.slave for device in options._working_devices] == [63]
    assert "R410 · 4 Relay" in result["description_placeholders"]["summary"]


async def test_edit_one_device_preserves_unchanged_groups_and_summary(
    options, entry, hass
):
    existing = [
        {"slave": 1, "model": "p404", "group_a": "cct", "group_b": "single"},
        {"slave": 3, "model": "p210", "group_a": "cct"},
    ]
    original = {**entry.options, "devices": existing, "future_option": 9}
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    result = await options.async_step_edit_devices()
    assert [item["value"] for item in _choices(result, "slave")] == ["1", "3"]
    result = await options.async_step_edit_devices({"slave": "1"})
    assert result["data_schema"]({})["model"] == "p404"
    await options.async_step_model({"model": "p404"})
    result = await options.async_step_groups()
    assert result["data_schema"]({}) == {"group_a": "cct", "group_b": "single"}
    result = await options.async_step_groups({"group_a": "cct", "group_b": "single"})
    assert result["step_id"] == "edit_summary"
    assert "目前：P404" in result["description_placeholders"]["summary"]
    assert "新輸出設定" in result["description_placeholders"]["summary"]
    assert entry.options == original
    result = await options.async_step_confirm_edit()
    assert result["data"]["devices"] == existing
    assert result["data"]["devices"][1] == existing[1]
    assert result["data"]["future_option"] == 9


async def test_edit_model_change_and_r410_has_no_group_step(options, entry, hass):
    original = {
        **entry.options,
        "devices": [
            {"slave": 1, "model": "p404", "group_a": "cct", "group_b": "cct"},
            {"slave": 3, "model": "p210", "group_a": "single"},
        ],
    }
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    await options.async_step_edit_devices({"slave": "1"})
    result = await options.async_step_model({"model": "p210"})
    assert result["step_id"] == "groups"
    assert result["data_schema"]({}) == {"group_a": "single"}
    result = await options.async_step_groups({"group_a": "single"})
    assert result["step_id"] == "edit_summary"
    assert "P404" in result["description_placeholders"]["summary"]
    assert "P210" in result["description_placeholders"]["summary"]
    assert entry.options == original
    await options.async_step_cancel_edit()
    await options.async_step_edit_devices({"slave": "1"})
    result = await options.async_step_model({"model": "r410"})
    assert result["step_id"] == "edit_summary"
    assert len(options._working_devices[0].outputs) == 4


async def test_remove_is_separate_multi_select_and_requires_confirmation(
    options, entry, hass
):
    original = {
        **entry.options,
        "devices": [
            {"slave": 1, "model": "p404"},
            {"slave": 3, "model": "p210"},
        ],
        "future_option": [1, 2],
    }
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    result = await options.async_step_remove_devices()
    assert _choices(result, "devices") == [
        {"value": "1", "label": "Slave 1 · P404"},
        {"value": "3", "label": "Slave 3 · P210"},
    ]
    assert (await options.async_step_remove_devices({"devices": []}))["errors"] == {
        "devices": "no_devices_to_remove"
    }
    result = await options.async_step_remove_devices({"devices": ["3"]})
    assert result["step_id"] == "remove_summary"
    assert "Slave 3 · P210" in result["description_placeholders"]["devices"]
    assert entry.options == original
    assert (await options.async_step_remove_summary())["step_id"] == "remove_summary"
    assert (await options.async_step_cancel_remove())["step_id"] == "device_management"
    await options.async_step_remove_devices({"devices": ["1"]})
    result = await options.async_step_confirm_remove()
    assert result["data"]["devices"] == [{"slave": 3, "model": "p210"}]
    assert result["data"]["future_option"] == [1, 2]
    assert result["data"]["poll_interval"] == original["poll_interval"]
    assert result["data"]["scan_start"] == original["scan_start"]
    assert result["data"]["scan_end"] == original["scan_end"]


async def test_add_and_edit_preserve_raw_unmodified_device_options(
    options, entry, hass
):
    devices = [
        {
            "slave": 1,
            "model": "p404",
            "group_a": "single",
            "group_b": "single",
            "future_device_option": {"keep": True},
        },
        {"slave": 3, "model": "p210"},
    ]
    original = {**entry.options, "devices": devices, "future_option": "preserved"}
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    options._working_devices = [Device(70, Model.R410)]
    added = options._persist_add()["data"]
    assert added["devices"] == devices + [{"slave": 70, "model": "r410"}]
    assert added["future_option"] == "preserved"

    options._working_devices = [
        Device(1, Model.P404, group_a=GroupMode.CCT, group_b=GroupMode.SINGLE)
    ]
    edited = options._persist_edit()["data"]
    assert edited["devices"] == [
        {
            "slave": 1,
            "model": "p404",
            "group_a": "cct",
            "group_b": "single",
            "future_device_option": {"keep": True},
        },
        devices[1],
    ]


async def test_edit_and_remove_empty_device_lists_have_safe_back_path(
    options, entry, hass
):
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "devices": []}
    )
    await _options_ready(options)
    assert (await options.async_step_edit_devices())["menu_options"] == [
        "back_to_device_management"
    ]
    assert (await options.async_step_remove_devices())["menu_options"] == [
        "back_to_device_management"
    ]
    assert (await options.async_step_back_to_device_management())[
        "step_id"
    ] == "device_management"
    assert (await options.async_step_back_to_options())["step_id"] == "init"


async def test_polling_preserves_unknown_options_and_applies_runtime_value(
    options, entry, hass
):
    original = {**entry.options, "future_option": "preserved"}
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    assert (await options.async_step_communication())["menu_options"] == [
        "polling",
        "back_to_options",
    ]
    form = await options.async_step_polling()
    assert form["data_schema"]({})["poll_interval"] == 2.0
    assert (await options.async_step_polling({"poll_interval": 0.4}))["errors"] == {
        "poll_interval": "invalid_poll_interval"
    }
    hub = Hub.create(hass, "test", 502, (Device(1, Model.P404),), 2)
    entry.runtime_data = hub
    result = await options.async_step_polling({"poll_interval": 1.0})
    assert result["data"]["poll_interval"] == 1.0
    assert result["data"]["future_option"] == "preserved"
    assert hub.coordinator.poll_interval == 1.0
    await hub.stop()


async def test_polling_without_runtime_and_add_without_local_range(options, entry):
    await _options_ready(options)
    result = await options.async_step_polling({"poll_interval": 0})
    assert result["data"]["poll_interval"] == 0
    assert result["data"]["devices"] == entry.options["devices"]
    options._working_devices = [Device(70, Model.R410)]
    options._options.pop("scan_start")
    options._options.pop("scan_end")
    result = options._persist_add()
    assert result["data"]["scan_start"] == entry.options["scan_start"]
    assert result["data"]["scan_end"] == entry.options["scan_end"]


async def test_options_scan_uses_runtime_hub_and_only_saves_range_after_add(
    options, entry, hass
):
    await _options_ready(options)
    hub = Hub.create(hass, "test", 502, (Device(1, Model.P404),), 0)
    entry.runtime_data = hub
    with patch.object(
        hub, "scan", new=AsyncMock(return_value=[ScanResult(1), ScanResult(60)])
    ) as scanner:
        await options.async_step_add_devices()
        result = await options.async_step_scan_range({"scan_start": 51, "scan_end": 80})
        assert result["type"] == FlowResultType.SHOW_PROGRESS
        await options._scan_task
        assert (await options.async_step_scan())["step_id"] == "scan_result"
        scanner.assert_awaited_once_with(51, 80, options._scan_progress)
        assert options._found == [1, 60]
    await hub.stop()


async def test_scan_cancel_and_gateway_errors_keep_existing_options_unchanged(
    options, entry
):
    await _options_ready(options)
    original = dict(entry.options)
    with patch(
        "custom_components.coselig_modbus_hub.config_flow.scan",
        new=AsyncMock(return_value=[]),
    ):
        await options.async_step_add_devices()
        await options.async_step_scan_range({"scan_start": 1, "scan_end": 10})
        await options._scan_task
        result = await options.async_step_scan()
        assert result["step_id"] == "scan_no_new"
        assert entry.options == original


async def test_native_options_save_after_explicit_add_confirmation(hass, entry):
    original = dict(entry.options)
    with patch(
        "custom_components.coselig_modbus_hub.config_flow.scan",
        new=AsyncMock(
            return_value=[ScanResult(70, DeviceFamily.RELAY, (Model.R410,), Model.R410)]
        ),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        flow_id = result["flow_id"]
        result = await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": "device_management"}
        )
        assert result["menu_options"] == [
            "add_devices",
            "edit_devices",
            "remove_devices",
            "back_to_options",
        ]
        result = await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": "add_devices"}
        )
        assert result["step_id"] == "scan_range"
        result = await hass.config_entries.options.async_configure(
            flow_id, {"scan_start": 70, "scan_end": 70}
        )
        assert result["type"] == FlowResultType.SHOW_PROGRESS
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_configure(flow_id)
        assert result["step_id"] == "scan_result"
        result = await hass.config_entries.options.async_configure(
            flow_id, {"devices": ["70"]}
        )
        assert result["step_id"] == "add_summary"
        assert entry.options == original
        result = await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": "confirm_add"}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert [item["slave"] for item in entry.options["devices"]] == [1, 3, 70]


async def test_native_remove_flow_requires_separate_confirm_step(hass, entry):
    original = {
        **entry.options,
        "devices": [
            {"slave": 1, "model": "p404"},
            {"slave": 3, "model": "p210"},
        ],
    }
    hass.config_entries.async_update_entry(entry, options=original)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = result["flow_id"]
    result = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "device_management"}
    )
    result = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "remove_devices"}
    )
    result = await hass.config_entries.options.async_configure(
        flow_id, {"devices": ["3"]}
    )
    assert result["step_id"] == "remove_summary"
    assert entry.options == original
    result = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "confirm_remove"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"]["devices"] == [{"slave": 1, "model": "p404"}]


async def test_native_initial_flow_creates_entry_only_after_summary_confirmation(hass):
    results = [
        ScanResult(1, DeviceFamily.DIMMER, (Model.P404, Model.P210), Model.P404),
        ScanResult(70, DeviceFamily.RELAY, (Model.R410,), Model.R410),
    ]
    with (
        patch(
            "custom_components.coselig_modbus_hub.config_flow.validate_gateway",
            new=AsyncMock(return_value={"host": "gateway.local", "port": 502}),
        ),
        patch(
            "custom_components.coselig_modbus_hub.config_flow.scan",
            new=AsyncMock(return_value=results),
        ) as scanner,
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
            flow_id, {"host": "gateway.local", "port": 502}
        )
        assert result["step_id"] == "scan_range"
        scanner.assert_not_awaited()
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"scan_start": 1, "scan_end": 80}
        )
        assert result["type"] == FlowResultType.SHOW_PROGRESS
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(flow_id)
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"devices": ["1", "70"]}
        )
        assert result["step_id"] == "model"
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"model": "p404"}
        )
        assert result["step_id"] == "groups"
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"group_a": "single", "group_b": "cct"}
        )
        assert result["step_id"] == "add_summary"
        assert len(hass.config_entries.async_entries(DOMAIN)) == 0
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "confirm_add"}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert len(hass.config_entries.async_entries(DOMAIN)) == 1
        assert result["version"] == 2 and result["minor_version"] == 1


async def test_native_cancel_during_add_scan_cancels_task_without_entry(hass):
    cancelled = asyncio.Event()

    async def slow_scan(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with (
        patch(
            "custom_components.coselig_modbus_hub.config_flow.validate_gateway",
            new=AsyncMock(return_value={"host": "gateway.local", "port": 502}),
        ),
        patch("custom_components.coselig_modbus_hub.config_flow.scan", new=slow_scan),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        flow_id = result["flow_id"]
        await hass.config_entries.flow.async_configure(
            flow_id, {"host": "gateway.local", "port": 502}
        )
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"scan_start": 1, "scan_end": 247}
        )
        assert result["type"] == FlowResultType.SHOW_PROGRESS
        await asyncio.sleep(0)
        hass.config_entries.flow.async_abort(flow_id)
        await asyncio.wait_for(cancelled.wait(), 1)
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_duplicate_gateway_aborts_initial_flow(hass, entry):
    with patch(
        "custom_components.coselig_modbus_hub.config_flow.validate_gateway",
        new=AsyncMock(return_value=dict(entry.data)),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], dict(entry.data)
        )
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "already_configured"


async def test_p210_edit_keeps_group_mode_and_other_options(options, entry, hass):
    configured = [
        {"slave": 1, "model": "p404", "group_a": "single", "group_b": "cct"},
        {"slave": 3, "model": "p210", "group_a": "cct"},
    ]
    original = {**entry.options, "devices": configured, "future_option": "keep"}
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    await options.async_step_edit_devices({"slave": "3"})
    assert (await options.async_step_model())["data_schema"]({})["model"] == "p210"
    await options.async_step_model({"model": "p210"})
    assert (await options.async_step_groups())["data_schema"]({}) == {"group_a": "cct"}
    await options.async_step_groups({"group_a": "cct"})
    assert entry.options == original
    result = await options.async_step_confirm_edit()
    assert result["data"]["devices"] == configured
    assert result["data"]["devices"][0] == configured[0]
    assert result["data"]["future_option"] == "keep"


async def test_user_validation_errors_and_reconfigure(flow, entry):
    assert (await flow.async_step_user())["step_id"] == "user"
    with patch(
        "custom_components.coselig_modbus_hub.config_flow.validate_gateway",
        new=AsyncMock(side_effect=ValueError),
    ):
        assert (await flow.async_step_user({"host": "bad", "port": 502}))["errors"] == {
            "host": "invalid_host"
        }
    with patch(
        "custom_components.coselig_modbus_hub.config_flow.validate_gateway",
        new=AsyncMock(side_effect=GatewayError),
    ):
        assert (await flow.async_step_user({"host": "gateway", "port": 502}))[
            "errors"
        ] == {"base": "cannot_connect"}
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    with patch(
        "custom_components.coselig_modbus_hub.config_flow.validate_gateway",
        new=AsyncMock(side_effect=ValueError),
    ):
        assert (await flow.async_step_reconfigure({"host": "bad", "port": 502}))[
            "errors"
        ] == {"host": "invalid_host"}
    with patch(
        "custom_components.coselig_modbus_hub.config_flow.validate_gateway",
        new=AsyncMock(side_effect=GatewayError),
    ):
        assert (await flow.async_step_reconfigure({"host": "gateway", "port": 502}))[
            "errors"
        ] == {"base": "cannot_connect"}
