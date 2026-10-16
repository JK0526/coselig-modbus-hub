"""Config Flow intent separation and confirmation regression tests."""

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.coselig_modbus_hub.config_flow import (
    MODEL_LABELS,
    ConfigFlow,
    CoseligOptionsFlow,
    valid_host,
    validate_gateway,
)
from custom_components.coselig_modbus_hub.const import DOMAIN
from custom_components.coselig_modbus_hub.hub import Hub
from custom_components.coselig_modbus_hub.models import Device, GroupMode, Model
from custom_components.coselig_modbus_hub.profiles import DeviceFamily
from custom_components.coselig_modbus_hub.r410_cover import R410CoverMode
from custom_components.coselig_modbus_hub.scanner import ScanResult
from custom_components.coselig_modbus_hub.transport import GatewayError


def test_model_labels_use_coselig_names_without_legacy_aliases():
    assert MODEL_LABELS == {
        Model.P404: "P404 — 4 路 PWM 調光器",
        Model.P210: "P210 — 2 路 PWM 調光器",
        Model.R410: "R410 — 4 路 Relay",
    }


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


def _translation_strings(value):
    if isinstance(value, dict):
        for child in value.values():
            yield from _translation_strings(child)
    elif isinstance(value, str):
        yield value


def _translation_placeholders(value):
    return set(re.findall(r"{([A-Za-z_][A-Za-z0-9_]*)}", value))


async def _options_ready(options):
    result = await options.async_step_init()
    assert result["menu_options"] == ["device_management", "communication"]
    return result


async def _select_modbus(options, slave):
    result = await options.async_step_manage_devices({"device": f"modbus:{slave}"})
    assert result["step_id"] == "device_menu"
    return result


async def _select_s402(options, device_id):
    result = await options.async_step_manage_devices({"device": f"s402:{device_id}"})
    assert result["step_id"] == "s402_device_management"
    return result


async def _open_r410_outputs(options, slave=63):
    await _select_modbus(options, slave)
    return await options.async_step_r410_output_configuration()


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
        assert result["step_id"] == "setup_devices"
        assert result["menu_options"] == ["search_modbus", "add_s402"]
        scanner.assert_not_awaited()
        assert (await flow.async_step_search_modbus())["step_id"] == "scan_range"
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
        assert "P404 — 4 路 PWM 調光器" in labels
        assert "P210 — 2 路 PWM 調光器" in labels
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


@pytest.mark.parametrize("device_id", [0, 255])
async def test_initial_s402_flow_creates_s402_only_entry_without_scanning(
    flow, device_id
):
    flow._gateway = {"host": "test", "port": 502}
    result = await flow.async_step_setup_devices()
    assert result["menu_options"] == ["search_modbus", "add_s402"]
    with patch(
        "custom_components.coselig_modbus_hub.config_flow.scan",
        new=AsyncMock(side_effect=AssertionError("S402 setup must not scan")),
    ) as scanner:
        result = await flow.async_step_add_s402({"s402_id": device_id})
        assert result["step_id"] == "s402_summary"
        assert (
            "Coselig S402 #" + str(device_id)
            in result["description_placeholders"]["summary"]
        )
        assert "DI1：單擊事件" in result["description_placeholders"]["summary"]
        assert flow._s402_ids == []
        result = await flow.async_step_confirm_s402()
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["options"]["devices"] == []
        assert result["options"]["s402_devices"] == [{"id": device_id}]
        assert result["version"] == 2 and result["minor_version"] == 1
        scanner.assert_not_awaited()


@pytest.mark.parametrize(
    "device_id,error",
    [(-1, "invalid_s402_id"), (256, "invalid_s402_id"), ("bad", "invalid_s402_id")],
)
async def test_s402_id_form_rejects_invalid_range(flow, device_id, error):
    result = await flow.async_step_add_s402({"s402_id": device_id})
    assert result["errors"] == {"s402_id": error}


async def test_s402_initial_cancel_never_creates_entry(flow):
    flow._gateway = {"host": "test", "port": 502}
    await flow.async_step_add_s402({"s402_id": 7})
    assert (await flow.async_step_cancel_s402())["reason"] == "user_cancelled"


async def test_add_s402_rejects_duplicate_id_and_existing_slave_one(
    options, entry, hass
):
    original = {**entry.options, "s402_devices": [{"id": 7}]}
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    assert options._s402_ids == [7]
    assert (await options.async_step_add_s402({"s402_id": 7}))["errors"] == {
        "s402_id": "duplicate_s402_id"
    }
    options._existing[1] = Device(1, Model.P404)
    assert (await options.async_step_add_s402({"s402_id": 12}))["errors"] == {
        "base": "reserved_slave_one"
    }
    assert entry.options == original


async def test_s402_reserves_scanned_slave_one_but_allows_same_id_as_device(
    options, entry, hass
):
    original = {**entry.options, "s402_devices": [{"id": 51}], "future_option": 9}
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    options._found = [1, 51]
    options._detections = {1: ScanResult(1), 51: ScanResult(51)}
    result = await options.async_step_scan_result()
    assert _choices(result, "devices") == [{"value": "51", "label": "Slave 51 · New"}]
    assert "Slave #1 is reserved" in result["description_placeholders"]["reserved"]
    rejected = await options.async_step_scan_result({"devices": ["1"]})
    assert rejected["errors"] == {"base": "reserved_slave_one"}
    options._working_devices = [Device(51, Model.P404)]
    added = options._persist_add()["data"]
    assert added["devices"][-1] == {
        "slave": 51,
        "model": "p404",
        "group_a": "single",
        "group_b": "single",
    }
    assert added["s402_devices"] == [{"id": 51}]
    assert added["future_option"] == 9


