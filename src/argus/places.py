"""Coordinate naming port and bounded, user-initiated lookup service."""

from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Callable, Protocol


class PlaceLookupError(RuntimeError):
    pass


class PlaceLookupLimited(PlaceLookupError):
    pass


@dataclass(frozen=True, slots=True)
class PlaceResolution:
    label: str | None
    timezone: str
    provider: str | None
    precision: str | None


class PlaceProvider(Protocol):
    def resolve_place(self, latitude: float, longitude: float) -> PlaceResolution: ...


class PlaceResolutionService:
    def __init__(self, provider: Callable[[], PlaceProvider | None],
                 timezone: Callable[[float, float], str],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.provider = provider
        self.timezone = timezone
        self.clock = clock
        self.cache: OrderedDict[tuple[float, float], tuple[float, PlaceResolution]] = OrderedDict()
        self.requests: deque[float] = deque()

    def resolve(self, latitude: float, longitude: float) -> PlaceResolution:
        if (not math.isfinite(latitude) or not math.isfinite(longitude)
                or not -90 <= latitude <= 90 or not -180 <= longitude <= 180):
            raise ValueError("invalid coordinates")
        now = self.clock()
        key = (round(latitude, 6), round(longitude, 6))
        cached = self.cache.get(key)
        if cached and now < cached[0]:
            self.cache.move_to_end(key)
            return cached[1]
        while self.requests and self.requests[0] <= now - 3600:
            self.requests.popleft()
        if len(self.requests) >= 60:
            raise PlaceLookupLimited("place lookup hourly limit reached")
        self.requests.append(now)
        result = None
        try:
            provider = self.provider()
            if provider is not None:
                result = provider.resolve_place(latitude, longitude)
        except PlaceLookupError:
            pass
        if result is None:
            result = PlaceResolution(None, self.timezone(latitude, longitude), None, None)
        self.cache[key] = (now + (86400 if result.label else 60), result)
        self.cache.move_to_end(key)
        while len(self.cache) > 128:
            self.cache.popitem(last=False)
        return result
