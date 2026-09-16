"""Canonical macro-region taxonomy and backward-compatible weighting helpers.

Stored observations may use the historical country/global codes (``CN``,
``JP`` and ``US``).  New sources should prefer macro regions so that source
coverage and regional policy do not depend on one country's naming convention.
"""

from __future__ import annotations

from typing import Mapping


LEGACY_REGIONS = frozenset({"CN", "JP", "US", "GLOBAL", "OTHER"})
MACRO_REGIONS = frozenset(
    {
        "EAST_ASIA",
        "NORTH_AMERICA",
        "EUROPE",
        "SOUTHEAST_ASIA",
        "MIDDLE_EAST",
        "SOUTH_AMERICA",
        "AFRICA",
        "AUSTRALIA_OCEANIA",
        "GLOBAL",
        "OTHER",
    }
)
SUPPORTED_REGIONS = LEGACY_REGIONS | MACRO_REGIONS

# These are deliberately soft preferences, not quotas.  East Asia reflects
# the user's highest-interest area; North America, Europe and Australia/Oceania
# share the next tier.  GLOBAL/OTHER retain a neutral middle fallback.
DEFAULT_MACRO_REGION_WEIGHTS: dict[str, int] = {
    "EAST_ASIA": 5,
    "NORTH_AMERICA": 4,
    "EUROPE": 4,
    "AUSTRALIA_OCEANIA": 4,
    "SOUTHEAST_ASIA": 3,
    "MIDDLE_EAST": 3,
    "GLOBAL": 3,
    "OTHER": 3,
    "SOUTH_AMERICA": 2,
    "AFRICA": 2,
}

_ALIASES = {
    "EASTASIA": "EAST_ASIA",
    "EAST_ASIA": "EAST_ASIA",
    "NORTHAMERICA": "NORTH_AMERICA",
    "NORTH_AMERICA": "NORTH_AMERICA",
    "SOUTHEASTASIA": "SOUTHEAST_ASIA",
    "SOUTH_EAST_ASIA": "SOUTHEAST_ASIA",
    "SOUTHEAST_ASIA": "SOUTHEAST_ASIA",
    "MIDDLEEAST": "MIDDLE_EAST",
    "MIDDLE_EAST": "MIDDLE_EAST",
    "SOUTHAMERICA": "SOUTH_AMERICA",
    "SOUTH_AMERICA": "SOUTH_AMERICA",
    "AUSTRALIA": "AUSTRALIA_OCEANIA",
    "OCEANIA": "AUSTRALIA_OCEANIA",
    "AUSTRALIA_OCEANIA": "AUSTRALIA_OCEANIA",
}

# Historical country codes remain valid storage values and map to a macro
# family only for policy scoring and coverage aggregation.
REGION_FAMILIES = {
    "CN": "EAST_ASIA",
    "JP": "EAST_ASIA",
    "US": "NORTH_AMERICA",
    "GLOBAL": "GLOBAL",
    "OTHER": "OTHER",
}


def normalize_region(value: str) -> str:
    """Return a supported, canonical region code or raise ``ValueError``."""

    if not isinstance(value, str):
        raise ValueError("region must be a string")
    key = value.strip().upper().replace("-", "_").replace(" ", "_")
    key = _ALIASES.get(key, key)
    if key not in SUPPORTED_REGIONS:
        raise ValueError(f"unsupported region: {value}")
    return key


def region_family(value: str) -> str:
    """Map legacy country codes to their macro region for policy decisions."""

    normalized = normalize_region(value)
    return REGION_FAMILIES.get(normalized, normalized)


def effective_region_weights(weights: Mapping[str, int] | None = None) -> dict[str, int]:
    """Merge configured overrides with the current macro defaults.

    Existing configurations containing only legacy keys remain effective.  A
    macro source gets the macro default unless its family or exact region has
    an explicit override; exact legacy keys win over family keys.
    """

    result = dict(DEFAULT_MACRO_REGION_WEIGHTS)
    if weights:
        for key, value in weights.items():
            result[normalize_region(str(key))] = int(value)
    return result


def region_weight(
    weights: Mapping[str, int] | None,
    region: str,
    *,
    default: int = 3,
) -> int:
    """Resolve a score weight with exact, family and default precedence."""

    configured = weights or {}
    try:
        normalized = normalize_region(region)
    except ValueError:
        fallback = configured.get("OTHER", default)
        return int(fallback)
    if normalized in configured:
        return int(configured[normalized])
    family = region_family(normalized)
    if family in configured:
        return int(configured[family])
    return int(effective_region_weights(configured).get(family, default))