async def test_s402_options_add_remove_confirmation_and_unknown_option_preservation(
    options, entry, hass
):
    original = {
        **entry.options,
        "devices": [],
        "s402_devices": [{"id": 7, "future_device_field": "keep"}],
        "future_option": {"keep": True},
    }
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    await options.async_step_device_management()
    assert (await options.async_step_add_device())["menu_options"] == [
        "search_modbus",
        "add_s402",
        "back_to_device_management",
    ]
    summary = await options.async_step_add_s402({"s402_id": 12})
    assert summary["step_id"] == "s402_summary"
    assert entry.options == original
    added = await options.async_step_confirm_s402()
    assert added["data"]["devices"] == original["devices"]
    assert added["data"]["s402_devices"] == [
        {"id": 7, "future_device_field": "keep"},
        {"id": 12},
    ]
    assert added["data"]["future_option"] == {"keep": True}
    assert added["data"]["poll_interval"] == original["poll_interval"]
    assert added["data"]["scan_start"] == original["scan_start"]
    assert added["data"]["scan_end"] == original["scan_end"]

    hass.config_entries.async_update_entry(entry, options=added["data"])
    await options.async_step_init()
    device = await _select_s402(options, 7)
    assert device["menu_options"] == [
        "remove_managed_s402",
        "back_to_device_list",
    ]
    assert device["description_placeholders"]["details"] == "DI1\nDI2\nDI3\nDI4"
    result = await options.async_step_remove_managed_s402()
    assert result["step_id"] == "s402_remove_summary"
    assert entry.options == added["data"]
    await options.async_step_cancel_s402_remove()
    assert entry.options == added["data"]
    await options.async_step_remove_managed_s402()
    removed = await options.async_step_confirm_s402_remove()
    assert removed["data"]["s402_devices"] == [{"id": 12}]
    assert removed["data"]["devices"] == original["devices"]
    assert removed["data"]["future_option"] == {"keep": True}


async def test_remove_s402_empty_state_has_return_menu(options, entry, hass):
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, "devices": [], "s402_devices": []}
    )
    original = dict(entry.options)
    await _options_ready(options)
    result = await options.async_step_manage_devices()
    assert result["step_id"] == "manage_devices_empty"
    assert result["description_placeholders"] is None
    assert result["menu_options"] == ["back_to_device_management"]
    assert (await options.async_step_back_to_device_management())["step_id"] == (
        "device_management"
    )
    assert entry.options == original


@pytest.mark.parametrize(
    (
        "filename",
        "expected_title",
        "expected_description",
        "expected_empty_title",
        "expected_empty_description",
        "expected_back_label",
    ),
    [
        (
            "strings.json",
            "Select device",
            "Select the device you want to manage.",
            "Installed devices",
            "No devices are configured.",
            "Back to previous step",
        ),
        (
            "translations/en.json",
            "Select device",
            "Select the device you want to manage.",
            "Installed devices",
            "No devices are configured.",
            "Back to previous step",
        ),
        (
            "translations/zh-Hant.json",
            "選擇裝置",
            "選擇要管理的裝置。",
            "已安裝裝置",
            "尚未設定任何裝置。",
            "返回上一層",
        ),
    ],
)
def test_manage_device_translation_steps_use_only_static_text(
    filename,
    expected_title,
    expected_description,
    expected_empty_title,
    expected_empty_description,
    expected_back_label,
):
    path = Path("custom_components/coselig_modbus_hub") / filename
    steps = json.loads(path.read_text())["options"]["step"]
    manage = steps["manage_devices"]
    empty = steps["manage_devices_empty"]

    assert manage["title"] == expected_title
    assert manage["description"] == expected_description
    assert empty["title"] == expected_empty_title
    assert empty["description"] == expected_empty_description
    for step in (manage, empty):
        assert all(
            "{" not in text and "}" not in text for text in _translation_strings(step)
        )
    assert empty["menu_options"]["back_to_device_management"] == expected_back_label


@pytest.mark.parametrize(
    "filename", ["strings.json", "translations/en.json", "translations/zh-Hant.json"]
)
def test_device_oriented_options_pages_have_static_titles_and_matching_descriptions(
    filename,
):
    path = Path("custom_components/coselig_modbus_hub") / filename
    steps = json.loads(path.read_text())["options"]["step"]
    expected_titles = (
        {
            "device_menu": "裝置管理",
            "s402_device_management": "S402 裝置",
            "model": "確認裝置型號",
            "groups": "輸出模式",
        }
        if filename.endswith("zh-Hant.json")
        else {
            "device_menu": "Device management",
            "s402_device_management": "S402 device",
            "model": "Confirm device model",
            "groups": "Output mode",
        }
    )
    device_oriented_steps = {
        "device_management",
        "add_device",
        "manage_devices",
        "manage_devices_empty",
        "device_menu",
        "s402_device_management",
        "r410_output_configuration",
        "manage_r410_covers",
    }

    for step_id in device_oriented_steps:
        assert "{" not in steps[step_id]["title"]
        assert "}" not in steps[step_id]["title"]
    for step_id, expected_title in expected_titles.items():
        assert steps[step_id]["title"] == expected_title

    assert _translation_placeholders(steps["device_menu"]["description"]) == {
        "device",
        "details",
    }
    assert _translation_placeholders(
        steps["s402_device_management"]["description"]
    ) == {"device", "details"}
    assert _translation_placeholders(
        steps["r410_output_configuration"]["description"]
    ) == {"allocations", "reason"}
    assert _translation_placeholders(steps["model"]["description"]) == {
        "slave",
        "family",
        "recommended",
        "model_reason",
    }
    assert _translation_placeholders(steps["groups"]["description"]) == {"slave"}
    assert all(
        not _translation_placeholders(step.get("title", "")) for step in steps.values()
    )

    # The legacy Config Flow title interpolation remains unchanged.
    config_steps = json.loads(path.read_text())["config"]["step"]
    assert _translation_placeholders(config_steps["model"]["title"]) == {"slave"}
    assert _translation_placeholders(config_steps["groups"]["title"]) == {"slave"}


