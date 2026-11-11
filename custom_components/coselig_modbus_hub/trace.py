"""Bounded, restart-local observation of complete RTU packets."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from .protocol import crc16


@dataclass(frozen=True)
class TraceEntry:
    timestamp: str
    direction: str
    raw_hex: str
    source: str
    latency_ms: float | None


class ProtocolTraceBuffer:
    """No persistence, listeners, I/O, or effects on the bus scheduler."""

    def __init__(self, capacity: int = 200) -> None:
        if not 1 <= capacity <= 500:
            raise ValueError("Trace capacity must be 1..500")
        self._entries: deque[TraceEntry] = deque(maxlen=capacity)
        self._last_timestamp: datetime | None = None

    def record(
        self,
        direction: str,
        raw: bytes = b"",
        *,
        source: str,
        latency_ms: float | None = None,
    ) -> None:
        timestamp = datetime.now(UTC)
        if self._last_timestamp is not None and timestamp <= self._last_timestamp:
            # The frontend uses timestamp as a stable trace key. Preserve two
            # identical rapid input events even if they arrive in one millisecond.
            timestamp = self._last_timestamp + timedelta(milliseconds=1)
        self._last_timestamp = timestamp
        self._entries.append(
            TraceEntry(
                timestamp.isoformat(timespec="milliseconds"),
                direction,
                raw.hex(" ").upper(),
                source,
                None if latency_ms is None else round(latency_ms, 3),
            )
        )

    def snapshot(self) -> list[dict[str, Any]]:
        return [asdict(entry) for entry in self._entries]

    def clear(self) -> None:
        self._entries.clear()
        self._last_timestamp = None


class PassiveRxBuffer:
    """Bounded observations of idle RX, sampled at most 20 times per second."""

    def __init__(self) -> None:
        self.enabled = False
        self.owner: int | None = None
        self._entries: deque[dict[str, Any]] = deque(maxlen=200)
        self._next_record = 0.0

    def record(self, packet: bytes) -> None:
        now = monotonic()
        if now < self._next_record:
            return
        self._next_record = now + 0.05
        self._entries.append(
            {
                "direction": "UNMATCHED_RX",
                "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "raw_hex": packet.hex(" ").upper(),
                "slave": packet[0],
                "function": packet[1],
                "crc_valid": crc16(packet[:-2])
                == int.from_bytes(packet[-2:], "little"),
            }
        )

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self._entries)

    def clear(self) -> None:
        self.enabled = False
        self.owner = None
        self.clear_entries()

    def clear_entries(self) -> None:
        self._entries.clear()
        self._next_record = 0.0
