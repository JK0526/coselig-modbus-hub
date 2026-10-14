from unittest.mock import AsyncMock, patch

from homeassistant.data_entry_flow import FlowResultType

from custom_components.coselig_modbus_hub.const import DOMAIN
from custom_components.coselig_modbus_hub.transport import GatewayError


async def test_reconfigure_preserves_identity(hass, entry):
    old_entry_id = entry.entry_id
    old_options = dict(entry.options)
    with (
        patch(
            "custom_components.coselig_modbus_hub.config_flow.validate_gateway",
            new=AsyncMock(
                side_effect=[
                    GatewayError(),
                    ValueError(),
                    {"host": "new.local", "port": 503},
                ]
            ),
        ),
        patch.object(hass.config_entries, "async_schedule_reload") as reload,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
        )
        assert result["step_id"] == "reconfigure"
        flow_id = result["flow_id"]
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"host": "new.local", "port": 503}
        )
        assert result["errors"] == {"base": "cannot_connect"}
        assert entry.data["host"] == "gateway.local"
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"host": "bad host", "port": 503}
        )
        assert result["errors"] == {"host": "invalid_host"}
        assert entry.data["host"] == "gateway.local"
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"host": "new.local", "port": 503}
        )
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "reconfigure_successful"
        reload.assert_called_once_with(old_entry_id)
    assert entry.entry_id == old_entry_id
    assert entry.data == {"host": "new.local", "port": 503}
    assert dict(entry.options) == old_options
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