async def test_s402_flows_handle_missing_pending_and_configured_list_safely(
    flow, options, entry
):
    assert (await flow.async_step_back_to_device_management())[
        "step_id"
    ] == "setup_devices"
    assert (await flow.async_step_s402_summary())["step_id"] == "add_s402"
    assert (await flow.async_step_confirm_s402())["step_id"] == "add_s402"
    assert (await options.async_step_cancel_s402())["step_id"] == "add_device"
    assert (await options.async_step_back_to_device_management())[
        "step_id"
    ] == "device_management"

    await _options_ready(options)
    options._existing = {}
    options._s402_ids = [7]
    original = dict(entry.options)
    result = await options.async_step_manage_devices()
    assert _choices(result, "device") == [
        {"value": "s402:7", "label": "S402 #7 · 4-input scene module"}
    ]
    assert result["description_placeholders"] is None
    assert entry.options == original
    result = await options.async_step_manage_devices({"device": "s402:7"})
    assert result["step_id"] == "s402_device_management"
    result = await options.async_step_remove_managed_s402()
    assert result["step_id"] == "s402_remove_summary"
    assert "s402_devices" not in entry.options


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
        "back_to_add_device",
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
    assert (await options.async_step_cancel())["step_id"] == "add_device"
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
    assert (await options.async_step_init())["step_id"] == "init"
    assert (await options.async_step_device_management())["menu_options"] == [
        "add_device",
        "manage_devices",
        "back_to_options",
    ]
    assert (await options.async_step_communication())["menu_options"] == [
        "polling",
        "communication_scan_range",
        "back_to_options",
    ]
    assert (await options.async_step_add_device())["menu_options"] == [
        "search_modbus",
        "add_s402",
        "back_to_device_management",
    ]
    managed = await options.async_step_manage_devices()
    assert [item["value"] for item in _choices(managed, "device")] == [
        "modbus:1",
        "modbus:3",
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


async def test_unified_device_selector_routes_each_device_to_its_capabilities(
    options, entry, hass
):
    devices = [
        {"slave": 51, "model": "p404", "group_a": "single", "group_b": "cct"},
        {"slave": 22, "model": "p210", "group_a": "cct"},
        {"slave": 63, "model": "r410"},
    ]
    s402 = [{"id": 7}]
    cover = _cover_options(slave=63)
    original = {
        **entry.options,
        "devices": devices,
        "s402_devices": s402,
        "r410_covers": [cover],
        "future_option": {"keep": True},
    }
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    result = await options.async_step_manage_devices()
    assert result["description_placeholders"] is None
    assert _choices(result, "device") == [
        {"value": "modbus:22", "label": "P210 #22 · 2-channel PWM dimmer"},
        {"value": "modbus:51", "label": "P404 #51 · 4-channel PWM dimmer"},
        {"value": "modbus:63", "label": "R410 #63 · 4-channel Relay"},
        {"value": "s402:7", "label": "S402 #7 · 4-input scene module"},
    ]
    assert entry.options == original

    p404 = await _select_modbus(options, 51)
    assert p404["description_placeholders"] == {
        "device": "P404 #51",
        "details": "4-channel PWM dimmer",
    }
    assert p404["menu_options"] == [
        "output_mode",
        "remove_managed_device",
        "back_to_device_list",
    ]
    result = await options.async_step_output_mode()
    assert result["data_schema"]({}) == {"group_a": "single", "group_b": "cct"}
    assert entry.options == original
    assert (await options.async_step_cancel_edit())["step_id"] == "device_menu"
    assert entry.options == original

    p210 = await options.async_step_manage_devices({"device": "modbus:22"})
    assert p210["step_id"] == "device_menu"
    assert p210["description_placeholders"] == {
        "device": "P210 #22",
        "details": "2-channel PWM dimmer",
    }
    assert (await options.async_step_output_mode())["data_schema"]({}) == {
        "group_a": "cct"
    }
    assert entry.options == original

    r410 = await options.async_step_manage_devices({"device": "modbus:63"})
    assert r410["description_placeholders"] == {
        "device": "R410 #63",
        "details": "4-channel Relay",
    }
    assert r410["menu_options"][0] == "r410_output_configuration"
    page = await options.async_step_r410_output_configuration()
    assert set(page["description_placeholders"]) == {"allocations", "reason"}
    assert "Garage" in page["description_placeholders"]["allocations"]
    assert page["menu_options"] == [
        "add_r410_cover",
        "manage_r410_covers",
        "back_to_device_menu",
    ]
    assert entry.options == original

    s402_page = await _select_s402(options, 7)
    assert s402_page["description_placeholders"] == {
        "device": "S402 #7",
        "details": "DI1\nDI2\nDI3\nDI4",
    }
    assert s402_page["menu_options"] == [
        "remove_managed_s402",
        "back_to_device_list",
    ]
    assert entry.options == original

    invalid = await options.async_step_manage_devices({"device": "modbus:999"})
    assert invalid["errors"] == {"device": "device_not_found"}
    malformed = await options.async_step_manage_devices({"device": "bad"})
    assert malformed["errors"] == {"device": "device_not_found"}
    assert entry.options == original


async def test_communication_scan_range_saves_without_scanning_or_dropping_options(
    options, entry, hass
):
    original = {
        **entry.options,
        "scan_start": 51,
        "scan_end": 80,
        "future_option": {"preserve": True},
    }
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    form = await options.async_step_communication_scan_range()
    assert form["step_id"] == "communication_scan_range"
    assert form["data_schema"]({}) == {"scan_start": 51, "scan_end": 80}
    assert (
        await options.async_step_communication_scan_range(
            {"scan_start": 80, "scan_end": 51}
        )
    )["errors"] == {"base": "invalid_range"}
    with pytest.raises(Exception):
        form["data_schema"]({"scan_start": 0, "scan_end": 248})
    assert entry.options == original

    with patch(
        "custom_components.coselig_modbus_hub.config_flow.scan",
        new=AsyncMock(),
    ) as scanner:
        saved = await options.async_step_communication_scan_range(
            {"scan_start": 1, "scan_end": 247}
        )
    assert saved["type"] == FlowResultType.CREATE_ENTRY
    assert saved["data"]["scan_start"] == 1
    assert saved["data"]["scan_end"] == 247
    assert saved["data"]["future_option"] == {"preserve": True}
    scanner.assert_not_awaited()
    assert entry.options == original

    await options.async_step_communication()
    assert (await options.async_step_back_to_options())["step_id"] == "init"
    assert entry.options == original


async def test_stale_device_routes_and_back_actions_are_navigation_only(
    options, entry, hass
):
    original = {
        **entry.options,
        "devices": [
            {"slave": 51, "model": "p404", "group_a": "single", "group_b": "single"},
            {"slave": 63, "model": "r410"},
        ],
        "s402_devices": [{"id": 7}],
        "future_option": "keep",
    }
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    assert (await options.async_step_manage_devices({"device": "bad"}))["errors"] == {
        "device": "device_not_found"
    }
    assert (await options.async_step_manage_devices({"device": "unknown:2"}))[
        "errors"
    ] == {"device": "device_not_found"}
    assert (await options.async_step_manage_devices({"device": None}))["errors"] == {
        "device": "device_not_found"
    }
    assert (await options.async_step_manage_devices({"device": "modbus:999"}))[
        "errors"
    ] == {"device": "device_not_found"}

    await _select_modbus(options, 63)
    assert (await options.async_step_output_mode())["step_id"] == "device_menu"
    assert (await options.async_step_manage_r410_covers())["menu_options"] == [
        "back_to_r410_output_configuration"
    ]
    assert (await options.async_step_back_to_r410_output_configuration())[
        "step_id"
    ] == "r410_output_configuration"
    assert (await options.async_step_back_to_manage_r410_covers())[
        "step_id"
    ] == "manage_r410_covers"
    assert (await options.async_step_back_to_device_menu())["step_id"] == "device_menu"
    assert (await options.async_step_back_to_device_list())[
        "step_id"
    ] == "manage_devices"
    assert (await options.async_step_back_to_add_device())["step_id"] == "add_device"
    assert (await options.async_step_cancel_add())["step_id"] == "add_device"
    options._flow_action = "edit"
    options._managed_id = 999
    assert (await options.async_step_cancel_edit())["step_id"] == "manage_devices"

    options._managed_kind = "invalid"
    options._managed_id = 63
    assert (await options.async_step_device_menu())["step_id"] == "manage_devices"
    assert (await options.async_step_output_mode())["step_id"] == "manage_devices"
    assert (await options.async_step_remove_managed_device())["step_id"] == (
        "manage_devices"
    )
    assert (await options.async_step_s402_device_management())["step_id"] == (
        "manage_devices"
    )
    assert (await options.async_step_remove_managed_s402())["step_id"] == (
        "manage_devices"
    )
    assert entry.options == original


async def test_reconfigure_flow_remains_available_for_gateway_host_and_port(
    flow, entry
):
    flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
    form = await flow.async_step_reconfigure()
    assert form["step_id"] == "reconfigure"
    assert form["data_schema"]({}) == entry.data
    with (
        patch(
            "custom_components.coselig_modbus_hub.config_flow.validate_gateway",
            new=AsyncMock(return_value={"host": "new-gateway.local", "port": 1502}),
        ),
        patch.object(
            flow,
            "async_update_reload_and_abort",
            return_value={
                "type": FlowResultType.ABORT,
                "reason": "reconfigure_successful",
            },
        ) as update,
    ):
        result = await flow.async_step_reconfigure(
            {"host": "new-gateway.local", "port": 1502}
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    update.assert_called_once_with(
        entry,
        data={"host": "new-gateway.local", "port": 1502},
        title="Coselig new-gateway.local",
    )


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
    result = await options.async_step_manage_devices()
    assert [item["value"] for item in _choices(result, "device")] == [
        "modbus:1",
        "modbus:3",
    ]
    await _select_modbus(options, 1)
    assert (await options.async_step_device_menu())["menu_options"] == [
        "output_mode",
        "remove_managed_device",
        "back_to_device_list",
    ]
    result = await options.async_step_output_mode()
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


async def test_modbus_management_edits_output_mode_without_changing_model(
    options, entry, hass
):
    original = {
        **entry.options,
        "devices": [
            {"slave": 1, "model": "p404", "group_a": "cct", "group_b": "cct"},
            {"slave": 3, "model": "p210", "group_a": "single"},
        ],
    }
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    await _select_modbus(options, 1)
    result = await options.async_step_output_mode()
    assert result["step_id"] == "groups"
    assert result["data_schema"]({}) == {"group_a": "cct", "group_b": "cct"}
    result = await options.async_step_groups({"group_a": "single", "group_b": "single"})
    assert result["step_id"] == "edit_summary"
    assert "P404" in result["description_placeholders"]["summary"]
    assert entry.options == original
    assert (await options.async_step_cancel_edit())["step_id"] == "device_menu"
    saved = await options.async_step_confirm_edit()
    assert saved["data"]["devices"][0]["model"] == "p404"
    assert [
        item["value"]
        for item in _choices(await options.async_step_manage_devices(), "device")
    ] == [
        "modbus:1",
        "modbus:3",
    ]


async def test_remove_selected_device_requires_confirmation_and_preserves_options(
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
    result = await options.async_step_manage_devices()
    assert _choices(result, "device") == [
        {"value": "modbus:1", "label": "P404 #1 · 4-channel PWM dimmer"},
        {"value": "modbus:3", "label": "P210 #3 · 2-channel PWM dimmer"},
    ]
    await _select_modbus(options, 3)
    result = await options.async_step_remove_managed_device()
    assert result["step_id"] == "remove_summary"
    assert "P210 #3" in result["description_placeholders"]["devices"]
    assert entry.options == original
    assert (await options.async_step_remove_summary())["step_id"] == "remove_summary"
    assert (await options.async_step_cancel_remove())["step_id"] == "device_menu"
    await options.async_step_remove_managed_device()
    result = await options.async_step_confirm_remove()
    assert result["data"]["devices"] == [{"slave": 1, "model": "p404"}]
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
    original = dict(entry.options)
    await _options_ready(options)
    empty = await options.async_step_manage_devices()
    assert empty["step_id"] == "manage_devices_empty"
    assert empty["description_placeholders"] is None
    assert empty["menu_options"] == ["back_to_device_management"]
    assert (await options.async_step_back_to_device_management())[
        "step_id"
    ] == "device_management"
    assert (await options.async_step_back_to_options())["step_id"] == "init"
    assert entry.options == original


async def test_polling_preserves_unknown_options_and_applies_runtime_value(
    options, entry, hass
):
    original = {**entry.options, "future_option": "preserved"}
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    assert (await options.async_step_communication())["menu_options"] == [
        "polling",
        "communication_scan_range",
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
        await options.async_step_search_modbus()
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
        await options.async_step_search_modbus()
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
            "add_device",
            "manage_devices",
            "back_to_options",
        ]
        result = await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": "add_device"}
        )
        assert result["menu_options"] == [
            "search_modbus",
            "add_s402",
            "back_to_device_management",
        ]
        result = await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": "search_modbus"}
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
        flow_id, {"next_step_id": "manage_devices"}
    )
    assert _choices(result, "device") == [
        {"value": "modbus:1", "label": "P404 #1 · 4-channel PWM dimmer"},
        {"value": "modbus:3", "label": "P210 #3 · 2-channel PWM dimmer"},
    ]
    result = await hass.config_entries.options.async_configure(
        flow_id, {"device": "modbus:3"}
    )
    result = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "remove_managed_device"}
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
        assert result["step_id"] == "setup_devices"
        scanner.assert_not_awaited()
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "search_modbus"}
        )
        assert result["step_id"] == "scan_range"
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


