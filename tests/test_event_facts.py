from __future__ import annotations

import unittest
from datetime import UTC, datetime

from argus.event_facts import (
    FACT_EXTRACTOR_VERSION,
    FACT_PRODUCER,
    EventFactCandidate,
    FactExtractionInput,
    extract_event_facts,
)


PUBLISHED_AT = int(datetime(2026, 9, 26, 8, 0, tzinfo=UTC).timestamp())


def report(title: str, summary: str = "", **updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "source_id": "news",
        "external_id": "report-1",
        "title": title,
        "summary": summary,
        "published_at": PUBLISHED_AT,
        "attributes": {},
    }
    value.update(updates)
    return value


class EventFactTests(unittest.TestCase):
    def test_candidate_keys_separate_slot_from_value(self) -> None:
        first = EventFactCandidate(
            "central_bank_decision", "federal_reserve", "policy_rate_target", "decision",
            "5.25", "percent", "asserted", PUBLISHED_AT, "first evidence",
        )
        second = EventFactCandidate(
            "central_bank_decision", "federal_reserve", "policy_rate_target", "decision",
            "5.5", "percent", "asserted", PUBLISHED_AT + 60, "second evidence",
        )
        self.assertEqual(first.slot_key, second.slot_key)
        self.assertNotEqual(first.claim_key, second.claim_key)
        self.assertEqual(FACT_PRODUCER, first.producer)
        self.assertEqual(FACT_EXTRACTOR_VERSION, first.version)
        self.assertLessEqual(len(first.slot_key), 160)
        self.assertLessEqual(len(first.claim_key), 160)

    def test_candidate_keys_use_the_candidate_version(self) -> None:
        first = EventFactCandidate(
            "central_bank_decision", "federal_reserve", "policy_rate_target", "decision",
            "5.25", "percent", "asserted", None, "evidence", version=1,
        )
        second = EventFactCandidate(
            "central_bank_decision", "federal_reserve", "policy_rate_target", "decision",
            "5.25", "percent", "asserted", None, "evidence", version=2,
        )
        self.assertTrue(first.slot_key.startswith("slot:v1:"))
        self.assertTrue(first.claim_key.startswith("claim:v1:"))
        self.assertTrue(second.slot_key.startswith("slot:v2:"))
        self.assertTrue(second.claim_key.startswith("claim:v2:"))
        self.assertNotEqual(first.slot_key, second.slot_key)
        self.assertNotEqual(first.claim_key, second.claim_key)

    def test_same_fact_has_same_keys_across_languages(self) -> None:
        english = extract_event_facts(
            report("Federal Reserve raised interest rates by 25 bps to 5.25%")
        )
        chinese = extract_event_facts(report("美联储加息25个基点至5.25%"))
        self.assertEqual(
            {(item.slot_key, item.claim_key) for item in english},
            {(item.slot_key, item.claim_key) for item in chinese},
        )
        self.assertEqual(
            {("policy_rate_change", "25", "basis_point"),
             ("policy_rate_target", "5.25", "percent")},
            {(item.predicate, item.value, item.unit) for item in english},
        )

    def test_japanese_central_bank_decision(self) -> None:
        facts = extract_event_facts(report("日本銀行は政策金利を25ベーシスポイント引き上げ、0.5%とした"))
        self.assertEqual(2, len(facts))
        self.assertEqual({"bank_of_japan"}, {item.subject for item in facts})
        self.assertEqual({"25", "0.5"}, {item.value for item in facts})

    def test_rate_cut_normalizes_basis_point_sign_and_range(self) -> None:
        facts = extract_event_facts(report(
            "ECB cut its policy rate by 50 basis points to 3.25%-3.50%"
        ))
        self.assertEqual(
            {("policy_rate_change", "-50", "basis_point"),
             ("policy_rate_target", "3.25..3.5", "percent_range")},
            {(item.predicate, item.value, item.unit) for item in facts},
        )

    def test_old_rate_to_new_rate_is_not_a_target_range(self) -> None:
        english = extract_event_facts(report("Fed raised interest rates from 5% to 5.25%"))
        japanese = extract_event_facts(report("日本銀行は政策金利を0.25%から0.5%に引き上げた"))
        self.assertEqual([("5.25", "percent")], [(item.value, item.unit) for item in english])
        self.assertEqual([("0.5", "percent")], [(item.value, item.unit) for item in japanese])

    def test_rate_hold_requires_an_explicit_target(self) -> None:
        self.assertEqual(
            (), extract_event_facts(report("Bank of Japan held policy rates unchanged"))
        )
        facts = extract_event_facts(report("Bank of Japan held its policy rate at 0.5%"))
        self.assertEqual(
            [("policy_rate_target", "0.5")],
            [(item.predicate, item.value) for item in facts],
        )

    def test_unnamed_central_bank_is_ambiguous(self) -> None:
        self.assertEqual((), extract_event_facts(report("Central bank raised rates by 25 bps")))

    def test_source_identity_and_region_never_infer_an_unnamed_institution(self) -> None:
        examples = (
            report(
                "Central bank raised rates by 25 bps",
                source_id="fed_monetary",
                region="US",
            ),
            report(
                "央行加息25个基点",
                source_id="china_central_bank",
                region="CN",
            ),
            report(
                "Reserve Bank of Australia raised rates by 25 bps",
                source_id="australia_official",
                region="AU",
            ),
        )
        for item in examples:
            with self.subTest(item=item):
                self.assertEqual((), extract_event_facts(item))

    def test_people_bank_apostrophes_are_not_mistaken_for_quotations(self) -> None:
        for name in ("People's Bank of China", "People’s Bank of China"):
            with self.subTest(name=name):
                facts = extract_event_facts(report(
                    f"{name} cut policy rates by 25 bps to 1.5%"
                ))
                self.assertEqual(2, len(facts))
                self.assertEqual({"peoples_bank_of_china"}, {item.subject for item in facts})

    def test_non_policy_rate_is_not_a_central_bank_fact(self) -> None:
        self.assertEqual((), extract_event_facts(report("Fed raised tax rates by 25 bps")))

    def test_multiple_institutions_are_ambiguous(self) -> None:
        self.assertEqual((), extract_event_facts(report(
            "Fed and ECB raised interest rates by 25 bps"
        )))

    def test_ambiguous_multiple_values_return_no_candidate_for_that_predicate(self) -> None:
        facts = extract_event_facts(report(
            "Fed raised interest rates by 25 bps, not the prior 50 bps, to 5.25%"
        ))
        self.assertEqual((), facts)

    def test_predictions_plans_and_minutes_are_not_facts(self) -> None:
        examples = (
            "Fed may raise interest rates by 25 bps to 5.25%",
            "美联储预计将加息25个基点至5.25%",
            "日本銀行は政策金利を0.5%に引き上げる見通し",
            "FOMC minutes discussed raising interest rates by 25 bps",
            "美联储会议纪要提到加息25个基点",
            "日本銀行の議事要旨は25ベーシスポイントの利上げを示した",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual((), extract_event_facts(report(text)))

    def test_denials_and_quotations_are_not_facts(self) -> None:
        examples = (
            "Fed denied it raised interest rates by 25 bps",
            "Official said Fed raised interest rates by 25 bps",
            "美联储否认加息25个基点",
            "官员表示美联储加息25个基点",
            'Analyst: "Fed raised interest rates by 25 bps"',
            "Analyst: 'Fed raised interest rates by 25 bps'",
            "Analyst: ‘Fed raised interest rates by 25 bps’",
            "「日本銀行は政策金利を0.5%に引き上げた」と報道",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual((), extract_event_facts(report(text)))

    def test_bare_numbers_do_not_become_facts(self) -> None:
        self.assertEqual((), extract_event_facts(report("Fed decision: 25 and 5.25")))
        self.assertEqual((), extract_event_facts(report("Earthquake update 120 2026")))

    def test_usgs_uses_only_the_real_source_contract(self) -> None:
        facts = extract_event_facts(report(
            "M 6.6 - 80 km ENE of Tadine, New Caledonia",
            "PAGER GREEN Time 2026-09-25 21:23:03 UTC Location 21.298 S 168.610 E Depth 10 km",
            source_id="usgs_earthquakes_significant_month",
            external_id="urn:earthquake-usgs-gov:us:6000txpi",
            attributes={"section": "Disaster", "published_at_inferred": False},
        ))
        self.assertEqual(1, len(facts))
        fact = facts[0]
        self.assertEqual(("earthquake_measurement", "magnitude", "6.6", "magnitude"),
                         (fact.kind, fact.predicate, fact.value, fact.unit))
        self.assertEqual("usgs:6000txpi", fact.scope)
        self.assertEqual(
            int(datetime(2026, 9, 25, 21, 23, 3, tzinfo=UTC).timestamp()),
            fact.effective_at,
        )

    def test_usgs_shape_is_not_trusted_from_another_source(self) -> None:
        self.assertEqual((), extract_event_facts(report(
            "M 7.8 - Major earthquake",
            "Time 2026-09-25 21:23:03 UTC",
            external_id="urn:earthquake-usgs-gov:us:fake",
        )))

    def test_usgs_requires_event_id_location_and_occurrence_time(self) -> None:
        base = report(
            "M 6.6 - Tadine, New Caledonia",
            "Time 2026-09-25 21:23:03 UTC",
            source_id="usgs_earthquakes_significant_month",
            external_id="urn:earthquake-usgs-gov:us:6000txpi",
        )
        for update in (
            {"external_id": "6000txpi"},
            {"title": "M 6.6"},
            {"summary": "Location only"},
        ):
            with self.subTest(update=update):
                self.assertEqual((), extract_event_facts({**base, **update}))

    def test_usgs_rejects_impossible_or_ambiguous_magnitude(self) -> None:
        for title in ("M 10.1 - Somewhere", "M 6.6 / 6.7 - Somewhere"):
            with self.subTest(title=title):
                self.assertEqual((), extract_event_facts(report(
                    title,
                    "Time 2026-09-25 21:23:03 UTC",
                    source_id="usgs_earthquakes_significant_month",
                    external_id="urn:earthquake-usgs-gov:us:6000txpi",
                )))

    def test_explicit_casualty_totals_with_subject_and_unit(self) -> None:
        examples = (
            ("Coastal earthquake death toll rose to 12 people", "coastal earthquake"),
            ("沿海地震死亡人数升至12人", "沿海地震"),
            ("沿岸地震の死者は12人", "沿岸地震"),
        )
        for text, subject in examples:
            with self.subTest(text=text):
                facts = extract_event_facts(report(text))
                self.assertEqual(1, len(facts))
                self.assertEqual(
                    ("casualty_count", subject, "death_toll", "total", "12", "person"),
                    (facts[0].kind, facts[0].subject, facts[0].predicate,
                     facts[0].scope, facts[0].value, facts[0].unit),
                )

    def test_casualty_count_accepts_explicit_killed_constructions(self) -> None:
        examples = (
            "12 people were killed in Coastal earthquake",
            "沿海地震造成12人死亡",
            "沿岸地震で12人が死亡",
        )
        for text in examples:
            with self.subTest(text=text):
                facts = extract_event_facts(report(text))
                self.assertEqual(["12"], [item.value for item in facts])

    def test_casualty_count_rejects_missing_subject_or_unit(self) -> None:
        examples = (
            "Death toll rose to 12 people",
            "Coastal earthquake total rose to 12",
            "12 after the Coastal earthquake",
            "死亡人数升至12人",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual((), extract_event_facts(report(text)))

    def test_casualty_predictions_quotes_and_history_are_not_facts(self) -> None:
        examples = (
            "Coastal earthquake death toll may rise to 12 people",
            'Official said "Coastal earthquake death toll rose to 12 people"',
            "2011 Coastal earthquake killed 12 people",
            "沿海地震死亡人数预计升至12人",
            "沿岸地震の死者は12人になる見通し",
            "At least 12 people were killed in Coastal earthquake",
            "Coastal earthquake death toll rose to about 12 people",
            "沿海地震造成至少12人死亡",
        )
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual((), extract_event_facts(report(text)))

    def test_duplicate_title_and_summary_fact_is_returned_once(self) -> None:
        text = "Fed raised interest rates by 25 bps to 5.25%"
        facts = extract_event_facts(report(text, text))
        self.assertEqual(2, len(facts))
        self.assertEqual(2, len({item.claim_key for item in facts}))

    def test_output_order_is_stable_when_fact_order_changes(self) -> None:
        first = extract_event_facts(report(
            "Fed raised interest rates by 25 bps to 5.25%",
            "Coastal earthquake death toll rose to 12 people",
        ))
        second = extract_event_facts(report(
            "Coastal earthquake death toll rose to 12 people",
            "Fed raised interest rates by 25 bps to 5.25%",
        ))
        self.assertEqual(
            [(item.slot_key, item.claim_key) for item in first],
            [(item.slot_key, item.claim_key) for item in second],
        )

    def test_mapping_accepts_aware_datetime_and_explicit_effective_time(self) -> None:
        effective = datetime(2026, 10, 1, tzinfo=UTC)
        facts = extract_event_facts({
            **report("Fed raised interest rates by 25 bps"),
            "published_at": datetime(2026, 9, 26, tzinfo=UTC),
            "effective_at": effective,
        })
        self.assertEqual([int(effective.timestamp())], [item.effective_at for item in facts])

    def test_unknown_effective_time_does_not_fall_back_to_publication_time(self) -> None:
        central_bank = extract_event_facts(report("Fed raised interest rates by 25 bps"))
        casualty = extract_event_facts(report(
            "Coastal earthquake death toll rose to 12 people"
        ))
        self.assertEqual([None], [item.effective_at for item in central_bank])
        self.assertEqual([None], [item.effective_at for item in casualty])

    def test_mapping_missing_or_invalid_timestamp_fails_closed(self) -> None:
        missing = report("Fed raised interest rates by 25 bps")
        missing.pop("published_at")
        self.assertEqual((), extract_event_facts(missing))
        self.assertEqual((), extract_event_facts({**missing, "published_at": "invalid"}))

    def test_invalid_timestamp_and_incomplete_candidate_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "timestamp"):
            FactExtractionInput("source", "id", "title", "", -1)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            EventFactCandidate(
                "", "subject", "predicate", "scope", "value", "unit", "asserted",
                PUBLISHED_AT, "evidence",
            )
        with self.assertRaisesRegex(ValueError, "incomplete"):
            EventFactCandidate(
                "kind", "subject", "predicate", "scope", "value", "unit", "asserted",
                -1, "evidence",
            )


if __name__ == "__main__":
    unittest.main()
