"""MQTT Discovery and topic construction.

This module is deliberately independent of Home Assistant's MQTT client.  It
returns payloads and topics, which keeps identity/migration rules testable
before a broker is touched.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping


def _part(value: object) -> str:
    """Return a topic-safe identifier."""
    result = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value).strip())
    if not result:
        raise ValueError("Empty topic identifier")
    return result


def entity_base(channel: Mapping[str, object], hub_id: str, *, topic_prefix: str = "coselig") -> str:
    """Return the base topic for a channel's state and commands."""
    slave = _part(channel["slave"])
    name = _part(channel["channel"])
    return f"{_part(topic_prefix)}/{_part(hub_id)}/light/{slave}/{name}"


def unique_id(channel: Mapping[str, object], hub_id: str) -> str:
    """Build a stable entity identity for one Hub channel."""
    return f"coselig_{_part(hub_id)}_{_part(channel['slave'])}_{_part(channel['channel'])}"


def discovery_topic(channel: Mapping[str, object], hub_id: str, *, discovery_prefix: str = "homeassistant") -> str:
    """Return the retained MQTT Discovery config topic."""
    return f"{_part(discovery_prefix)}/light/{unique_id(channel, hub_id)}/config"


def discovery_payload(
    channel: Mapping[str, object],
    hub_id: str,
    *,
    discovery_prefix: str = "homeassistant",
    topic_prefix: str = "coselig",
) -> dict[str, object]:
    """Create one MQTT Light default-schema Discovery payload."""
    base = entity_base(channel, hub_id, topic_prefix=topic_prefix)
    payload: dict[str, object] = {
        "name": str(channel["name"]),
        "unique_id": unique_id(channel, hub_id),
        "object_id": unique_id(channel, hub_id),
        "state_topic": f"{base}/state",
        "command_topic": f"{base}/set",
        "payload_on": "ON",
        "payload_off": "OFF",
        "optimistic": False,
        "brightness_scale": 100,
        "brightness_state_topic": f"{base}/brightness",
        "brightness_command_topic": f"{base}/set/brightness",
        "availability_topic": f"{_part(topic_prefix)}/{_part(hub_id)}/availability",
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": {
            "identifiers": [f"coselig_{_part(hub_id)}_{_part(channel['slave'])}"],
            "name": f"Coselig {channel['model']} {channel['slave']}",
            "manufacturer": "Coselig",
            "model": str(channel["model"]),
        },
    }
    if channel["kind"] == "dual":
        payload.update(
            {
                "color_temp_state_topic": f"{base}/colortemp",
                "color_temp_command_topic": f"{base}/set/colortemp",
                "min_mireds": int(channel.get("mired_min", 175)),
                "max_mireds": int(channel.get("mired_max", 455)),
            }
        )
    return payload


def discovery_message(channel: Mapping[str, object], hub_id: str, **kwargs: object) -> tuple[str, str]:
    """Return a retained Discovery topic/payload pair."""
    return discovery_topic(channel, hub_id, **kwargs), json.dumps(
        discovery_payload(channel, hub_id, **kwargs), ensure_ascii=False, separators=(",", ":")
    )


def clear_message(channel: Mapping[str, object], hub_id: str, **kwargs: object) -> tuple[str, str]:
    """Return a retained empty payload that removes an entity's Discovery."""
    return discovery_topic(channel, hub_id, **kwargs), ""


def availability_message(hub_id: str, online: bool, *, topic_prefix: str = "coselig") -> tuple[str, str]:
    """Return the retained Hub availability message."""
    return f"{_part(topic_prefix)}/{_part(hub_id)}/availability", "online" if online else "offline"