async def test_native_s402_only_initial_flow_creates_entry_after_confirm(hass):
    with (
        patch(
            "custom_components.coselig_modbus_hub.config_flow.validate_gateway",
            new=AsyncMock(return_value={"host": "gateway.local", "port": 502}),
        ),
        patch(
            "custom_components.coselig_modbus_hub.config_flow.scan",
            new=AsyncMock(side_effect=AssertionError("Must not scan S402-only bus")),
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
        assert result["step_id"] == "setup_devices"
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "add_s402"}
        )
        assert result["step_id"] == "add_s402"
        result = await hass.config_entries.flow.async_configure(flow_id, {"s402_id": 7})
        assert result["step_id"] == "s402_summary"
        assert len(hass.config_entries.async_entries(DOMAIN)) == 0
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "confirm_s402"}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["options"]["devices"] == []
        assert result["options"]["s402_devices"] == [{"id": 7}]
        assert result["minor_version"] == 1
        assert len(hass.config_entries.async_entries(DOMAIN)) == 1
        scanner.assert_not_awaited()


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
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"host": "gateway.local", "port": 502}
        )
        assert result["step_id"] == "setup_devices"
        await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "search_modbus"}
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
    await _select_modbus(options, 3)
    await options.async_step_output_mode()
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


def _cover_options(slave=63, *, mode="two_relay", open_relay=1, close_relay=2):
    cover = {
        "id": uuid4().hex,
        "slave": slave,
        "name": "Garage",
        "device_class": "garage",
        "mode": mode,
        "open_relay": open_relay,
        "close_relay": close_relay,
        "pulse_tenths": 5,
        "future_cover_setting": "keep",
    }
    if mode == "three_relay":
        cover["stop_relay"] = 3
    return cover


