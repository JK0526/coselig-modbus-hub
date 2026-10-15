"""Native gateway, scan, profile, options and reconfigure flows."""

from __future__ import annotations

import asyncio
import copy
import ipaddress
import re
from typing import Any
from uuid import uuid4

import probatio as vol
from homeassistant import config_entries
from homeassistant.components.cover import CoverDeviceClass
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SCAN_END,
    DEFAULT_SCAN_START,
    DOMAIN,
)
from .coordinator import valid_poll_interval
from .models import Device, GroupMode, Model, devices_from_options
from .r410_cover import (
    R410CoverConfig,
    R410CoverConfigError,
    R410CoverMode,
    parse_r410_cover,
    r410_covers_from_options,
)
from .s402 import S402_ID_MAX, S402_ID_MIN, s402_devices_from_options
from .scanner import ScanResult, scan
from .transport import GatewayError, Transport

MODEL_LABELS = {
    Model.P404: "P404 — 4 路 PWM 調光器",
    Model.P210: "P210 — 2 路 PWM 調光器",
    Model.R410: "R410 — 4 路 Relay",
}
GROUP_LABELS = {
    GroupMode.SINGLE: "兩路獨立調光",
    GroupMode.CCT: "一組雙色溫",
}


def valid_host(value: str) -> str:
    value = value.strip().lower().rstrip(".")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if (
            not value
            or len(value) > 253
            or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                for part in value.split(".")
            )
        ):
            raise ValueError("Invalid host") from None
    return value


def gateway_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("host", default=defaults.get("host", "")): str,
            vol.Required("port", default=defaults.get("port", DEFAULT_PORT)): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
        }
    )


async def validate_gateway(data: dict[str, Any]) -> dict[str, Any]:
    validated = {"host": valid_host(data["host"]), "port": data["port"]}
    transport = Transport(**validated)
    try:
        await transport.connect()
    finally:
        await transport.close()
    return validated


def select(
    options: list[dict[str, str]], *, multiple: bool = False
) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=options,
            multiple=multiple,
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


