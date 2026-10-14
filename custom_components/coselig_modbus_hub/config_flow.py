"""Native gateway, scan, profile, options and reconfigure flows."""

from __future__ import annotations

import asyncio
import ipaddress
import re
from typing import Any

import probatio as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
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
from .scanner import scan
from .transport import GatewayError, Transport


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
    """Shared scan/profile steps, without a second runtime or persistence layer."""

    def __init__(self) -> None:
        super().__init__()
        self._gateway: dict[str, Any] = {}
        self._options: dict[str, Any] = {
            "poll_interval": DEFAULT_POLL_INTERVAL,
            "scan_start": DEFAULT_SCAN_START,
            "scan_end": DEFAULT_SCAN_END,
        }
        self._existing: dict[int, Device] = {}
        self._found: list[int] = []
        self._selected: list[int] = []
        self._devices: list[Device] = []
        self._index = 0
        self._group_index = 0
        self._scan_task: asyncio.Task[list[int]] | None = None
        self._progress = {
            "current": "0",
            "total": str(DEFAULT_SCAN_END - DEFAULT_SCAN_START + 1),
            "found": "0",
        }
        self._scan_error = False

    def _scan_progress(self, current: int, total: int, found: int) -> None:
        self._progress = {
            "current": str(current),
            "total": str(total),
            "found": str(found),
        }
        self.async_update_progress(current / total)
        self.async_notify_flow_changed()

    async def _run_scan(self) -> list[int]:
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
            self._found = self._scan_task.result()
        except GatewayError:
            self._scan_error = True
        self._scan_task = None
        next_step = (
            "scan_failed"
            if self._scan_error or (not self._found and not self._existing)
            else "scan_result"
        )
        return self.async_show_progress_done(next_step_id=next_step)

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

    async def async_step_rescan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_scan_range()

    async def async_step_scan_range(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
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

    async def async_step_scan_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        choices = []
        for slave in sorted(set(self._existing) | set(self._found)):
            existing = self._existing.get(slave)
            status = (
                "Missing"
                if slave not in self._found
                else "Existing"
                if existing
                else "New"
            )
            choices.append(
                {
                    "value": str(slave),
                    "label": f"Slave {slave:02d} {existing.model.value.upper() if existing else ''} — {status}",
                }
            )
        if user_input is not None:
            self._selected = sorted(int(value) for value in user_input["devices"])
            if self._selected or isinstance(self, CoseligOptionsFlow):
                return await self._start_profiles()
            errors["devices"] = "no_devices"
        return self.async_show_form(
            step_id="scan_result",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "devices", default=[str(slave) for slave in self._existing]
                    ): select(choices, multiple=True)
                }
            ),
        )

    async def _start_profiles(self) -> ConfigFlowResult:
        self._devices = []
        self._index = self._group_index = 0
        return await self.async_step_model()

    async def async_step_model(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self._index >= len(self._selected):
            return await self.async_step_groups()
        slave = self._selected[self._index]
        old = self._existing.get(slave)
        if user_input is not None:
            model = Model(user_input["model"])
            self._devices.append(
                old if old is not None and old.model == model else Device(slave, model)
            )
            self._index += 1
            return await self.async_step_model()
        return self.async_show_form(
            step_id="model",
            description_placeholders={"slave": f"{slave:02d}"},
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "model", default=old.model.value if old else "p404"
                    ): select(
                        [
                            {"value": model.value, "label": model.value.upper()}
                            for model in Model
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
            for index, device in enumerate(self._devices)
            if device.model == Model.P404
        ]
        if self._group_index >= len(indices):
            return self._finish()
        index = indices[self._group_index]
        device = self._devices[index]
        if user_input is not None:
            self._devices[index] = Device(
                device.slave,
                device.model,
                GroupMode(user_input["group_a"]),
                GroupMode(user_input["group_b"]),
            )
            self._group_index += 1
            return await self.async_step_groups()
        choices = [
            {"value": "single", "label": "2 × independent dimmers / 2 × 單路調光"},
            {"value": "cct", "label": "1 × CCT / 1 × 雙色溫"},
        ]
        return self.async_show_form(
            step_id="groups",
            description_placeholders={"slave": f"{device.slave:02d}"},
            data_schema=vol.Schema(
                {
                    vol.Required("group_a", default=device.group_a.value): select(
                        choices
                    ),
                    vol.Required("group_b", default=device.group_b.value): select(
                        choices
                    ),
                }
            ),
        )

    async def async_step_cancel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_abort(reason="user_cancelled")

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
                return await self.async_step_scan_range()
        return self.async_show_form(
            step_id="user", data_schema=gateway_schema(user_input or {}), errors=errors
        )

    def _finish(self) -> ConfigFlowResult:
        self._options["devices"] = [device.as_dict() for device in self._devices]
        self._async_abort_entries_match(self._gateway)
        return self.async_create_entry(
            title=f"Coselig {self._gateway['host']}",
            data=self._gateway,
            options=self._options,
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
        self._options.update(self.config_entry.options)
        self._existing = {
            device.slave: device
            for device in devices_from_options(self.config_entry.options)
        }
        return self.async_show_menu(
            step_id="init", menu_options=["polling", "manage_devices", "scan_range"]
        )

    async def async_step_polling(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            interval = float(user_input["poll_interval"])
            if not valid_poll_interval(interval):
                errors["poll_interval"] = "invalid_poll_interval"
            else:
                self._options["poll_interval"] = interval
                # Profile changes still use automatic reload. A timing change never does.
                self.automatic_reload = False
                if hasattr(self.config_entry, "runtime_data"):
                    self.config_entry.runtime_data.coordinator.set_poll_interval(
                        interval
                    )
                return self.async_create_entry(data=self._options)
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

    async def async_step_manage_devices(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._found = list(self._existing)
        return await self.async_step_scan_result(user_input)

    def _finish(self) -> ConfigFlowResult:
        self._options["devices"] = [device.as_dict() for device in self._devices]
        return self.async_create_entry(data=self._options)