async def _start_add_cover(options):
    await _open_r410_outputs(options)
    return await options.async_step_add_r410_cover()


async def _open_cover_management(options, slave=63):
    await _open_r410_outputs(options, slave)
    return await options.async_step_manage_r410_covers()


async def _continue_add_cover(options, *, mode="two_relay", mapping=None, pulse=0.5):
    await options.async_step_cover_name({"name": "車庫鐵捲門"})
    await options.async_step_cover_class({})
    await options.async_step_cover_mode({"mode": mode})
    await options.async_step_cover_mapping(
        mapping or {"open_relay": "4", "close_relay": "1"}
    )
    await options.async_step_cover_pulse({"pulse_seconds": pulse})
    return await options.async_step_cover_summary()


async def test_add_two_relay_cover_free_mapping_validation_and_cancel_atomic(
    options, entry, hass
):
    original = {
        **entry.options,
        "devices": [{"slave": 63, "model": "r410"}],
        "future_option": {"keep": True},
    }
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    page = await _open_r410_outputs(options)
    assert page["menu_options"] == [
        "add_r410_cover",
        "back_to_device_menu",
    ]
    assert (await options.async_step_add_r410_cover())["step_id"] == "cover_name"
    assert (await options.async_step_cover_name({"name": "  "}))["errors"] == {
        "name": "invalid_cover_name"
    }
    assert (await options.async_step_cover_name({"name": "車庫"}))[
        "step_id"
    ] == "cover_class"
    class_schema = (await options.async_step_cover_class())["data_schema"]
    class_selector = next(iter(class_schema.schema.values()))
    assert class_selector.config["domain"] == "cover"
    assert (await options.async_step_cover_class({"device_class": "bad"}))[
        "errors"
    ] == {"device_class": "invalid_cover_device_class"}
    assert (await options.async_step_cover_class({}))["step_id"] == "cover_mode"
    assert (await options.async_step_cover_mode({"mode": "bad"}))["errors"] == {
        "mode": "invalid_cover_mode"
    }
    await options.async_step_cover_mode({"mode": "two_relay"})
    assert (await options.async_step_cover_mapping({"open_relay": "bad"}))[
        "errors"
    ] == {"base": "invalid_cover_relays"}
    assert (
        await options.async_step_cover_mapping({"open_relay": "1", "close_relay": "1"})
    )["errors"] == {"base": "invalid_cover_relays"}
    assert (
        await options.async_step_cover_mapping({"open_relay": "4", "close_relay": "1"})
    )["step_id"] == "cover_pulse"
    assert (await options.async_step_cover_pulse({"pulse_seconds": 0}))["errors"] == {
        "pulse_seconds": "invalid_cover_pulse"
    }
    assert (await options.async_step_cover_pulse({"pulse_seconds": 0.14}))[
        "errors"
    ] == {"pulse_seconds": "invalid_cover_pulse"}
    assert (await options.async_step_cover_pulse({"pulse_seconds": "bad"}))[
        "errors"
    ] == {"pulse_seconds": "invalid_cover_pulse"}
    result = await options.async_step_cover_pulse({"pulse_seconds": 0.5})
    assert result["step_id"] == "cover_summary"
    assert "停止：R4 + R1" in result["description_placeholders"]["summary"]
    assert "R2、R3" in result["description_placeholders"]["resource_change"]
    assert (await options.async_step_cancel_cover())["step_id"] == (
        "r410_output_configuration"
    )
    assert entry.options == original

    await _start_add_cover(options)
    await _continue_add_cover(options)
    assert entry.options == original
    result = await options.async_step_confirm_cover()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    saved = result["data"]
    assert saved["future_option"] == {"keep": True}
    assert saved["r410_covers"][0]["mode"] == R410CoverMode.TWO_RELAY
    assert saved["r410_covers"][0]["open_relay"] == 4
    assert saved["r410_covers"][0]["close_relay"] == 1
    assert "stop_relay" not in saved["r410_covers"][0]
    assert saved["r410_covers"][0]["pulse_tenths"] == 5
    assert len(saved["r410_covers"][0]["id"]) == 32


