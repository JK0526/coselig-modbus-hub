"""Config flow for Sunwave Modbus Hub."""

from __future__ import annotations

import ipaddress
import re

import voluptuous as vol
from homeassistant import config_entries

from .const import (
    CONF_HOST,
    CONF_CHANNELS,
    CONF_POLL_INTERVAL,
    CONF_POLLING_ENABLED,
    CONF_PORT,
    CONF_TIMEOUT,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    DOMAIN,
)


def _valid_host(value: str) -> str:
    """Accept an IP address or a resolvable hostname-shaped value."""
    value = str(value).strip()
    if not value or any(char.isspace() for char in value):
        raise vol.Invalid("Host is required")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        # DNS validation is intentionally syntactic; actual reachability is
        # checked by the transport once a device is configured.
        hostname = value[:-1] if value.endswith(".") else value
        labels = hostname.split(".")
        if (
            len(hostname) > 253
            or not all(
                1 <= len(label) <= 63
                and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
                for label in labels
            )
        ):
            raise vol.Invalid("Invalid host")
    return value


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle setup of a Sunwave TCP gateway."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Create one gateway entry from the UI."""
        errors = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"Sunwave {host}",
                data={CONF_HOST: host, CONF_PORT: port, CONF_TIMEOUT: user_input[CONF_TIMEOUT]},
                options={
                    CONF_POLLING_ENABLED: user_input[CONF_POLLING_ENABLED],
                    CONF_POLL_INTERVAL: user_input[CONF_POLL_INTERVAL],
                    CONF_CHANNELS: [],
                },
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): _valid_host,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=65535)
                ),
                vol.Required(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): vol.All(
                    vol.Coerce(float), vol.Range(min=0.05, max=30.0)
                ),
                vol.Required(CONF_POLLING_ENABLED, default=True): bool,
                vol.Required(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): vol.All(
                    vol.Coerce(float), vol.Range(min=0.5, max=3600.0)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    def async_get_options_flow(config_entry):
        """Expose polling settings without changing connection identity."""
        return SunwaveOptionsFlow()


class SunwaveOptionsFlow(config_entries.OptionsFlow):
    """Edit runtime options for an existing gateway."""

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            options = dict(self.config_entry.options)
            options.update(user_input)
            return self.async_create_entry(data=options)
        schema = vol.Schema(
            {
                vol.Required(CONF_POLLING_ENABLED, default=True): bool,
                vol.Required(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): vol.All(
                    vol.Coerce(float), vol.Range(min=0.5, max=3600.0)
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                schema, self.config_entry.options
            ),
        )
