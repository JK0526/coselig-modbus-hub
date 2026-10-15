"""Bounded, restart-local observation of complete RTU packets."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


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
