from __future__ import annotations

import asyncio
import io
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from argus.analysis import analyze_observation
from argus.config import DigestConfig, _parse_rule, parse_source_config
from argus.content import ContentDocumentDraft, ContentLevel
from argus.database import Database
from argus.digest import DigestScheduler, cluster_observations
from argus.digest_analysis import ApiDigestSummarizer
from argus.host import HostHealthCollector, _matches
from argus.model_analyzers import AnalyzerError, AnalyzerSettings, OpenAICompatibleAnalyzer
from argus.models import FeedFetchResult, SourceState
from argus.news_catalog import NEWS_SOURCE_CATALOG
from argus.news_rollout import plan_official_news
from argus.official_list import OfficialListCollector
from argus.rss import FeedError, parse_feed
from argus.rules import RuleSet
from argus.runtime_rollout import plan_runtime_audit
from tests.helpers import observation
from tests.test_digest import _row


class RuntimeAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'audit.db')
        self.now = 1789214400  # 2026-09-12 12:00 UTC (daily publication boundary)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def ingest(self, item, rules=None):
        return self.db.record_source_success(item.source_id, FeedFetchResult((item,), None, None),
                                             rules or RuleSet(()), self.now - 10, 'eos')

    def test_rule_notification_survives_reanalysis_and_candidate_limit(self):
        planned, _ = plan_official_news({})
        rules = RuleSet.from_config((_parse_rule(planned['rules'][0], 0),), 'eos')
        source = 'china_mof_announcements'
        self.ingest(observation('baseline', 'routine', source_id=source, timestamp=self.now-20))
        self.ingest(observation('critical', '启动一级应急响应', source_id=source, timestamp=self.now-10), rules)
        for work in self.db.claim_analysis_observations(self.now, limit=10, lease_seconds=300):
            self.db.save_analysis_result(work.observation_id, work.lease_token,
                                         replace(analyze_observation(work.observation), handling='archive'), (),
                                         processing_state='analyzed', now=self.now)
        rows = self.db.list_digest_observations(self.now-100, self.now, source_ids=[source], limit=1)
        self.assertEqual('启动一级应急响应', rows[0]['title'])
        self.assertEqual('immediate', rows[0]['handling'])
        self.assertEqual([], self.db.list_digest_observations(self.now-100, self.now, source_ids=['disabled']))

    def test_diversity_and_fractional_scores_keep_other_sources_visible(self):
        rows = [_row(i, f'Weather bulletin {i}', source_id='weather') for i in range(1, 15)]
        # Avoid intentionally clustering all weather bulletins in this test.
        for i, row in enumerate(rows):
            row['topic'] = str(i)
        rows.append(_row(30, 'New vaccine programme', source_id='health', importance=3))
        result = cluster_observations(rows, max_items=3)
        self.assertTrue(any('health' in item.source_ids for item in result))
        self.assertTrue(any(item.score != round(item.score) for item in result))

    async def test_digest_model_failure_publishes_once_without_blocking_loop(self):
        self.ingest(observation('item', 'policy announcement', timestamp=self.now-10))
        class SlowFailure:
            def summarize(self, digest):
                time.sleep(0.05)
                raise AnalyzerError('provider unavailable')
        scheduler = DigestScheduler(DigestConfig(enabled=True, notify=False), self.db,
                                    topic='eos', summarizer=SlowFailure())
        ticks = []
        async def ticker():
            await asyncio.sleep(0.01)
            ticks.append(True)
        digest, _ = await asyncio.gather(scheduler.process_once_async(self.now), ticker())
        self.assertTrue(ticks)
        self.assertEqual('algorithm', digest.generation_kind)
        self.assertIn('失败', digest.summary)
        self.assertEqual(digest, await scheduler.process_once_async(self.now+60))
        self.assertEqual(1, self.db.connection.execute('SELECT SUM(calls) FROM analysis_api_usage').fetchone()[0])

    async def test_digest_full_text_and_budget_use_repository_before_worker(self):
        item = replace(observation('content', 'Official release', timestamp=self.now-10),
                       content_documents=(ContentDocumentDraft(ContentLevel.FULL_TEXT, 'public_html', 'Saved official text'),))
        self.ingest(item)
        seen = []
        class Summary:
            def summarize(self, digest):
                seen.append(digest.items[0].summary)
                return '基于正文的摘要 [1]'
        config = DigestConfig(enabled=True, notify=False)
        scheduler = DigestScheduler(config, self.db, topic='eos', summarizer=Summary(), send_full_text=True)
        result = await scheduler.process_once_async(self.now)
        self.assertEqual(['Saved official text'], seen)
        self.assertEqual('api', result.generation_kind)
        self.assertNotEqual('Saved official text', result.items[0].summary)
        tomorrow = await DigestScheduler(config, self.db, topic='eos', summarizer=Summary(), daily_api_budget=0).process_once_async(self.now+86400)
        self.assertEqual('algorithm', tomorrow.generation_kind)

    def test_model_rejects_oversize_envelope_and_unknown_citations(self):
        client = OpenAICompatibleAnalyzer(AnalyzerSettings('https://example.test/v1', 'test', max_response_bytes=1024))
        client._opener = Mock()
        client._opener.open.return_value = io.BytesIO(b' ' * 1025)
        with self.assertRaisesRegex(AnalyzerError, 'exceeded'):
            client.complete('test')
        self.ingest(observation('item', 'policy announcement', timestamp=self.now-10))
        digest = DigestScheduler(DigestConfig(enabled=True, notify=False), self.db, topic='eos').process_once(self.now)
        with patch.object(client, 'complete', return_value=json.dumps({'summary': 'Untrusted claim ' * 5 + '[99]', 'citations': [99]})):
            with self.assertRaisesRegex(AnalyzerError, 'citations'):
                ApiDigestSummarizer(client).summarize(digest)

    def test_host_initial_alert_and_unknown_disk_not_false_recovery(self):
        source = parse_source_config({'id':'host_test','kind':'host','publisher':'bk','section':'Host','dedupe_scope':'host',
                                      'enabled':True,'poll_interval_seconds':60, 'request_timeout_seconds':20,
                                      'request_attempts':1,'retry_base_seconds':1,'max_response_bytes':1024,
                                      'settings':{'paths':['/'], 'disk_used_percent':90}})
        state = SourceState(source.id, False, None, None, None, None, 0, False, None)
        rules = RuleSet.from_config((_parse_rule({'id':'host_rule','kind':'weighted_text','source_ids':[source.id],
            'threshold':1,'priority':4,'notification_title':'Host','tags':['computer'],'max_item_age_seconds':3600,
            'patterns':[{'label':'all','regex':'.','title_weight':2,'summary_weight':0}]},0),), 'eos')
        with patch('argus.host.shutil.disk_usage', return_value=Mock(total=100, used=95)), patch('argus.host._mem_percent', return_value=10), patch('argus.host.os.getloadavg', return_value=(0,0,0)):
            initial = HostHealthCollector(source).fetch(state)
        result = self.db.record_source_success(source.id, initial, rules, int(time.time()), 'eos')
        self.assertGreater(result.queued_alerts, 0)
        with patch('argus.host.shutil.disk_usage', side_effect=OSError), patch('argus.host._mem_percent', return_value=10), patch('argus.host.os.getloadavg', return_value=(0,0,0)):
            unknown = HostHealthCollector(source).fetch(replace(state, initialized=True, cursor=initial.cursor))
        self.assertFalse(any(x.attributes.get('recovery') and x.attributes['check']=='disk:/' for x in unknown.observations))
        self.assertTrue(_matches(('tcp','ipv4',22),('tcp','0.0.0.0',22)))
        self.assertTrue(_matches(('tcp','ipv6',22),('tcp','::',22)))
        self.assertFalse(_matches(('tcp','ipv4',22),('tcp','::',22)))

    def test_official_json_and_japanese_group_dates(self):
        raw = NEWS_SOURCE_CATALOG.source_template('china_state_council','announcements','council',enabled=True,user_confirmed=True)
        collector = OfficialListCollector(parse_source_config(raw))
        payload = json.dumps([{'URL':'https://www.gov.cn/zhengce/content/202609/content_123.htm','TITLE':'国务院政策公告','DOCRELPUBTIME':'2026-09-11'}]).encode()
        self.assertEqual(1,len(collector.parse_payload(payload)))
        with self.assertRaises(FeedError):
            collector.parse_payload(b'{"unexpected": []}')
        raw = NEWS_SOURCE_CATALOG.source_template('japan_mof','announcements','jpmof',enabled=True,user_confirmed=True)
        item = OfficialListCollector(parse_source_config(raw)).parse_payload(b'<a name="a20260911"></a><a class="information-item-inner" href="/english/policy/budget/release.html">Budget announcement</a>')[0]
        self.assertEqual(2026,item.published_at.year)

    def test_jma_category_does_not_alert_but_actual_emergency_does(self):
        raw = NEWS_SOURCE_CATALOG.source_template('japan_meteorological_agency','high_frequency','jma',enabled=True,user_confirmed=True)
        source = parse_source_config(raw)
        planned,_ = plan_official_news({})
        rule = planned['rules'][0];rule['source_ids']=['jma']
        rules = RuleSet.from_config((_parse_rule(rule,0),),'eos')
        for summary, expected in [('濃霧による視程障害に注意してください。',False),('大津波警報を発表。直ちに避難してください。',True)]:
            payload = f'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>1</id><title>気象特別警報・警報・注意報</title><content>{summary}</content><updated>2026-09-12T11:59:00Z</updated></entry></feed>'.encode()
            item = parse_feed(payload,source)[0]
            self.assertEqual(expected,bool(rules.evaluate(item,self.now)))

    def test_runtime_rollout_is_idempotent_and_preserves_operator_choices(self):
        current, _ = plan_official_news({})
        current['rules'].append({
            'id': 'global_breaking', 'kind': 'weighted_text', 'source_ids': [],
            'threshold': 1, 'max_item_age_seconds': 86400,
            'notification_title': 'Global', 'priority': 4, 'tags': ['newspaper'],
            'patterns': [{'label': 'all', 'regex': '.', 'title_weight': 1, 'summary_weight': 0}],
        })
        current['sources'].append({
            **NEWS_SOURCE_CATALOG.source_template(
                'economist', 'business', 'economist_business',
                enabled=False, user_confirmed=True,
            ),
        })
        current['analysis'] = {'enabled': False, 'api_enabled': False}
        current['digest'] = {'api_summary': False}
        planned, first_probes = plan_runtime_audit(current)
        repeated, second_probes = plan_runtime_audit(planned)
        self.assertEqual(planned, repeated)
        self.assertTrue(first_probes)
        self.assertEqual([], second_probes)
        economist = next(source for source in planned['sources'] if source['id'] == 'economist_business')
        self.assertFalse(economist['enabled'])
        self.assertEqual({'enabled': False, 'api_enabled': False}, planned['analysis'])
        self.assertFalse(planned['digest']['api_summary'])
        memberships = {rule['id']: set(rule['source_ids']) for rule in planned['rules']}
        self.assertTrue({
            'china_state_council_announcements', 'japan_mof_announcements',
            'federal_reserve_press_all', 'federal_reserve_speeches',
        } <= memberships['official_news_critical'])
        self.assertTrue({
            'economist_world_this_week', 'economist_business',
            'financial_times_global_economy',
        } <= memberships['global_breaking'])
        self.assertEqual({'host_health'}, memberships['host_health_notify'])


if __name__ == '__main__':
    unittest.main()
