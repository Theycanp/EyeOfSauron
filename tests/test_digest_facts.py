from __future__ import annotations

import unittest

from argus.digest import DigestCluster, DigestEventFacts, freeze_digest_event_facts
from argus.digest_analysis import ApiDigestSummarizer
from argus.events import (
    EventEvidenceGraph, PersistedEvent, PersistedEventClaim,
    PersistedEventClaimEvidence, PersistedEventReport,
)


class DigestFactTests(unittest.TestCase):
    def report(self, report_id=1):
        return PersistedEventReport(
            event_key="event", observation_id=report_id, source_id="official", publisher="official",
            source_tier="primary", relation="primary", match_score=1, is_representative=True,
            published_at=100, title="Official claim", summary="Saved proof", url="https://example.test/report",
            report_id=report_id,
        )

    def claim(self, key="claim", *, status="active", parent=None):
        return PersistedEventClaim(
            event_key="event", claim_key=key, text="Reviewed fact " + key, status=status,
            confidence=0.9, first_seen_at=100, last_seen_at=100, supersedes_claim_key=parent,
        )

    def cluster(self, facts):
        return DigestCluster(
            cluster_key="event", title="Event", summary="Summary " * 10, score=4, importance=4,
            urgency=3, relevance=4, confidence=0.9, published_at=100, regions=("US",),
            topics=("economy",), source_ids=("official",), observation_ids=(1,),
            links=("https://example.test/report",), event_id="event", reports=(self.report(),), facts=facts,
        )

    def test_selected_facts_keep_supersedes_ancestry_without_unrelated_evidence(self):
        old = self.claim("old", status="superseded")
        new = self.claim("new", parent="old")
        other = self.claim("unselected")
        graph = EventEvidenceGraph(
            event=PersistedEvent("event", "fp", "Event", "summary", 4, 4, 3, 4, 0.9, 100, 100),
            reports=(self.report(), self.report(2)), claims=(old, new, other),
            evidence=(PersistedEventClaimEvidence("new", 1, "supports"),
                      PersistedEventClaimEvidence("unselected", 2, "supports")),
            timeline=(), notifications=(), truncated={},
        )
        facts = freeze_digest_event_facts(graph, (self.report(),), as_of=200)
        self.assertEqual((old, new), facts.claims)
        self.assertEqual((PersistedEventClaimEvidence("new", 1, "supports"),), facts.evidence)
        self.assertEqual(200, facts.as_of)

    def test_ai_payload_uses_only_frozen_fact_status_and_proof_tier(self):
        facts = DigestEventFacts(
            as_of=200, claims=(self.claim(status="disputed"),),
            evidence=(PersistedEventClaimEvidence("claim", 1, "supports"),),
        )
        payload = ApiDigestSummarizer._item_evidence(self.cluster(facts), 1, 2000)
        self.assertEqual(200, payload["frozen_facts"]["as_of"])
        claim = payload["frozen_facts"]["claims"][0]
        self.assertEqual("disputed", claim["status"])
        self.assertEqual([{"stance": "supports", "tier": "primary"}], claim["proofs"])
        self.assertFalse(payload["frozen_facts"]["truncated"])

    def test_snapshot_rejects_evidence_outside_selected_reports(self):
        facts = DigestEventFacts(
            as_of=200, claims=(self.claim(),), evidence=(PersistedEventClaimEvidence("claim", 2, "supports"),),
        )
        with self.assertRaisesRegex(ValueError, "outside selected reports"):
            self.cluster(facts)

    def test_ai_payload_marks_fact_truncation_even_when_no_claim_fits(self):
        facts = DigestEventFacts(
            as_of=200, claims=(self.claim(),), evidence=(PersistedEventClaimEvidence("claim", 1, "supports"),),
        )
        payload = ApiDigestSummarizer._item_evidence(self.cluster(facts), 1, 80)
        self.assertEqual([], payload["frozen_facts"]["claims"])
        self.assertTrue(payload["frozen_facts"]["truncated"])

    def test_independently_bounded_graph_drops_orphan_evidence_and_marks_truncated(self):
        claims = tuple(self.claim(f"claim-{index}") for index in range(200))
        graph = EventEvidenceGraph(
            event=PersistedEvent("event", "fp", "Event", "summary", 4, 4, 3, 4, 0.9, 100, 100),
            reports=(self.report(),), claims=claims,
            evidence=tuple(PersistedEventClaimEvidence(f"claim-{index}", 1, "supports")
                           for index in range(201)),
            timeline=(), notifications=(), truncated={},
        )
        facts = freeze_digest_event_facts(graph, (self.report(),), as_of=200)
        self.assertEqual(200, len(facts.claims))
        self.assertEqual(200, len(facts.evidence))
        self.assertTrue(facts.truncated)

    def test_empty_truncated_fact_snapshot_still_warns_model(self):
        payload = ApiDigestSummarizer._item_evidence(
            self.cluster(DigestEventFacts(as_of=200, truncated=True)), 1, 2000,
        )
        self.assertEqual({"as_of": 200, "claims": [], "truncated": True}, payload["frozen_facts"])
