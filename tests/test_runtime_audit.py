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
from argus.digest import DigestBuilder, DigestScheduler, cluster_observations, with_api_summary
from argus.digest_analysis import ApiDigestSummarizer
from argus.host import HostHealthCollector, _matches
from argus.model_analyzers import AnalyzerError, AnalyzerSettings, OpenAICompatibleAnalyzer
from argus.models import FeedFetchResult, SourceState
from argus.news_catalog import NEWS_SOURCE_CATALOG
from argus.news_rollout import plan_official_news
from argus.official_list import OfficialListCollector
from argus.prompts import DIGEST_V4
from argus.rss import FeedError, parse_feed
from argus.rules import RuleSet
from argus.runtime_rollout import plan_runtime_audit
from tests.helpers import observation
from tests.test_digest import FakeDigestRepository, _row


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
        self.assertEqual(1, self.db.connection.execute('SELECT SUM(calls) FROM digest_api_usage').fetchone()[0])

    async def test_digest_model_failure_retries_after_fallback_publication(self):
        self.ingest(observation('retry-item', 'policy announcement', timestamp=self.now-10))

        class EventuallyAvailable:
            def __init__(self):
                self.calls = 0

            def summarize(self, digest):
                self.calls += 1
                if self.calls == 1:
                    raise AnalyzerError('temporary provider outage')
                return '模型恢复后补发的日报摘要，保留事实和证据编号。[1]'

        summarizer = EventuallyAvailable()
        scheduler = DigestScheduler(
            DigestConfig(enabled=True, notify=False), self.db, topic='eos',
            summarizer=summarizer, daily_api_budget=5,
        )
        fallback = await scheduler.process_once_async(self.now)
        self.assertEqual('algorithm', fallback.generation_kind)
        self.assertEqual('published', fallback.status)
        self.assertEqual(1, summarizer.calls)
        self.assertIsNotNone(self.db.get_digest_retry(fallback.digest_key))

        # The worker polls frequently, but must not retry before its durable
        # next-attempt timestamp.
        same = await scheduler.process_once_async(self.now + 3599)
        self.assertEqual(fallback.version, same.version)
        self.assertEqual(1, summarizer.calls)

        polished = await scheduler.process_once_async(self.now + 3600)
        self.assertEqual('api', polished.generation_kind)
        self.assertGreater(polished.version, fallback.version)
        self.assertEqual(2, summarizer.calls)
        self.assertEqual('succeeded', self.db.get_digest_retry(fallback.digest_key).status)

    async def test_digest_model_failure_exhausts_exactly_five_attempts(self):
        self.ingest(observation('failed-item', 'policy announcement', timestamp=self.now-10))

        class AlwaysUnavailable:
            calls = 0

            def summarize(self, digest):
                self.calls += 1
                raise AnalyzerError('provider unavailable')

        summarizer = AlwaysUnavailable()
        scheduler = DigestScheduler(
            DigestConfig(enabled=True, notify=False), self.db, topic='eos',
            summarizer=summarizer,
        )
        fallback = await scheduler.process_once_async(self.now)
        # Normal polling jitter must not lose the fourth retry at the deadline.
        for offset in (3607, 7214, 10821, 14428):
            await scheduler.process_once_async(self.now + offset)
        state = self.db.get_digest_retry(fallback.digest_key)
        self.assertEqual(5, summarizer.calls)
        self.assertEqual(5, state.attempts)
        self.assertEqual('failed', state.status)
        self.assertEqual(
            5,
            self.db.connection.execute(
                'SELECT calls FROM digest_api_usage WHERE digest_key = ?',
                (fallback.digest_key,),
            ).fetchone()[0],
        )

    async def test_invalid_model_output_falls_back_and_retries_after_restart(self):
        self.ingest(observation('invalid-output', 'policy announcement', timestamp=self.now-10))
        summarizer = Mock()
        summarizer.summarize.side_effect = [None, ' ' * 50, '有效的综合摘要。[1]']
        config = DigestConfig(enabled=True, notify=True, public_base_url='https://eos.example.test')
        scheduler = DigestScheduler(config, self.db, topic='eos', summarizer=summarizer)
        fallback = await scheduler.process_once_async(self.now)
        self.assertEqual('algorithm', fallback.generation_kind)
        self.assertIn('summary must be text', self.db.get_digest_retry(fallback.digest_key).last_error)
        self.assertEqual(1, len(self.db.list_alerts()))

        self.db.close()
        self.db = Database(Path(self.temp.name) / 'audit.db')
        scheduler = DigestScheduler(config, self.db, topic='eos', summarizer=summarizer)
        self.assertEqual(fallback.version, (await scheduler.process_once_async(self.now + 3608)).version)
        self.assertEqual(2, self.db.get_digest_retry(fallback.digest_key).attempts)
        result = await scheduler.process_once_async(self.now + 7208)
        self.assertEqual('api', result.generation_kind)
        self.assertEqual(2, result.version)
        self.assertEqual('succeeded', self.db.get_digest_retry(fallback.digest_key).status)
        await scheduler.process_once_async(self.now + 7268)
        self.assertEqual(3, summarizer.summarize.call_count)
        self.assertEqual(2, len(self.db.list_alerts()))

    async def test_expiry_does_not_invent_an_attempt(self):
        self.ingest(observation('expiry', 'policy announcement', timestamp=self.now-10))
        summarizer = Mock()
        summarizer.summarize.side_effect = AnalyzerError('temporary outage')
        scheduler = DigestScheduler(DigestConfig(enabled=True, notify=False), self.db,
                                    topic='eos', summarizer=summarizer)
        fallback = await scheduler.process_once_async(self.now)
        await scheduler.process_once_async(self.now + 18001)
        state = self.db.get_digest_retry(fallback.digest_key)
        self.assertEqual(('failed', 1), (state.status, state.attempts))
        await scheduler.process_once_async(self.now + 18061)
        self.assertEqual(1, summarizer.summarize.call_count)
        self.assertEqual(1, self.db.get_digest_failure_state()[0])

    async def test_missing_retry_row_is_recovered_without_extending_window(self):
        self.ingest(observation('reconstruct', 'policy announcement', timestamp=self.now-10))
        config = DigestConfig(enabled=True, notify=True, public_base_url='https://eos.example.test')
        summarizer = Mock()
        summarizer.summarize.side_effect = AnalyzerError('temporary outage')
        scheduler = DigestScheduler(config, self.db, topic='eos', summarizer=summarizer)
        draft = scheduler._prepare(self.now)
        fallback = replace(draft, summary=draft.summary + ' AI 总结暂时失败，已先发送算法版日报。')
        saved = self.db.save_digest(fallback)
        self.db.publish_digest(saved.digest_key, saved.version, self.now)
        # Crash after publication, before both notification and retry-state save.
        await scheduler.process_once_async(self.now + 18100)
        state = self.db.get_digest_retry(saved.digest_key)
        self.assertEqual(self.now + 18000, state.retry_deadline_at)
        self.assertEqual(('failed', 1), (state.status, state.attempts))
        summarizer.summarize.assert_not_called()
        self.assertEqual(1, len(self.db.list_alerts()))

    async def test_published_ai_after_crash_is_success_even_after_deadline(self):
        self.ingest(observation('already-published', 'policy announcement', timestamp=self.now-10))
        config = DigestConfig(enabled=True, notify=True, public_base_url='https://eos.example.test')
        summarizer = Mock()
        summarizer.summarize.side_effect = AnalyzerError('temporary outage')
        scheduler = DigestScheduler(config, self.db, topic='eos', summarizer=summarizer)
        fallback = await scheduler.process_once_async(self.now)
        self.db.reserve_digest_api_call(fallback.digest_key, limit=5, now=self.now+3600)
        polished = self.db.save_digest(with_api_summary(fallback, '成功的摘要。[1]', created_at=self.now+3600))
        self.db.publish_digest(polished.digest_key, polished.version, self.now+3600)
        # Crash after AI publication, before outbox/retry acknowledgement.
        await scheduler.process_once_async(self.now + 18001)
        state = self.db.get_digest_retry(fallback.digest_key)
        self.assertEqual(('succeeded', 2), (state.status, state.attempts))
        self.assertEqual(0, self.db.get_digest_failure_state()[0])
        self.assertEqual(2, len(self.db.list_alerts()))
        self.assertEqual(1, summarizer.summarize.call_count)

    async def test_local_duplicate_evidence_does_not_spend_ai_budget(self):
        self.ingest(observation('preflight', 'policy announcement', timestamp=self.now-10))
        summarizer = Mock()
        scheduler = DigestScheduler(DigestConfig(enabled=True, notify=False), self.db,
                                    topic='eos', summarizer=summarizer)
        draft = scheduler._prepare(self.now)
        invalid = replace(draft, items=(draft.items[0], draft.items[0]))
        with self.assertRaisesRegex(ValueError, 'duplicate cluster keys before inference'):
            await scheduler._summarize(invalid, self.now)
        summarizer.summarize.assert_not_called()
        self.assertEqual(0, self.db.connection.execute('SELECT COUNT(*) FROM digest_api_usage').fetchone()[0])

    def test_starting_retry_again_preserves_existing_window_and_attempts(self):
        key = 'daily:2026-09-12'
        self.db.start_digest_retry(key, 1, self.now, self.now+18000, last_error='first error')
        state = self.db.record_digest_retry(key, attempts=3, status='pending',
                                           next_attempt_at=self.now+10800, last_error='third error', now=self.now+7200)
        self.assertEqual(state, self.db.start_digest_retry(key, 1, self.now+7500, self.now+25500))

    async def test_four_failed_days_raise_one_operator_notification(self):
        config = DigestConfig(enabled=True, notify=True, public_base_url='https://eos.example.test')
        summarizer = Mock()
        summarizer.summarize.side_effect = AnalyzerError('temporary outage')
        scheduler = DigestScheduler(config, self.db, topic='eos', summarizer=summarizer)
        for day in range(4):
            instant = self.now + day * 86400
            self.ingest(observation(f'day-{day}', f'policy announcement {day}', timestamp=instant-10))
            for offset in (0, 3607, 7214, 10821, 14428, 14488):
                await scheduler.process_once_async(instant+offset)
        alerts = self.db.list_alerts(limit=100)
        self.assertEqual(1, sum(row['rule_id'] == 'digest.ai_failure' for row in alerts))
        self.assertEqual(4, self.db.get_digest_failure_state()[0])

    def test_digest_invalid_first_model_falls_back_to_second(self):
        self.ingest(observation('item', 'policy announcement', timestamp=self.now-10))
        digest = DigestScheduler(DigestConfig(enabled=True, notify=False), self.db, topic='eos').process_once(self.now)
        first = OpenAICompatibleAnalyzer(AnalyzerSettings('https://api.example.test/v1', 'first'))
        second = OpenAICompatibleAnalyzer(AnalyzerSettings('https://api.example.test/v1', 'second'))
        first.complete = Mock(return_value='not-json')  # type: ignore[method-assign]
        second.complete = Mock(return_value=json.dumps({
            'summary': '第二个模型成功整理了这条信息，并保留了可核对的证据引用，同时说明了事件背景和潜在影响。[1]',
            'citations': [1],
        }))  # type: ignore[method-assign]
        result = ApiDigestSummarizer((first, second)).summarize(digest)
        self.assertIn('第二个模型成功', result)
        first.complete.assert_called_once()
        second.complete.assert_called_once()

    def test_large_digest_preserves_original_topic_ids_in_synthesis(self):
        rows = [
            _row(index, f'独立主题 {index}', source_id=f'source_{index}', topic=f'topic_{index}')
            for index in range(1, 14)
        ]
        digest = DigestBuilder(FakeDigestRepository(rows)).build(
            digest_key='test:large', period_start=self.now - 86400, period_end=self.now,
            timezone='Asia/Shanghai', created_at=self.now,
        )
        self.assertEqual(13, len(digest.items))
        client = OpenAICompatibleAnalyzer(AnalyzerSettings('https://api.example.test/v1', 'model'))
        calls = []

        def complete(payload):
            calls.append(json.loads(payload))
            if len(calls) == 1:
                return json.dumps({'expand_topics': [13, 7]})
            return json.dumps({
                'summary': '模型综合了原始编号十三和七的主题，引用仍能直接对应日报列表，并保留了可核对的事实、影响和后续观察信息。[13][7]',
                'citations': [13, 7],
            })

        client.complete = Mock(side_effect=complete)  # type: ignore[method-assign]
        result = ApiDigestSummarizer(client).summarize(digest)
        self.assertIn('[13][7]', result)
        self.assertEqual([13, 7], calls[1]['selected_topic_ids'])
        self.assertEqual([13, 7], [item['id'] for item in calls[1]['items']])

    def test_large_digest_keeps_all_model_selected_topics(self):
        rows = [
            _row(index, f'独立主题 {index}', source_id=f'source_{index}', topic=f'topic_{index}')
            for index in range(1, 16)
        ]
        digest = DigestBuilder(FakeDigestRepository(rows)).build(
            digest_key='test:all-selected', period_start=self.now - 86400, period_end=self.now,
            timezone='Asia/Shanghai', created_at=self.now,
        )
        client = OpenAICompatibleAnalyzer(AnalyzerSettings('https://api.example.test/v1', 'model'))
        calls = []

        def complete(payload):
            calls.append(json.loads(payload))
            if len(calls) == 1:
                return json.dumps({'expand_topics': list(range(1, 16))})
            ids = list(range(1, 16))
            return json.dumps({
                'summary': ' '.join(f'主题 {index} 的事实与影响已核对。[{index}]' for index in ids),
                'citations': ids,
            })

        client.complete = Mock(side_effect=complete)  # type: ignore[method-assign]
        ApiDigestSummarizer(client).summarize(digest)
        self.assertEqual(list(range(1, 16)), calls[1]['selected_topic_ids'])
        self.assertEqual(15, len(calls[1]['items']))

    def test_digest_prompt_defines_distinct_index_and_synthesis_contracts(self):
        self.assertEqual(4, DIGEST_V4.version)
        self.assertIn('stage 为 index', DIGEST_V4.system_text)
        self.assertIn('expand_topics', DIGEST_V4.system_text)
        self.assertIn('自主决定需要深入阅读的主题数量', DIGEST_V4.system_text)
        self.assertNotIn('最多选择 12 个', DIGEST_V4.system_text)
        self.assertIn('stage 为 synthesis', DIGEST_V4.system_text)
        self.assertIn('由你根据材料复杂度决定合适长度', DIGEST_V4.system_text)
        self.assertNotIn('600 至 1200 字', DIGEST_V4.system_text)
        self.assertIn('中文只是输出语言', DIGEST_V4.system_text)
        self.assertIn('重要程度相近时，优先保持合理的跨地区、跨板块覆盖', DIGEST_V4.system_text)
        self.assertIn('不得使用机械地区配额', DIGEST_V4.system_text)
        self.assertIn('summary', DIGEST_V4.system_text)

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
        self.assertIsNone(self.db.connection.execute('SELECT SUM(calls) FROM analysis_api_usage').fetchone()[0])

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

    def test_runtime_rollout_migrates_only_the_legacy_region_defaults(self):
        current, _ = plan_official_news({})
        current['rules'].append({
            'id': 'global_breaking', 'kind': 'weighted_text', 'source_ids': [],
            'threshold': 1, 'max_item_age_seconds': 86400,
            'notification_title': 'Global', 'priority': 4, 'tags': ['newspaper'],
            'patterns': [{'label': 'all', 'regex': '.', 'title_weight': 1, 'summary_weight': 0}],
        })
        current['analysis'] = {
            'enabled': True,
            'region_weights': {'CN': 5, 'JP': 4, 'US': 5, 'GLOBAL': 3, 'OTHER': 3},
        }
        current['digest'] = {'api_summary': True}
        planned, _ = plan_runtime_audit(current)
        self.assertEqual(5, planned['analysis']['region_weights']['EAST_ASIA'])
        self.assertEqual(4, planned['analysis']['region_weights']['NORTH_AMERICA'])
        self.assertNotIn('CN', planned['analysis']['region_weights'])
        self.assertEqual('digest', planned['digest']['prompt_id'])
        self.assertEqual(4, planned['digest']['prompt_version'])

        custom = dict(current)
        custom['analysis'] = {'region_weights': {'CN': 3, 'US': 2}}
        preserved, _ = plan_runtime_audit(custom)
        self.assertEqual({'CN': 3, 'US': 2}, preserved['analysis']['region_weights'])


if __name__ == '__main__':
    unittest.main()
