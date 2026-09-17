from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from argus.database import Database
from argus.digest import DigestBuilder, DigestDocument, cluster_observations_event_centric
from argus.events import PersistedEvent
from argus.models import FeedFetchResult
from argus.rules import RuleSet
from helpers import observation
from test_digest import END, START, _row


class DigestIdentityTests(unittest.TestCase):
    def test_distinct_batch_clusters_matching_one_historical_event_keep_all_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "state.db")
            self.addCleanup(database.close)
            title = "央行维持政策利率"
            rows = [
                _row(1, title, source_id="official", topic="policy"),
                _row(2, title, source_id="media", topic="economy"),
            ]
            rows[1]["handling"] = "immediate"
            rows[1]["source_tier"] = "secondary"
            for row in rows:
                draft = replace(
                    observation(str(row["id"]), title, source_id=row["source_id"],
                                timestamp=row["published_at"]),
                    dedupe_scope=row["source_id"],
                )
                database.record_source_success(
                    draft.source_id, FeedFetchResult((draft,), None, None), RuleSet(()),
                    START + 10, "eos",
                )
            database.save_event(PersistedEvent(
                event_key="historical-event", fingerprint="historical-fingerprint",
                title=title, summary=title, score=4.0, importance=4, urgency=3,
                relevance=4, confidence=0.8, first_seen_at=START, last_seen_at=START,
                regions=("CN",), topics=("general",), created_at=START, updated_at=START,
            ))
            clusters = cluster_observations_event_centric(rows)
            self.assertEqual(2, len(clusters))
            builder = DigestBuilder(database)
            persisted = builder._persist_events(clusters, created_at=END)

            # Batch clustering rejects the topic mismatch, but historical
            # continuation legitimately resolves both to the existing event.
            # The digest must contain that stable identity exactly once.
            self.assertEqual(1, len(persisted))
            item = persisted[0]
            self.assertEqual("historical-event", item.cluster_key)
            self.assertEqual((1, 2), item.observation_ids)
            self.assertEqual(("media", "official"), item.source_ids)
            self.assertEqual(("secondary", "primary"), item.source_tiers)
            self.assertEqual(2, len(item.reports))
            self.assertEqual(2, len(item.links))
            self.assertEqual("immediate", item.handling)
            self.assertEqual(("economy", "policy"), item.topics)
            self.assertEqual(2, database.get_event("historical-event").independent_source_count)
            self.assertEqual(2, len(database.list_event_reports("historical-event")))
            document = DigestDocument(
                digest_key="daily:identity-regression", version=0,
                period_start=START, period_end=END, timezone="Asia/Shanghai",
                title="Algorithm fallback", summary="Algorithm summary",
                generation_kind="algorithm", items=persisted, coverage=(), created_at=END,
            )
            saved = database.save_digest(document)
            published = database.publish_digest(saved.digest_key, saved.version, END)
            self.assertEqual("published", published.status)
            self.assertEqual("algorithm", published.generation_kind)


if __name__ == "__main__":
    unittest.main()
