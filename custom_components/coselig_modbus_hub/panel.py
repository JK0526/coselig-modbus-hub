"""Register the Coselig Hub sidebar panel."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.frontend import async_register_built_in_panel
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

PANEL_URL = f"/{DOMAIN}/panel.js"


async def async_register_panel(hass: HomeAssistant) -> None:
    """Serve and register the panel once per Home Assistant process."""
    if DOMAIN in hass.data.get("frontend_panels", {}):
        return
    panel_path = Path(__file__).with_name("panel.js")
    await hass.http.async_register_static_paths(
        [StaticPathConfig(PANEL_URL, str(panel_path), cache_headers=False)]
    )
    async_register_built_in_panel(
        hass,
        component_name="custom",
        sidebar_title="Coselig Hub",
        sidebar_icon="mdi:lightbulb-group",
        frontend_url_path=DOMAIN,
        config={
            "_panel_custom": {
                "name": "coselig-modbus-hub-panel",
                "js_url": PANEL_URL,
                "embed_iframe": False,
                "trust_external": False,
            }
        },
        require_admin=True,
    )