class ProfileFlow:
    """Shared scan/profile steps. Persisted devices change only on confirmation."""

    def __init__(self) -> None:
        super().__init__()
        self._gateway: dict[str, Any] = {}
        self._options: dict[str, Any] = {
            "poll_interval": DEFAULT_POLL_INTERVAL,
            "scan_start": DEFAULT_SCAN_START,
            "scan_end": DEFAULT_SCAN_END,
        }
        self._existing: dict[int, Device] = {}
        self._existing_raw: dict[int, dict[str, Any]] = {}
        self._existing_raw_devices: list[dict[str, Any]] = []
        self._found: list[int] = []
        self._detections: dict[int, ScanResult] = {}
        self._selected: list[int] = []
        self._working_devices: list[Device] = []
        self._index = 0
        self._group_index = 0
        self._flow_action = "add"
        self._edit_old: Device | None = None
        self._remove_selected: list[int] = []
        self._scan_task: asyncio.Task[list[ScanResult]] | None = None
        self._progress = {
            "current": "0",
            "total": str(DEFAULT_SCAN_END - DEFAULT_SCAN_START + 1),
            "found": "0",
        }
        self._scan_error = False
        self._s402_ids: list[int] = []
        self._s402_pending_id: int | None = None
        self._s402_remove_selected: list[int] = []
        self._r410_covers: tuple[R410CoverConfig, ...] = ()
        self._cover_action = "add"
        self._cover_original: R410CoverConfig | None = None
        self._cover_draft: dict[str, Any] = {}
        self._cover_remove_id: str | None = None

    def _scan_progress(self, current: int, total: int, found: int) -> None:
        self._progress = {
            "current": str(current),
            "total": str(total),
            "found": str(found),
        }
        self.async_update_progress(current / total)
        self.async_notify_flow_changed()

    async def _run_scan(self) -> list[ScanResult]:
        # Publish the native progress step before an eager HA task can finish.
        await asyncio.sleep(0)
        start, end = self._options["scan_start"], self._options["scan_end"]
        if isinstance(self, CoseligOptionsFlow) and hasattr(
            self.config_entry, "runtime_data"
        ):
            return await self.config_entry.runtime_data.scan(
                start, end, self._scan_progress
            )
        transport = Transport(**self._gateway)
        try:
            return await scan(transport, start, end, self._scan_progress)
        finally:
            await transport.close()

    async def async_step_scan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._scan_task is None:
            self._scan_error = False
            self._scan_task = self.hass.async_create_task(
                self._run_scan(), "coselig-scan"
            )
        if not self._scan_task.done():
            return self.async_show_progress(
                step_id="scan",
                progress_action="scan",
                description_placeholders=self._progress,
                progress_task=self._scan_task,
            )
        try:
            results = self._scan_task.result()
            self._detections = {result.slave: result for result in results}
            self._found = sorted(self._detections)
        except GatewayError:
            self._scan_error = True
        self._scan_task = None
        if self._scan_error or (not self._found and not self._existing):
            next_step = "scan_failed"
        elif isinstance(self, CoseligOptionsFlow) and not self._new_slaves():
            next_step = "scan_no_new"
        else:
            next_step = "scan_result"
        return self.async_show_progress_done(next_step_id=next_step)

    async def async_step_scan_no_new(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="scan_no_new",
            menu_options=["rescan", "scan_range", "back_to_device_management"],
            description_placeholders={
                "existing": self._existing_description(),
                "missing": self._missing_description(),
            },
        )

    async def async_step_rescan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_scan_range()

    async def async_step_scan_range(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input["scan_start"] > user_input["scan_end"]:
                errors["base"] = "invalid_range"
            else:
                self._options.update(user_input)
                self._progress = {
                    "current": "0",
                    "total": str(user_input["scan_end"] - user_input["scan_start"] + 1),
                    "found": "0",
                }
                return await self.async_step_scan()
        schema = vol.Schema(
            {
                vol.Required(key, default=self._options[key]): vol.All(
                    int, vol.Range(min=1, max=247)
                )
                for key in ("scan_start", "scan_end")
            }
        )
        return self.async_show_form(
            step_id="scan_range", data_schema=schema, errors=errors
        )

    def _new_slaves(self) -> list[int]:
        return sorted(set(self._found) - set(self._existing))

    def _existing_description(self) -> str:
        if not self._existing:
            return "—"
        return "\n".join(
            f"Slave {slave} · {device.model.value.upper()}"
            for slave, device in sorted(self._existing.items())
        )

    def _missing_description(self) -> str:
        missing = sorted(set(self._existing) - set(self._found))
        if not missing:
            return "—"
        return "\n".join(
            f"Slave {slave} · {self._existing[slave].model.value.upper()} · 未在線"
            for slave in missing
        )

    async def async_step_scan_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        new_slaves = (
            self._new_slaves() if isinstance(self, CoseligOptionsFlow) else self._found
        )
        if user_input is not None:
            self._selected = sorted(int(value) for value in user_input["devices"])
            if self._s402_ids and 1 in self._selected:
                return self.async_show_form(
                    step_id="scan_result",
                    errors={"base": "reserved_slave_one"},
                    data_schema=self._scan_result_schema(new_slaves),
                    description_placeholders=self._scan_result_placeholders(),
                )
            if self._selected:
                self._flow_action = "add"
                self._edit_old = None
                return await self._start_profiles()
            return self.async_show_form(
                step_id="scan_result",
                errors={"devices": "no_devices"},
                data_schema=self._scan_result_schema(new_slaves),
                description_placeholders=self._scan_result_placeholders(),
            )
        if isinstance(self, CoseligOptionsFlow) and not new_slaves:
            return await self.async_step_scan_no_new()
        return self.async_show_form(
            step_id="scan_result",
            data_schema=self._scan_result_schema(new_slaves),
            description_placeholders=self._scan_result_placeholders(),
        )

    def _scan_result_schema(self, new_slaves: list[int]) -> vol.Schema:
        options = [
            {"value": str(slave), "label": f"Slave {slave} · New"}
            for slave in new_slaves
            if not self._s402_ids or slave != 1
        ]
        return vol.Schema(
            {vol.Required("devices", default=[]): select(options, multiple=True)}
        )

    def _scan_result_placeholders(self) -> dict[str, str]:
        return {
            "existing": self._existing_description(),
            "missing": self._missing_description(),
            "reserved": (
                "Slave #1 is reserved while S402 4x mode is enabled."
                if self._s402_ids
                else "—"
            ),
        }

    async def async_step_setup_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the first device type without forcing a Modbus scan."""
        return self.async_show_menu(
            step_id="setup_devices",
            menu_options=["search_modbus", "add_s402"],
        )

    async def async_step_search_modbus(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._flow_action = "add"
        return await self.async_step_scan_range()

    async def async_step_add_s402(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                device_id = int(user_input["s402_id"])
            except TypeError, ValueError:
                device_id = None
            if device_id is None or not S402_ID_MIN <= device_id <= S402_ID_MAX:
                errors["s402_id"] = "invalid_s402_id"
            elif device_id in self._s402_ids:
                errors["s402_id"] = "duplicate_s402_id"
            elif 1 in self._existing:
                errors["base"] = "reserved_slave_one"
            else:
                self._s402_pending_id = device_id
                return await self.async_step_s402_summary()
        return self.async_show_form(
            step_id="add_s402",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("s402_id"): vol.All(
                        vol.Coerce(int), vol.Range(min=S402_ID_MIN, max=S402_ID_MAX)
                    )
                }
            ),
        )

    async def async_step_s402_summary(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        device_id = self._s402_pending_id
        if device_id is None:
            return await self.async_step_add_s402()
        return self.async_show_menu(
            step_id="s402_summary",
            menu_options=["confirm_s402", "cancel_s402"],
            description_placeholders={
                "summary": (
                    f"Coselig S402 #{device_id}\n"
                    "DI1：單擊事件\nDI2：單擊事件\n"
                    "DI3：單擊事件\nDI4：單擊事件"
                )
            },
        )

    async def async_step_confirm_s402(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        device_id = self._s402_pending_id
        if device_id is None:
            return await self.async_step_add_s402()
        if isinstance(self, CoseligOptionsFlow):
            updated = self._copy_options()
            existing = list(updated.get("s402_devices", []))
            existing.append({"id": device_id})
            updated["s402_devices"] = existing
            return self.async_create_entry(data=updated)
        self._options["devices"] = []
        self._options["s402_devices"] = [{"id": device_id}]
        return self.async_create_entry(
            title=f"Coselig {self._gateway['host']}",
            data=self._gateway,
            options=self._options,
        )

    async def async_step_cancel_s402(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._s402_pending_id = None
        if isinstance(self, CoseligOptionsFlow):
            return await self.async_step_s402_management()
        return self.async_abort(reason="user_cancelled")

    async def _start_profiles(self) -> ConfigFlowResult:
        self._working_devices = []
        self._index = self._group_index = 0
        return await self.async_step_model()

    def _model_candidates(self, slave: int) -> tuple[Model, ...]:
        if self._flow_action == "edit":
            return tuple(Model)
        detection = self._detections.get(slave, ScanResult(slave))
        return detection.candidates

    def _make_profile_device(self, slave: int, model: Model) -> Device:
        old = self._edit_old if self._flow_action == "edit" else None
        groups: dict[str, GroupMode] = {}
        if old is not None and old.model == model:
            groups = {field: getattr(old, field) for field in old.profile.group_fields}
        return Device(slave, model, **groups)

    async def async_step_model(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._index >= len(self._selected):
            self._group_index = 0
            return await self.async_step_groups()
        slave = self._selected[self._index]
        candidates = self._model_candidates(slave)
        detection = self._detections.get(slave, ScanResult(slave))
        if len(candidates) == 1:
            self._working_devices.append(
                self._make_profile_device(slave, candidates[0])
            )
            self._index += 1
            return await self.async_step_model()
        if user_input is not None:
            model = Model(user_input["model"])
            self._working_devices.append(self._make_profile_device(slave, model))
            self._index += 1
            return await self.async_step_model()
        old = self._edit_old if self._flow_action == "edit" else None
        default = old.model if old else detection.recommended or Model.P404
        return self.async_show_form(
            step_id="model",
            description_placeholders={
                "slave": str(slave),
                "family": detection.family.value,
                "recommended": detection.recommended.value.upper()
                if detection.recommended
                else "—",
                "model_reason": (
                    "目前可確認為 PWM 調光器，但 P404／P210 無法由協議可靠自動區分，請確認設備型號。"
                    if detection.family.value == "dimmer"
                    else "請依設備標示確認型號；Unknown 設備不會由 Scanner 自動猜測。"
                ),
            },
            data_schema=vol.Schema(
                {
                    vol.Required("model", default=default.value): select(
                        [
                            {"value": model.value, "label": MODEL_LABELS[model]}
                            for model in candidates
                        ]
                    )
                }
            ),
        )

    async def async_step_groups(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        indices = [
            index
            for index, device in enumerate(self._working_devices)
            if device.profile.group_fields
        ]
        if self._group_index >= len(indices):
            return self._show_profile_summary()
        index = indices[self._group_index]
        device = self._working_devices[index]
        if user_input is not None:
            self._working_devices[index] = Device(
                device.slave,
                device.model,
                **{
                    field: GroupMode(user_input[field])
                    for field in device.profile.group_fields
                },
            )
            self._group_index += 1
            return await self.async_step_groups()
        fields: dict[Any, Any] = {
            vol.Required(field, default=getattr(device, field).value): select(
                [
                    {"value": mode.value, "label": GROUP_LABELS[mode]}
                    for mode in GroupMode
                ]
            )
            for field in device.profile.group_fields
        }
        return self.async_show_form(
            step_id="groups",
            description_placeholders={"slave": str(device.slave)},
            data_schema=vol.Schema(fields),
        )

    def _device_summary(self, device: Device) -> str:
        profile = device.profile
        if device.model == Model.R410:
            return f"Slave {device.slave}\nR410 · 4 Relay"
        modes = []
        for index, field in enumerate(profile.group_fields):
            group_label = "輸出 1＋2" if index == 0 else "輸出 3＋4"
            modes.append(f"{group_label}：{GROUP_LABELS[getattr(device, field)]}")
        output_names = "、".join(output.name for output in device.outputs)
        model_label = "4 路 PWM" if device.model == Model.P404 else "2 路 PWM"
        return (
            f"Slave {device.slave}\n{device.model.value.upper()} · {model_label}\n"
            f"{chr(10).join(modes)}\n輸出：{output_names}"
        )

    def _show_profile_summary(self) -> ConfigFlowResult:
        summaries = [self._device_summary(device) for device in self._working_devices]
        if self._flow_action == "edit" and self._edit_old:
            changed = self._working_devices[0]
            body = (
                f"目前：{self._edit_old.model.value.upper()}\n"
                f"變更為：{changed.model.value.upper()}\n"
                f"原輸出：{', '.join(output.name for output in self._edit_old.outputs)}\n"
                f"新輸出設定：\n{self._device_summary(changed)}"
            )
            return self.async_show_menu(
                step_id="edit_summary",
                menu_options=["confirm_edit", "cancel_edit"],
                description_placeholders={
                    "summary": body,
                    "cover_removals": self._cover_removals_for_devices(
                        {changed.slave} if changed.model != Model.R410 else set()
                    ),
                },
            )
        return self.async_show_menu(
            step_id="add_summary",
            menu_options=["confirm_add", "cancel_add"],
            description_placeholders={
                "summary": "\n\n".join(summaries),
                "count": str(len(summaries)),
            },
        )

    async def async_step_add_summary(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self._show_profile_summary()

    async def async_step_edit_summary(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self._show_profile_summary()

    async def async_step_confirm_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self._persist_add()

    async def async_step_confirm_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self._persist_edit()

    async def async_step_cancel_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self._return_to_device_management()

    async def async_step_cancel_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self._return_to_device_management()

    async def async_step_cancel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if isinstance(self, CoseligOptionsFlow):
            return await self.async_step_device_management()
        return self.async_abort(reason="user_cancelled")

    async def async_step_back_to_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_init()

    async def async_step_back_to_device_management(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_setup_devices()

    def _persist_add(self) -> ConfigFlowResult:
        self._options["devices"] = [
            device.as_dict() for device in self._working_devices
        ]
        return self.async_create_entry(
            title=f"Coselig {self._gateway['host']}",
            data=self._gateway,
            options=self._options,
        )

    def _persist_edit(self) -> ConfigFlowResult:
        return self.async_abort(reason="user_cancelled")

    def _return_to_device_management(self) -> ConfigFlowResult:
        return self.async_abort(reason="user_cancelled")

    async def async_step_scan_failed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="scan_failed",
            menu_options=["rescan", "scan_range", "cancel"],
            description_placeholders={
                "reason": "Gateway connection lost"
                if self._scan_error
                else "No slaves found"
            },
        )

    @callback
    def async_remove(self) -> None:
        if self._scan_task is not None:
            self._scan_task.cancel()
        super().async_remove()


class ConfigFlow(ProfileFlow, config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2
    MINOR_VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            try:
                self._gateway = await validate_gateway(user_input)
            except ValueError:
                errors["host"] = "invalid_host"
            except GatewayError:
                errors["base"] = "cannot_connect"
            else:
                self._async_abort_entries_match(self._gateway)
                return await self.async_step_setup_devices()
        return self.async_show_form(
            step_id="user", data_schema=gateway_schema(user_input or {}), errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            try:
                gateway = await validate_gateway(user_input)
            except ValueError:
                errors["host"] = "invalid_host"
            except GatewayError:
                errors["base"] = "cannot_connect"
            else:
                self._async_abort_entries_match(gateway)
                return self.async_update_reload_and_abort(
                    entry, data=gateway, title=f"Coselig {gateway['host']}"
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=gateway_schema(user_input or dict(entry.data)),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> CoseligOptionsFlow:
        return CoseligOptionsFlow()


class CoseligOptionsFlow(ProfileFlow, config_entries.OptionsFlowWithReload):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._gateway = dict(self.config_entry.data)
        self._options = {
            "poll_interval": DEFAULT_POLL_INTERVAL,
            "scan_start": DEFAULT_SCAN_START,
            "scan_end": DEFAULT_SCAN_END,
            **copy.deepcopy(dict(self.config_entry.options)),
        }
        try:
            devices = devices_from_options(self.config_entry.options)
            self._existing = {device.slave: device for device in devices}
            self._r410_covers = r410_covers_from_options(self._options, devices)
        except R410CoverConfigError, KeyError, ValueError, TypeError:
            return self.async_abort(reason="invalid_stored_options")
        self._s402_ids = list(
            s402_devices_from_options(dict(self.config_entry.options))
        )
        raw_devices = self.config_entry.options.get("devices", [])
        self._existing_raw_devices = copy.deepcopy(raw_devices)
        self._existing_raw = {
            int(device["slave"]): copy.deepcopy(device) for device in raw_devices
        }
        return self.async_show_menu(
            step_id="init", menu_options=["device_management", "communication"]
        )

    async def async_step_device_management(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="device_management",
            menu_options=[
                "add_devices",
                "edit_devices",
                "remove_devices",
                "s402_management",
                "r410_cover_management",
                "back_to_options",
            ],
        )

    def _cover_relay_pool(self, slave: int) -> tuple[int, ...]:
        reserved = set()
        for cover in self._r410_covers:
            if cover.slave == slave and (
                self._cover_original is None or cover.id != self._cover_original.id
            ):
                reserved.update(cover.relays)
        return tuple(relay for relay in range(1, 5) if relay not in reserved)

    def _cover_candidate_slaves(self) -> tuple[int, ...]:
        return tuple(
            slave
            for slave, device in sorted(self._existing.items())
            if device.model == Model.R410 and len(self._cover_relay_pool(slave)) >= 2
        )

    def _cover_allocations_description(self) -> str:
        lines = []
        for cover in self._r410_covers:
            stop = (
                " + ".join(f"R{relay}" for relay in cover.relays)
                if cover.mode == R410CoverMode.TWO_RELAY
                else f"R{cover.stop_relay}"
            )
            lines.append(
                f"R410 #{cover.slave} · {cover.name} · "
                f"{cover.mode.value} · 開 R{cover.open_relay} · "
                f"停 {stop} · 關 R{cover.close_relay} · "
                f"脈衝 {cover.pulse_tenths / 10:.1f} 秒"
            )
        for slave, device in sorted(self._existing.items()):
            if device.model != Model.R410:
                continue
            free = self._cover_relay_pool(slave)
            lines.append(
                f"R410 #{slave} 一般 Switch："
                + ("、".join(f"Relay {relay}" for relay in free) if free else "無")
            )
        return "\n".join(lines) or "尚未設定 R410。至少需要 2 個未使用 Relay。"

    async def async_step_r410_cover_management(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = []
        if self._cover_candidate_slaves():
            options.append("add_r410_cover")
        if self._r410_covers:
            options.extend(("edit_r410_cover", "remove_r410_cover"))
        if not options:
            options.append("back_to_device_management")
        else:
            options.append("back_to_device_management")
        return self.async_show_menu(
            step_id="r410_cover_management",
            menu_options=options,
            description_placeholders={
                "allocations": self._cover_allocations_description()
            },
        )

    async def async_step_add_r410_cover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        slaves = self._cover_candidate_slaves()
        if not slaves:
            return self.async_show_menu(
                step_id="add_r410_cover",
                menu_options=["back_to_r410_cover_management"],
                description_placeholders={"reason": "至少需要 2 個未使用 Relay。"},
            )
        options = [{"value": str(slave), "label": f"R410 #{slave}"} for slave in slaves]
        if user_input is not None:
            try:
                slave = int(user_input["slave"])
            except KeyError, TypeError, ValueError:
                slave = None
            if slave not in slaves:
                return self.async_show_form(
                    step_id="add_r410_cover",
                    errors={"slave": "cover_r410_unavailable"},
                    data_schema=vol.Schema({vol.Required("slave"): select(options)}),
                )
            self._cover_action = "add"
            self._cover_original = None
            self._cover_draft = {"id": uuid4().hex, "slave": slave}
            return await self.async_step_cover_name()
        return self.async_show_form(
            step_id="add_r410_cover",
            data_schema=vol.Schema({vol.Required("slave"): select(options)}),
        )

    async def async_step_edit_r410_cover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not self._r410_covers:
            return self.async_show_menu(
                step_id="edit_r410_cover",
                menu_options=["back_to_r410_cover_management"],
                description_placeholders={"covers": "尚未設定 Cover。"},
            )
        options = [
            {"value": cover.id, "label": f"R410 #{cover.slave} · {cover.name}"}
            for cover in self._r410_covers
        ]
        if user_input is not None:
            selected = next(
                (cover for cover in self._r410_covers if cover.id == user_input["id"]),
                None,
            )
            if selected is None:
                return self.async_show_form(
                    step_id="edit_r410_cover",
                    errors={"id": "cover_not_found"},
                    data_schema=vol.Schema({vol.Required("id"): select(options)}),
                )
            self._cover_action = "edit"
            self._cover_original = selected
            self._cover_draft = selected.as_dict()
            return await self.async_step_cover_name()
        return self.async_show_form(
            step_id="edit_r410_cover",
            data_schema=vol.Schema({vol.Required("id"): select(options)}),
        )

    async def async_step_cover_name(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            name = user_input.get("name")
            if not isinstance(name, str) or not name.strip():
                errors["name"] = "invalid_cover_name"
            else:
                self._cover_draft["name"] = name.strip()
                return await self.async_step_cover_class()
        return self.async_show_form(
            step_id="cover_name",
            errors=errors,
            data_schema=vol.Schema(
                {vol.Required("name", default=self._cover_draft.get("name", "")): str}
            ),
        )

    async def async_step_cover_class(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            device_class = user_input.get("device_class")
            try:
                self._cover_draft["device_class"] = (
                    None
                    if device_class in (None, "")
                    else CoverDeviceClass(device_class).value
                )
            except TypeError, ValueError:
                errors["device_class"] = "invalid_cover_device_class"
            else:
                return await self.async_step_cover_mode()
        schema: dict[Any, Any] = {
            vol.Optional("device_class"): selector.DeviceClassSelector(
                selector.DeviceClassSelectorConfig(domain=Platform.COVER)
            )
        }
        return self.async_show_form(
            step_id="cover_class", errors=errors, data_schema=vol.Schema(schema)
        )

    async def async_step_cover_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            try:
                mode = R410CoverMode(user_input["mode"])
            except KeyError, TypeError, ValueError:
                errors["mode"] = "invalid_cover_mode"
            else:
                self._cover_draft["mode"] = mode.value
                if (
                    mode == R410CoverMode.THREE_RELAY
                    and len(self._cover_relay_pool(self._cover_draft["slave"])) < 3
                ):
                    errors["base"] = "insufficient_cover_relays"
                else:
                    if mode == R410CoverMode.TWO_RELAY:
                        self._cover_draft.pop("stop_relay", None)
                    return await self.async_step_cover_mapping()
        return self.async_show_form(
            step_id="cover_mode",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "mode",
                        default=self._cover_draft.get(
                            "mode", R410CoverMode.TWO_RELAY.value
                        ),
                    ): select(
                        [
                            {"value": mode.value, "label": mode.value.replace("_", " ")}
                            for mode in R410CoverMode
                        ]
                    )
                }
            ),
        )

    def _cover_mapping_fields(self) -> tuple[str, ...]:
        if self._cover_draft.get("mode") == R410CoverMode.THREE_RELAY.value:
            return ("open_relay", "stop_relay", "close_relay")
        return ("open_relay", "close_relay")

    def _cover_mapping_schema(self) -> vol.Schema:
        slave = self._cover_draft["slave"]
        pool = self._cover_relay_pool(slave)
        fields: dict[Any, Any] = {}
        already_selected: set[int] = set()
        for field in self._cover_mapping_fields():
            current = self._cover_draft.get(field)
            if current not in pool or current in already_selected:
                current = next(relay for relay in pool if relay not in already_selected)
            already_selected.add(current)
            fields[vol.Required(field, default=str(current))] = select(
                [{"value": str(relay), "label": f"Relay {relay}"} for relay in pool]
            )
        return vol.Schema(fields)

    async def async_step_cover_mapping(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            try:
                relays = {
                    field: int(user_input[field])
                    for field in self._cover_mapping_fields()
                }
            except KeyError, TypeError, ValueError:
                relays = {}
            pool = self._cover_relay_pool(self._cover_draft["slave"])
            if (
                len(relays) != len(self._cover_mapping_fields())
                or len(set(relays.values())) != len(relays)
                or not set(relays.values()).issubset(pool)
            ):
                errors["base"] = "invalid_cover_relays"
            else:
                self._cover_draft.update(relays)
                return await self.async_step_cover_pulse()
        return self.async_show_form(
            step_id="cover_mapping",
            errors=errors,
            data_schema=self._cover_mapping_schema(),
        )

    async def async_step_cover_pulse(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            try:
                pulse = float(user_input["pulse_seconds"])
            except KeyError, TypeError, ValueError:
                pulse = 0.0
            tenths = round(pulse * 10)
            if not 0.1 <= pulse <= 25.0 or abs(pulse * 10 - tenths) > 1e-7:
                errors["pulse_seconds"] = "invalid_cover_pulse"
            else:
                self._cover_draft["pulse_tenths"] = tenths
                return await self.async_step_cover_summary()
        return self.async_show_form(
            step_id="cover_pulse",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "pulse_seconds",
                        default=self._cover_draft.get("pulse_tenths", 5) / 10,
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0.1,
                            max=25.0,
                            step=0.1,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    )
                }
            ),
        )

    def _cover_summary_text(self, cover: R410CoverConfig) -> tuple[str, str]:
        stop = (
            " + ".join(f"R{relay}" for relay in (cover.open_relay, cover.close_relay))
            if cover.mode == R410CoverMode.TWO_RELAY
            else f"R{cover.stop_relay}"
        )
        summary = (
            f"{cover.name}\nR410 #{cover.slave}\n"
            f"類別：{cover.device_class.value if cover.device_class else '一般 Cover'}\n"
            f"模式：{cover.mode.value}\n開啟：R{cover.open_relay}\n"
            f"停止：{stop}\n關閉：R{cover.close_relay}\n"
            f"脈衝：{cover.pulse_tenths / 10:.1f} 秒"
        )
        previous = self._cover_original
        change = ""
        if previous is not None:
            released = sorted(set(previous.relays) - set(cover.relays))
            claimed = sorted(set(cover.relays) - set(previous.relays))
            change = (
                f"釋出一般 Relay：{', '.join(f'R{x}' for x in released) or '無'}\n"
                f"新增 Cover Relay：{', '.join(f'R{x}' for x in claimed) or '無'}"
            )
        reserved = set(cover.relays)
        reserved.update(
            relay
            for item in self._r410_covers
            if item.slave == cover.slave
            and (previous is None or item.id != previous.id)
            for relay in item.relays
        )
        free = [relay for relay in range(1, 5) if relay not in reserved]
        change = "\n".join(
            part
            for part in (
                change,
                "確認後的一般 Switch：" + ("、".join(f"R{x}" for x in free) or "無"),
            )
            if part
        )
        return summary, change

    async def async_step_cover_summary(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        try:
            cover = parse_r410_cover(self._cover_draft)
            devices = tuple(self._existing.values())
            r410_covers_from_options(
                {
                    "r410_covers": [
                        item.as_dict()
                        for item in self._r410_covers
                        if self._cover_original is None
                        or item.id != self._cover_original.id
                    ]
                    + [cover.as_dict()]
                },
                devices,
            )
        except R410CoverConfigError:
            return self.async_show_form(
                step_id="cover_mapping",
                errors={"base": "invalid_cover_relays"},
                data_schema=self._cover_mapping_schema(),
            )
        self._cover_draft = cover.as_dict()
        summary, resource_change = self._cover_summary_text(cover)
        return self.async_show_menu(
            step_id="cover_summary",
            menu_options=["confirm_cover", "cancel_cover"],
            description_placeholders={
                "summary": summary,
                "resource_change": resource_change,
            },
        )

    async def async_step_confirm_cover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        try:
            cover = parse_r410_cover(self._cover_draft)
        except R410CoverConfigError:
            return await self.async_step_r410_cover_management()
        updated = self._copy_options()
        records = copy.deepcopy(updated.get("r410_covers", []))
        if self._cover_action == "add":
            if any(parse_r410_cover(raw).id == cover.id for raw in records):
                return await self.async_step_r410_cover_management()
            records.append(cover.as_dict())
        else:
            found = False
            replaced = []
            for raw in records:
                existing = parse_r410_cover(raw)
                if existing.id == cover.id:
                    merged = copy.deepcopy(raw)
                    merged.update(cover.as_dict())
                    if cover.mode == R410CoverMode.TWO_RELAY:
                        merged.pop("stop_relay", None)
                    replaced.append(merged)
                    found = True
                else:
                    replaced.append(raw)
            if not found:
                return await self.async_step_r410_cover_management()
            records = replaced
        updated["r410_covers"] = records
        try:
            devices = devices_from_options(updated)
            r410_covers_from_options(updated, devices)
        except R410CoverConfigError, KeyError, ValueError, TypeError:
            return self.async_show_form(
                step_id="cover_mapping",
                errors={"base": "invalid_cover_relays"},
                data_schema=self._cover_mapping_schema(),
            )
        return self.async_create_entry(data=updated)

    async def async_step_cancel_cover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._cover_draft = {}
        self._cover_original = None
        return await self.async_step_r410_cover_management()

    async def async_step_remove_r410_cover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not self._r410_covers:
            return self.async_show_menu(
                step_id="remove_r410_cover",
                menu_options=["back_to_r410_cover_management"],
                description_placeholders={"covers": "尚未設定 Cover。"},
            )
        options = [
            {"value": cover.id, "label": f"R410 #{cover.slave} · {cover.name}"}
            for cover in self._r410_covers
        ]
        if user_input is not None:
            selected = next(
                (cover for cover in self._r410_covers if cover.id == user_input["id"]),
                None,
            )
            if selected is None:
                return self.async_show_form(
                    step_id="remove_r410_cover",
                    errors={"id": "cover_not_found"},
                    data_schema=vol.Schema({vol.Required("id"): select(options)}),
                )
            self._cover_remove_id = selected.id
            self._cover_original = selected
            return await self.async_step_remove_cover_summary()
        return self.async_show_form(
            step_id="remove_r410_cover",
            data_schema=vol.Schema({vol.Required("id"): select(options)}),
        )

    async def async_step_remove_cover_summary(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        cover = self._cover_original
        if cover is None:
            return await self.async_step_r410_cover_management()
        return self.async_show_menu(
            step_id="remove_cover_summary",
            menu_options=["confirm_remove_cover", "cancel_remove_cover"],
            description_placeholders={
                "name": cover.name,
                "slave": str(cover.slave),
                "relays": "、".join(f"R{relay}" for relay in cover.relays),
            },
        )

    async def async_step_confirm_remove_cover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._cover_remove_id is None:
            return await self.async_step_r410_cover_management()
        updated = self._copy_options()
        records = []
        for raw in updated.get("r410_covers", []):
            if parse_r410_cover(raw).id != self._cover_remove_id:
                records.append(copy.deepcopy(raw))
        updated["r410_covers"] = records
        return self.async_create_entry(data=updated)

    async def async_step_cancel_remove_cover(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._cover_remove_id = None
        self._cover_original = None
        return await self.async_step_r410_cover_management()

    async def async_step_back_to_r410_cover_management(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_r410_cover_management()

    async def async_step_s402_management(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="s402_management",
            menu_options=["add_s402", "remove_s402", "back_to_device_management"],
            description_placeholders={
                "devices": "\n".join(
                    f"S402 #{device_id}" for device_id in self._s402_ids
                )
                or "尚未設定 S402。"
            },
        )

    async def async_step_remove_s402(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not self._s402_ids:
            return self.async_show_menu(
                step_id="remove_s402",
                menu_options=["back_to_s402_management"],
                description_placeholders={"devices": "尚未設定 S402。"},
            )
        options = [
            {"value": str(device_id), "label": f"S402 #{device_id}"}
            for device_id in self._s402_ids
        ]
        if user_input is not None:
            self._s402_remove_selected = sorted(
                int(value) for value in user_input["devices"]
            )
            if not self._s402_remove_selected:
                return self.async_show_form(
                    step_id="remove_s402",
                    errors={"devices": "no_s402_to_remove"},
                    data_schema=vol.Schema(
                        {
                            vol.Required("devices", default=[]): select(
                                options, multiple=True
                            )
                        }
                    ),
                )
            return await self.async_step_s402_remove_summary()
        return self.async_show_form(
            step_id="remove_s402",
            data_schema=vol.Schema(
                {vol.Required("devices", default=[]): select(options, multiple=True)}
            ),
        )

    async def async_step_s402_remove_summary(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="s402_remove_summary",
            menu_options=["confirm_s402_remove", "cancel_s402_remove"],
            description_placeholders={
                "devices": "\n".join(
                    f"S402 #{device_id}" for device_id in self._s402_remove_selected
                )
            },
        )

    async def async_step_confirm_s402_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        updated = self._copy_options()
        updated["s402_devices"] = [
            copy.deepcopy(device)
            for device in updated.get("s402_devices", [])
            if int(device["id"]) not in self._s402_remove_selected
        ]
        return self.async_create_entry(data=updated)

    async def async_step_cancel_s402_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_s402_management()

    async def async_step_back_to_s402_management(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_s402_management()

    async def async_step_back_to_device_management(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_device_management()

    async def async_step_communication(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="communication",
            menu_options=["polling", "back_to_options"],
        )

    async def async_step_add_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._flow_action = "add"
        self._detections = {}
        self._found = []
        self._scan_task = None
        return await self.async_step_scan_range()

    async def async_step_edit_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not self._existing:
            return self.async_show_menu(
                step_id="edit_devices",
                menu_options=["back_to_device_management"],
                description_placeholders={"devices": "尚未設定設備。"},
            )
        if user_input is not None:
            slave = int(user_input["slave"])
            self._edit_old = self._existing[slave]
            self._flow_action = "edit"
            self._selected = [slave]
            return await self._start_profiles()
        options = [
            {
                "value": str(slave),
                "label": f"Slave {slave} · {device.model.value.upper()}",
            }
            for slave, device in sorted(self._existing.items())
        ]
        return self.async_show_form(
            step_id="edit_devices",
            data_schema=vol.Schema({vol.Required("slave"): select(options)}),
        )

    async def async_step_remove_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not self._existing:
            return self.async_show_menu(
                step_id="remove_devices",
                menu_options=["back_to_device_management"],
                description_placeholders={"devices": "尚未設定設備。"},
            )
        options = [
            {
                "value": str(slave),
                "label": f"Slave {slave} · {device.model.value.upper()}",
            }
            for slave, device in sorted(self._existing.items())
        ]
        if user_input is not None:
            self._remove_selected = sorted(
                int(value) for value in user_input["devices"]
            )
            if not self._remove_selected:
                return self.async_show_form(
                    step_id="remove_devices",
                    data_schema=vol.Schema(
                        {
                            vol.Required("devices", default=[]): select(
                                options, multiple=True
                            )
                        }
                    ),
                    errors={"devices": "no_devices_to_remove"},
                )
            return self.async_show_menu(
                step_id="remove_summary",
                menu_options=["confirm_remove", "cancel_remove"],
                description_placeholders={
                    "devices": "\n".join(
                        f"Slave {slave} · {self._existing[slave].model.value.upper()}"
                        for slave in self._remove_selected
                    ),
                    "covers": self._cover_removals_for_devices(
                        set(self._remove_selected)
                    ),
                },
            )
        return self.async_show_form(
            step_id="remove_devices",
            data_schema=vol.Schema(
                {vol.Required("devices", default=[]): select(options, multiple=True)}
            ),
        )

    async def async_step_confirm_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        updated = self._copy_options()
        updated["devices"] = [
            copy.deepcopy(device)
            for device in self._existing_raw_devices
            if int(device["slave"]) not in self._remove_selected
        ]
        if "r410_covers" in updated:
            updated["r410_covers"] = [
                copy.deepcopy(cover)
                for cover in updated["r410_covers"]
                if int(cover["slave"]) not in self._remove_selected
            ]
        return self.async_create_entry(data=updated)

    async def async_step_remove_summary(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="remove_summary",
            menu_options=["confirm_remove", "cancel_remove"],
            description_placeholders={
                "devices": "\n".join(
                    f"Slave {slave} · {self._existing[slave].model.value.upper()}"
                    for slave in self._remove_selected
                ),
                "covers": self._cover_removals_for_devices(set(self._remove_selected)),
            },
        )

    async def async_step_cancel_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_device_management()

    async def async_step_polling(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            interval = float(user_input["poll_interval"])
            if not valid_poll_interval(interval):
                errors["poll_interval"] = "invalid_poll_interval"
            else:
                updated = self._copy_options()
                updated["poll_interval"] = interval
                self.automatic_reload = False
                if hasattr(self.config_entry, "runtime_data"):
                    self.config_entry.runtime_data.coordinator.set_poll_interval(
                        interval
                    )
                return self.async_create_entry(data=updated)
        return self.async_show_form(
            step_id="polling",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "poll_interval", default=self._options["poll_interval"]
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=3600,
                            step=0.5,
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    )
                }
            ),
        )

    def _copy_options(self) -> dict[str, Any]:
        return copy.deepcopy(dict(self.config_entry.options))

    def _persist_add(self) -> ConfigFlowResult:
        updated = self._copy_options()
        existing_slaves = {device.slave for device in self._existing.values()}
        additions = [
            device.as_dict()
            for device in self._working_devices
            if device.slave not in existing_slaves
        ]
        updated["devices"] = [
            copy.deepcopy(device) for device in self._existing_raw_devices
        ] + additions
        if "scan_start" in self._options:
            updated["scan_start"] = self._options["scan_start"]
            updated["scan_end"] = self._options["scan_end"]
        return self.async_create_entry(data=updated)

    def _persist_edit(self) -> ConfigFlowResult:
        updated = self._copy_options()
        replacement = self._working_devices[0]
        updated["devices"] = [
            self._serialize_existing(replacement)
            if int(device["slave"]) == replacement.slave
            else copy.deepcopy(device)
            for device in self._existing_raw_devices
        ]
        if replacement.model != Model.R410 and "r410_covers" in updated:
            updated["r410_covers"] = [
                copy.deepcopy(cover)
                for cover in updated["r410_covers"]
                if int(cover["slave"]) != replacement.slave
            ]
        return self.async_create_entry(data=updated)

    def _cover_removals_for_devices(self, slaves: set[int]) -> str:
        names = [
            f"R410 #{cover.slave} · {cover.name} · "
            + "、".join(f"R{relay}" for relay in cover.relays)
            for cover in self._r410_covers
            if cover.slave in slaves
        ]
        return "\n".join(names) or "無"

    def _serialize_existing(self, device: Device) -> dict[str, Any]:
        """Update the selected device while retaining its unknown future fields."""
        raw = self._existing_raw.get(device.slave, {})
        known_fields = {"slave", "model", "group_a", "group_b"}
        return {
            **{
                key: copy.deepcopy(value)
                for key, value in raw.items()
                if key not in known_fields
            },
            **device.as_dict(),
        }

    def _return_to_device_management(self) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="device_management",
            menu_options=[
                "add_devices",
                "edit_devices",
                "remove_devices",
                "s402_management",
                "back_to_options",
            ],
        )
