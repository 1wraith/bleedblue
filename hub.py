"""
Event hub: a single-process, in-memory publish/subscribe bus.

Efficiency module #1 — bounded, drop-oldest fan-out.
Every WebSocket client gets its own fixed-size queue. When a client can't keep
up (e.g. a phone browser rendering a spectrum waterfall), we discard the OLDEST
event rather than letting the queue grow without bound. For realtime RF data a
stale sweep is worthless, so dropping it is the correct trade-off and it keeps
memory flat no matter how many or how slow the clients are.
"""
from __future__ import annotations

import asyncio
import time
from typing import Dict, Optional


class Subscriber:
    __slots__ = ("queue",)

    def __init__(self, maxsize: int = 256) -> None:
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)

    def push(self, event: Dict) -> None:
        q = self.queue
        if q.full():                       # drop oldest — realtime data ages out fast
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass

    def __aiter__(self) -> "Subscriber":
        return self

    async def __anext__(self) -> Dict:
        return await self.queue.get()


class Hub:
    """Fan-out bus. Producers call publish(); each subscriber drains its own queue.

    The hub also holds the latest GPS fix. Since every event passes through
    publish(), stamping position here geotags the whole system at emit time —
    the cheapest possible path to spatial data, and the seam the future
    correlation/storage layer will tap.
    """

    def __init__(self) -> None:
        self._subs: set[Subscriber] = set()
        self.last_fix: Optional[Dict] = None   # {"lat":..., "lon":...}

    def subscribe(self, maxsize: int = 256) -> Subscriber:
        sub = Subscriber(maxsize)
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subs.discard(sub)

    def set_fix(self, lat: float, lon: float) -> None:
        self.last_fix = {"lat": lat, "lon": lon}

    def publish(self, event: Dict) -> None:
        event.setdefault("ts", round(time.time(), 3))
        # geotag data events (not gps itself, not control/log) when a fix exists
        if self.last_fix and event.get("type") not in ("gps", "state", "snapshot"):
            event.setdefault("lat", self.last_fix["lat"])
            event.setdefault("lon", self.last_fix["lon"])
        for sub in self._subs:
            sub.push(event)

    @property
    def n_subscribers(self) -> int:
        return len(self._subs)