async def test_add_three_relay_cover_custom_pulse(options, entry, hass):
    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, "devices": [{"slave": 63, "model": "r410"}]},
    )
    await _options_ready(options)
    await _start_add_cover(options)
    await options.async_step_cover_name({"name": "Store shutter"})
    await options.async_step_cover_class({"device_class": "shutter"})
    await options.async_step_cover_mode({"mode": "three_relay"})
    await options.async_step_cover_mapping(
        {"open_relay": "3", "stop_relay": "1", "close_relay": "4"}
    )
    result = await options.async_step_cover_pulse({"pulse_seconds": 1.0})
    assert result["step_id"] == "cover_summary"
    saved = (await options.async_step_confirm_cover())["data"]["r410_covers"][0]
    assert (saved["open_relay"], saved["stop_relay"], saved["close_relay"]) == (
        3,
        1,
        4,
    )
    assert saved["pulse_tenths"] == 10


async def test_edit_cover_retains_uuid_and_unknown_fields_across_mode_change(
    options, entry, hass
):
    cover = _cover_options()
    original = {
        **entry.options,
        "devices": [{"slave": 63, "model": "r410"}],
        "r410_covers": [cover],
        "future_option": [1, 2],
    }
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    result = await _open_r410_outputs(options)
    assert "R2" in result["description_placeholders"]["allocations"]
    result = await options.async_step_manage_r410_covers()
    assert result["menu_options"] == [
        "edit_r410_cover",
        "remove_r410_cover",
        "back_to_r410_output_configuration",
    ]
    await options.async_step_edit_r410_cover({"id": cover["id"]})
    await options.async_step_cover_name({"name": "店面鐵捲門"})
    await options.async_step_cover_class({"device_class": "shutter"})
    await options.async_step_cover_mode({"mode": "three_relay"})
    await options.async_step_cover_mapping(
        {"open_relay": "3", "stop_relay": "1", "close_relay": "4"}
    )
    await options.async_step_cover_pulse({"pulse_seconds": 1.2})
    summary = await options.async_step_cover_summary()
    assert "R2" in summary["description_placeholders"]["resource_change"]
    saved = (await options.async_step_confirm_cover())["data"]
    updated = saved["r410_covers"][0]
    assert updated["id"] == cover["id"]
    assert updated["name"] == "店面鐵捲門"
    assert updated["device_class"] == "shutter"
    assert updated["mode"] == "three_relay"
    assert updated["pulse_tenths"] == 12
    assert updated["future_cover_setting"] == "keep"
    assert saved["future_option"] == [1, 2]


