from __future__ import annotations

import unittest
from unittest.mock import Mock

from argus.places import PlaceLookupError, PlaceLookupLimited, PlaceResolution, PlaceResolutionService


class PlaceResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 100.0
        self.result = PlaceResolution("北京市 · 昌平", "Asia/Shanghai", "QWeather", "administrative")
        self.provider = Mock()
        self.provider.resolve_place.return_value = self.result
        self.timezone = Mock(return_value="Asia/Shanghai")
        self.service = PlaceResolutionService(lambda: self.provider, self.timezone, lambda: self.now)

    def test_caches_success_and_expires_after_a_day(self) -> None:
        for _ in range(3):
            self.assertEqual(self.result, self.service.resolve(40.1563, 116.2836))
        self.provider.resolve_place.assert_called_once_with(40.1563, 116.2836)
        self.timezone.assert_not_called()
        self.now += 86400
        self.service.resolve(40.1563, 116.2836)
        self.assertEqual(2, self.provider.resolve_place.call_count)

    def test_falls_back_to_timezone_and_retries_missing_name_after_a_minute(self) -> None:
        self.provider.resolve_place.side_effect = PlaceLookupError("unavailable")
        result = self.service.resolve(40, 116)
        self.assertIsNone(result.label)
        self.assertEqual("Asia/Shanghai", result.timezone)
        self.service.resolve(40, 116)
        self.provider.resolve_place.assert_called_once()
        self.now += 60
        self.provider.resolve_place.side_effect = None
        self.assertEqual(self.result, self.service.resolve(40, 116))

    def test_unconfigured_provider_still_resolves_timezone(self) -> None:
        service = PlaceResolutionService(lambda: None, self.timezone)
        self.assertIsNone(service.resolve(40, 116).label)
        self.timezone.assert_called_once_with(40, 116)

    def test_limits_uncached_queries_but_keeps_cached_results_available(self) -> None:
        for index in range(60):
            self.service.resolve(index, 116)
        with self.assertRaises(PlaceLookupLimited):
            self.service.resolve(61, 116)
        self.assertEqual(self.result, self.service.resolve(0, 116))
        self.now += 3600
        self.service.resolve(61, 116)
        self.assertLessEqual(len(self.service.requests), 60)

    def test_validates_coordinates_before_network_calls(self) -> None:
        for lat, lon in ((float("nan"), 116), (40, float("inf")), (91, 116), (40, -181)):
            with self.subTest(lat=lat, lon=lon), self.assertRaises(ValueError):
                self.service.resolve(lat, lon)
        self.provider.resolve_place.assert_not_called()
        self.timezone.assert_not_called()

    def test_bounds_the_process_cache(self) -> None:
        for index in range(150):
            self.now += 61
            self.service.resolve(40 + index / 1000, 116)
        self.assertEqual(128, len(self.service.cache))
