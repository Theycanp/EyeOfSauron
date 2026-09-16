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
    ("nvidia_newsroom", "news"),
    ("european_commission", "press"),
    ("australian_prime_minister", "media"),
    ("uk_government", "government"),
    ("bank_of_england", "news"),
    ("asean_secretariat", "news"),
    ("channel_news_asia", "news"),
    ("who_africa", "news"),
    ("un_news_africa", "africa"),
    ("who_paho", "news"),
    ("un_news_middle_east", "middle_east"),
    ("iaea_news", "news"),
    ("nist_news", "news"),
    ("new_york_times", "world"),
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
        official_rule = next((rule for rule in rules if rule.get("id") == rule_id), None)
        if official_rule is None:
            official_rule = {
            "id": rule_id, "kind": "weighted_text",
            "source_ids": [],
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
            }
            rules.append(official_rule)
        covered = official_rule.setdefault("source_ids", [])
        for source in additions:
            # Secondary and aggregator feeds provide context and corroboration;
            # only reviewed primary sources may enter the first-party critical
            # notification rule.
            if source["source_tier"] != "primary":
                continue
            if source["id"] not in covered:
                covered.append(source["id"])
        embassy_source_id = "us_embassy_china_alerts"
        if embassy_source_id in {source["id"] for source in additions}:
            embassy_rule = next(
                (rule for rule in rules if rule.get("id") == "us_embassy_security_alerts"), None
            )
            if embassy_rule is None:
                rules.append({
                    "id": "us_embassy_security_alerts", "kind": "weighted_text",
                    "source_ids": [embassy_source_id], "threshold": 4.0,
                    "max_item_age_seconds": 172800,
                    "notification_title": "美国驻华使馆安全提醒", "priority": 5,
                    "tags": ["security", "embassy", "rotating_light"],
                    "patterns": [
                        {"label": "灾害与紧急安全提醒",
                         "regex": r"(?i)alert|security|emergency|evacuat|natural disaster|typhoon|flood|earthquake|爆炸|地震|台风|洪水|紧急|安全提醒",
                         "title_weight": 6.0, "summary_weight": 3.0},
                        {"label": "普通信息降权",
                         "regex": r"(?i)reminder|routine|general information|例行|常规",
                         "title_weight": -2.0, "summary_weight": -1.0},
                    ],
                })
            elif embassy_source_id not in embassy_rule.setdefault("source_ids", []):
                embassy_rule["source_ids"].append(embassy_source_id)
    return result, additions