async def test_edit_three_relay_cover_to_two_drops_stop_relay(options, entry, hass):
    cover = _cover_options(mode="three_relay")
    original = {
        **entry.options,
        "devices": [{"slave": 63, "model": "r410"}],
        "r410_covers": [cover],
    }
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    await _open_cover_management(options)
    await options.async_step_edit_r410_cover({"id": cover["id"]})
    await options.async_step_cover_name({"name": "Garage"})
    await options.async_step_cover_class({"device_class": "garage"})
    await options.async_step_cover_mode({"mode": "two_relay"})
    await options.async_step_cover_mapping({"open_relay": "4", "close_relay": "2"})
    await options.async_step_cover_pulse({"pulse_seconds": 0.5})
    saved = (await options.async_step_confirm_cover())["data"]["r410_covers"][0]
    assert saved["id"] == cover["id"]
    assert saved["mode"] == "two_relay"
    assert "stop_relay" not in saved


async def test_three_relay_allocation_hides_add_when_only_one_relay_free(
    options, entry, hass
):
    hass.config.language = "zh-Hant"
    cover = _cover_options(mode="three_relay", open_relay=1, close_relay=3)
    cover["stop_relay"] = 2
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "devices": [{"slave": 63, "model": "r410"}],
            "r410_covers": [cover],
        },
    )
    await _options_ready(options)
    result = await _open_r410_outputs(options)
    assert "add_r410_cover" not in result["menu_options"]
    assert "R4：一般 Relay" in result["description_placeholders"]["allocations"]
    result = await options.async_step_add_r410_cover()
    assert "至少需要 2 個未使用 Relay" in result["description_placeholders"]["reason"]


async def test_remove_cover_requires_confirmation_and_restores_resources(
    options, entry, hass
):
    cover = _cover_options()
    original = {
        **entry.options,
        "devices": [{"slave": 63, "model": "r410"}],
        "r410_covers": [cover],
        "future_option": "preserve",
    }
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    await _open_cover_management(options)
    assert (await options.async_step_remove_r410_cover())[
        "step_id"
    ] == "remove_r410_cover"
    result = await options.async_step_remove_r410_cover({"id": cover["id"]})
    assert result["step_id"] == "remove_cover_summary"
    assert result["description_placeholders"]["relays"] == "R1、R2"
    assert entry.options == original
    assert (await options.async_step_cancel_remove_cover())[
        "step_id"
    ] == "manage_r410_covers"
    await options.async_step_manage_r410_covers()
    await options.async_step_remove_r410_cover({"id": cover["id"]})
    result = await options.async_step_confirm_remove_cover()
    assert result["data"]["r410_covers"] == []
    assert result["data"]["future_option"] == "preserve"


async def test_remove_one_cover_preserves_other_cover_record(options, entry, hass):
    first = _cover_options()
    second = _cover_options(slave=64, open_relay=3, close_relay=4)
    original = {
        **entry.options,
        "devices": [
            {"slave": 63, "model": "r410"},
            {"slave": 64, "model": "r410"},
        ],
        "r410_covers": [first, second],
    }
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    await _open_cover_management(options, 63)
    await options.async_step_remove_r410_cover({"id": first["id"]})
    result = await options.async_step_confirm_remove_cover()
    assert result["data"]["r410_covers"] == [second]


async def test_add_second_two_relay_cover_uses_remaining_free_relays(
    options, entry, hass
):
    first = _cover_options()
    original = {
        **entry.options,
        "devices": [{"slave": 63, "model": "r410"}],
        "r410_covers": [first],
    }
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    await _start_add_cover(options)
    result = await _continue_add_cover(
        options, mapping={"open_relay": "4", "close_relay": "3"}
    )
    assert "一般 Switch：無" in result["description_placeholders"]["resource_change"]
    saved = (await options.async_step_confirm_cover())["data"]
    assert len(saved["r410_covers"]) == 2
    assert {item["open_relay"] for item in saved["r410_covers"]} == {1, 4}


async def test_cancel_edit_cover_does_not_persist(options, entry, hass):
    cover = _cover_options()
    original = {
        **entry.options,
        "devices": [{"slave": 63, "model": "r410"}],
        "r410_covers": [cover],
    }
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    await _open_cover_management(options)
    await options.async_step_edit_r410_cover({"id": cover["id"]})
    await options.async_step_cover_name({"name": "Changed name"})
    await options.async_step_cover_class({"device_class": "shutter"})
    assert entry.options == original
    assert (await options.async_step_cancel_cover())["step_id"] == (
        "manage_r410_covers"
    )
    assert entry.options == original


async def test_removing_r410_cascades_cover_only_after_confirmation(
    options, entry, hass
):
    cover = _cover_options()
    original = {
        **entry.options,
        "devices": [
            {"slave": 63, "model": "r410"},
            {"slave": 64, "model": "r410"},
        ],
        "r410_covers": [cover],
        "future_option": {"preserve": True},
    }
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    await _select_modbus(options, 63)
    summary = await options.async_step_remove_managed_device()
    assert "Garage" in summary["description_placeholders"]["covers"]
    assert entry.options == original
    saved = await options.async_step_confirm_remove()
    assert saved["data"]["devices"] == [{"slave": 64, "model": "r410"}]
    assert saved["data"]["r410_covers"] == []
    assert saved["data"]["future_option"] == {"preserve": True}


async def test_r410_management_has_no_model_change_and_remove_cascades_cover(
    options, entry, hass
):
    cover = _cover_options()
    original = {
        **entry.options,
        "devices": [{"slave": 63, "model": "r410"}],
        "r410_covers": [cover],
        "future_option": "preserve",
    }
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    menu = await _select_modbus(options, 63)
    assert "output_mode" not in menu["menu_options"]
    summary = await options.async_step_remove_managed_device()
    assert "Garage" in summary["description_placeholders"]["covers"]
    assert entry.options == original
    saved = await options.async_step_confirm_remove()
    assert saved["data"]["devices"] == []
    assert saved["data"]["r410_covers"] == []
    assert saved["data"]["future_option"] == "preserve"


