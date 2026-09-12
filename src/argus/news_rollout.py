"""Reviewed source selection, separate from the reusable catalog and live configuration."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from .news_catalog import NEWS_SOURCE_CATALOG


OFFICIAL_NEWS_SELECTION = (
    ("china_mof", "announcements"),
    ("china_ndrc", "announcements"),
    ("china_stats", "announcements"),
    ("china_mfa", "announcements"),
    ("china_most", "announcements"),
    ("bank_of_japan", "whats_new"),
    ("japan_cabinet", "announcements"),
    ("japan_mhlw", "news"),
    ("japan_meteorological_agency", "high_frequency"),
    ("jaxa", "press"),
    ("sec", "press_releases"),
    ("nasa", "news_releases"),
    ("ecb", "press_releases"),
    ("usgs_earthquakes", "significant_month"),
    ("un_news", "chinese"),
    ("economist", "science_technology"),
)


def plan_official_news(
    current: Mapping[str, Any], base_sources: tuple[Any, ...] = (),
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Add missing sources atomically; preserve existing choices and all other policy.

    Catalog inclusion does not activate a source. This explicit operations plan
    is applied only when an operator requests the reviewed regional rollout.
    """
    result = deepcopy(dict(current))
    sources = result.setdefault("sources", [])
    rules = result.setdefault("rules", [])
    ids = {source["id"] for source in sources} | {source.id for source in base_sources}
    urls = {source.get("url") for source in sources} | {source.url for source in base_sources}
    additions = []
    for entry_id, feed_id in OFFICIAL_NEWS_SELECTION:
        source_id = f"{entry_id}_{feed_id}"
        source = NEWS_SOURCE_CATALOG.source_template(
            entry_id, feed_id, source_id, enabled=True, user_confirmed=True,
            poll_interval_seconds=300 if entry_id in {"japan_meteorological_agency", "usgs_earthquakes"} else 900,
        )
        if source_id in ids or source["url"] in urls:
            continue
        if source["source_tier"] == "primary":
            source["settings"]["max_content_age_seconds"] = 90 * 86400
        sources.append(source)
        additions.append(source)
        ids.add(source_id)
        urls.add(source["url"])
    if additions:
        rule_id = "official_news_critical"
        # A shared operator-edited rule must not silently be replaced.
        if any(rule["id"] == rule_id for rule in rules):
            raise ValueError("official_news_critical already exists; review additions and rule coverage manually")
        rules.append({
            "id": rule_id, "kind": "weighted_text",
            "source_ids": [source["id"] for source in additions],
            "threshold": 8.0, "max_item_age_seconds": 172800,
            "notification_title": "重大一手信息", "priority": 4,
            "tags": ["rotating_light"],
            "patterns": [
                {"label": "重大灾害或紧急状态",
                 "regex": r"(?i)大津波警報|特別警報|特别重大|一级应急响应|一級應急|state of emergency|public health emergency|M [7-9]\.[0-9]",
                 "title_weight": 8.0, "summary_weight": 4.0},
                {"label": "重大安全事态",
                 "regex": r"(?i)宣战|宣戦|宣布断交|declar.{0,20}war|nuclear emergency|核事故|全面停火|ceasefire agreement",
                 "title_weight": 8.0, "summary_weight": 4.0},
                {"label": "货币政策重大调整",
                 "regex": r"(?i)降息|加息|降准|利上げ|利下げ|policy rate.{0,35}(?:increase|decrease|lower|raise)|(?:cuts|raises).{0,25}interest rate",
                 "title_weight": 8.0, "summary_weight": 4.0},
                {"label": "演习、预告和例行回顾降权",
                 "regex": r"(?i)演练|演習|訓練|drill|exercise|anniversary|回顾|纪念|議事録|minutes of",
                 "title_weight": -12.0, "summary_weight": -4.0},
            ],
        })
    return result, additions
