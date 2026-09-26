"""Deterministic Chinese lunar-calendar labels used in weather summaries."""

from __future__ import annotations

from datetime import date

from lunardate import LunarDate


_LUNAR_NAMES = {
    (1, 1): "春节", (1, 15): "元宵节", (5, 5): "端午节", (7, 7): "七夕",
    (7, 15): "中元节", (8, 15): "中秋节", (9, 9): "重阳节", (12, 8): "腊八节",
}
_SOLAR_NAMES = {(1, 1): "元旦", (5, 1): "劳动节", (10, 1): "国庆节", (12, 25): "圣诞节"}


def calendar_context(day: date) -> dict[str, str]:
    lunar = LunarDate.from_solar_date(day.year, day.month, day.day)
    names: list[str] = []
    if (day.month, day.day) in _SOLAR_NAMES:
        names.append(_SOLAR_NAMES[(day.month, day.day)])
    if (lunar.month, lunar.day) in _LUNAR_NAMES and not lunar.isLeapMonth:
        names.append(_LUNAR_NAMES[(lunar.month, lunar.day)])
    return {
        "lunar": f"农历{lunar.year}年{lunar.month}月{lunar.day}日",
        "festivals": f"节日：{'、'.join(names)}。" if names else "",
    }