async def test_corrupt_persisted_cover_options_return_controlled_abort(
    options, entry, hass
):
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "devices": [{"slave": 63, "model": "r410"}],
            "r410_covers": [{"id": "broken"}],
        },
    )
    result = await options.async_step_init()
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "invalid_stored_options"


async def test_empty_management_and_stale_cover_selections_are_controlled(
    options, entry, hass
):
    hass.config.language = "zh-Hant"
    original = {
        **entry.options,
        "devices": [{"slave": 1, "model": "p404"}],
        "r410_covers": [],
    }
    hass.config_entries.async_update_entry(entry, options=original)
    await _options_ready(options)
    await _select_modbus(options, 1)
    management = await options.async_step_r410_output_configuration()
    assert management["step_id"] == "device_menu"
    assert (await options.async_step_edit_r410_cover())["step_id"] == "edit_r410_cover"
    assert (await options.async_step_remove_r410_cover())["step_id"] == (
        "remove_r410_cover"
    )
    assert (await options.async_step_remove_cover_summary())["step_id"] == (
        "device_menu"
    )
    assert (await options.async_step_confirm_remove_cover())["step_id"] == (
        "device_menu"
    )
    assert (await options.async_step_back_to_r410_output_configuration())[
        "step_id"
    ] == ("device_menu")

    cover = _cover_options()
    configured = {
        **original,
        "devices": [
            {"slave": 63, "model": "r410"},
            {"slave": 1, "model": "p404"},
        ],
        "r410_covers": [cover],
    }
    hass.config_entries.async_update_entry(entry, options=configured)
    await _options_ready(options)
    management = await _open_r410_outputs(options, 63)
    assert "R3：一般 Relay" in management["description_placeholders"]["allocations"]
    assert "R4：一般 Relay" in management["description_placeholders"]["allocations"]
    assert management["menu_options"] == [
        "add_r410_cover",
        "manage_r410_covers",
        "back_to_device_menu",
    ]
    await options.async_step_manage_r410_covers()
    assert (await options.async_step_edit_r410_cover())["step_id"] == (
        "edit_r410_cover"
    )
    assert (await options.async_step_edit_r410_cover({"id": "missing"}))["errors"] == {
        "id": "cover_not_found"
    }
    assert (await options.async_step_remove_r410_cover())["step_id"] == (
        "remove_r410_cover"
    )
    assert (await options.async_step_remove_r410_cover({"id": "missing"}))[
        "errors"
    ] == {"id": "cover_not_found"}


async def test_three_relay_mode_requires_three_available_relays(options, entry, hass):
    first = _cover_options(open_relay=1, close_relay=2)
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "devices": [{"slave": 63, "model": "r410"}],
            "r410_covers": [first],
        },
    )
    await _options_ready(options)
    await _start_add_cover(options)
    await options.async_step_cover_name({"name": "Second cover"})
    await options.async_step_cover_class({})
    result = await options.async_step_cover_mode({"mode": "three_relay"})
    assert result["errors"] == {"base": "insufficient_cover_relays"}


async def test_cover_summary_and_confirmation_revalidate_allocations(
    options, entry, hass
):
    existing = _cover_options()
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "devices": [{"slave": 63, "model": "r410"}],
            "r410_covers": [existing],
        },
    )
    await _options_ready(options)
    await _start_add_cover(options)
    await _continue_add_cover(options, mapping={"open_relay": "1", "close_relay": "2"})
    result = await options.async_step_cover_summary()
    assert result["errors"] == {"base": "invalid_cover_relays"}

    options._cover_draft = dict(existing)
    options._cover_action = "add"
    result = await options.async_step_confirm_cover()
    assert result["step_id"] == "r410_output_configuration"

    # A concurrently-added Cover may consume the draft's relays before confirmation.
    concurrent = _cover_options(open_relay=3, close_relay=4)
    draft = _cover_options(open_relay=3, close_relay=4)
    options._cover_draft = draft
    options._cover_action = "add"
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "r410_covers": [existing, concurrent],
        },
    )
    result = await options.async_step_confirm_cover()
    assert result["step_id"] == "cover_mapping"
    assert result["errors"] == {"base": "invalid_cover_relays"}


async def test_edit_missing_cover_and_remove_confirmation_guards(options, entry, hass):
    cover = _cover_options()
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            "devices": [{"slave": 63, "model": "r410"}],
            "r410_covers": [cover],
        },
    )
    await _options_ready(options)
    await _open_r410_outputs(options)
    options._cover_draft = {}
    assert (await options.async_step_confirm_cover())["step_id"] == (
        "r410_output_configuration"
    )
    options._cover_action = "edit"
    options._cover_draft = _cover_options()
    result = await options.async_step_confirm_cover()
    assert result["step_id"] == "r410_output_configuration"

    options._cover_action = "edit"
    options._cover_draft = {**cover, "id": uuid4().hex}
    result = await options.async_step_confirm_cover()
    assert result["step_id"] == "r410_output_configuration"

    second = _cover_options(open_relay=3, close_relay=4)
    hass.config_entries.async_update_entry(
        entry,
        options={**entry.options, "r410_covers": [cover, second]},
    )
    await _options_ready(options)
    await _open_cover_management(options)
    assert (await options.async_step_edit_r410_cover())["step_id"] == (
        "edit_r410_cover"
    )
    result = await options.async_step_edit_r410_cover({"id": cover["id"]})
    assert result["step_id"] == "cover_name"
    options._cover_action = "edit"
    options._cover_draft = {**cover, "id": uuid4().hex}
    result = await options.async_step_confirm_cover()
    assert result["step_id"] == "r410_output_configuration"
    assert entry.options["r410_covers"] == [cover, second]
