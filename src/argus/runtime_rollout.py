"""Explicit, reviewable production rollout for the runtime audit fixes."""
from __future__ import annotations

import os
from copy import deepcopy
from typing import Any, Mapping

from .news_catalog import NEWS_SOURCE_CATALOG


def plan_runtime_audit(current: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    planned = deepcopy(dict(current))
    sources = planned.setdefault('sources', [])
    rules = planned.setdefault('rules', [])
    probes = []
    additions = (
        ('china_state_council', 'announcements'), ('japan_mof', 'announcements'),
        ('economist', 'world_this_week'), ('economist', 'business'),
        ('financial_times', 'global_economy'),
        ('federal_reserve', 'press_all'), ('federal_reserve', 'speeches'),
    )
    for entry, feed in additions:
        source_id = f'{entry}_{feed}'
        if any(source['id'] == source_id for source in sources):
            continue
        source = NEWS_SOURCE_CATALOG.source_template(entry, feed, source_id,
                    enabled=True, user_confirmed=True, poll_interval_seconds=900)
        sources.append(source)
        probes.append(source)
    for source in sources:
        if source['id'] == 'japan_meteorological_agency_high_frequency':
            settings = source.setdefault('settings', {})
            changed = settings.get('headline_from_summary') is not True
            settings['headline_from_summary'] = True
            if changed and source.get('enabled', True):
                probes.append(source)
        if source['id'] == 'japan_cabinet_announcements':
            settings = source.setdefault('settings', {})
            prefixes = ['https://japan.kantei.go.jp/']
            changed = settings.get('article_url_prefixes') != prefixes
            settings['article_url_prefixes'] = prefixes
            if changed and source.get('enabled', True):
                probes.append(source)
    official = next((rule for rule in rules if rule['id'] == 'official_news_critical'), None)
    if official is None:
        raise ValueError('install the reviewed official news rule before this rollout')
    for entry in ('china_state_council_announcements', 'japan_mof_announcements',
                  'federal_reserve_press_all', 'federal_reserve_speeches'):
        if entry not in official['source_ids']:
            official['source_ids'].append(entry)
    global_rule = next((rule for rule in rules if rule['id'] == 'global_breaking'), None)
    if global_rule is None:
        raise ValueError('install the reviewed global breaking rule before this rollout')
    for entry in ('economist_world_this_week', 'economist_business',
                  'financial_times_global_economy'):
        if entry not in global_rule['source_ids']:
            global_rule['source_ids'].append(entry)
    label = '气象厅通用栏目标题不是实际警报'
    if not any(pattern['label'] == label for pattern in official['patterns']):
        official['patterns'].append({'label':label, 'regex':'^気象特別警報・警報・注意報$',
                                    'title_weight':-20, 'summary_weight':0})
    if not any(source['id'] == 'host_health' for source in sources):
        host = {'id':'host_health','kind':'host','publisher':'bk 主机','section':'Host',
                'dedupe_scope':'host_health','enabled':True,'poll_interval_seconds':60,
                'request_timeout_seconds':30,'request_attempts':1,'retry_base_seconds':1,
                'max_response_bytes':1024,'region':'OTHER','default_importance':4,
                'settings':{'paths':['/'], 'units':['nginx.service','ntfy.service','argus-admin.service',
                                                  'ssh.service','mc.service','alist.service'],
                            'disk_used_percent':90,'inode_used_percent':90,'memory_used_percent':90,
                            'load1':max(4, (os.cpu_count() or 1)*1.5)}}
        sources.append(host)
        probes.append(host)
        rules.append({'id':'host_health_notify','kind':'weighted_text','source_ids':['host_health'],
                      'threshold':1,'max_item_age_seconds':3600,'notification_title':'主机健康',
                      'priority':4,'tags':['computer'],
                      'patterns':[{'label':'异常与恢复','regex':'.','title_weight':2,'summary_weight':1}]})
    # There was no semantic runtime configuration in production. Existing
    # operator choices, including a later explicit disable, remain authoritative.
    planned.setdefault('analysis', {
        'enabled':True, 'api_enabled':True, 'api_triage_enabled':False,
        'local_enabled':False, 'shadow_mode':True,
        'api_base_url':'https://api.juggler.cc/v1', 'api_model':'gpt-5.4-mini',
        'api_key_env':'ARGUS_ANALYSIS_API_KEY', 'timeout_seconds':45,
        'max_input_chars':40000, 'max_response_bytes':65536, 'max_tokens':3000,
        'max_items_per_run':50, 'daily_api_budget':2, 'send_full_text':False,
        'region_weights':{'CN':5,'JP':4,'US':5,'GLOBAL':3,'OTHER':3},
    })
    planned.setdefault('digest', {}).setdefault('api_summary', True)
    return planned, probes
