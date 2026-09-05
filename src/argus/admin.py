from __future__ import annotations

import json
import mimetypes
import os
import secrets
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Mapping

from .config import AdminConfig, ConfigError, _parse_rule, _parse_source
from .adapters import AdapterError, build_collector
from .database import Database
from .models import SourceState
from .reminders import ReminderError, parse_reminder


class AdminError(ValueError):
    pass


def _reject_secret_values(value: Any, location: str = "config") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key).lower()
            sensitive = any(token in name for token in ("token", "password", "secret")) or name in {"key", "api_key", "apikey"} or name.endswith("_key")
            if sensitive and not name.endswith("_env"):
                raise AdminError(f"{location}.{key} must reference an *_env variable, not a secret")
            _reject_secret_values(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_values(item, f"{location}[{index}]")


def _public(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _public(item)
            for key, item in value.items()
            if str(key).lower().endswith("_env")
            or not any(token in str(key).lower() for token in ("token", "password", "secret"))
        }
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    return value


class ManagedConfigStore:
    def __init__(self, path: Path, base_source_ids: set[str] | None = None, base_rule_ids: set[str] | None = None) -> None:
        self.path = path
        self.base_source_ids = base_source_ids or set()
        self.base_rule_ids = base_rule_ids or set()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.history_path = self.path.with_name(self.path.name + ".revisions")
        self.history_path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.history_path, 0o700)
        except OSError:
            pass

    def read(self) -> dict[str, list[dict[str, Any]]]:
        if not self.path.exists():
            return {"sources": [], "rules": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AdminError(f"cannot read managed configuration: {exc}") from exc
        if not isinstance(data, Mapping):
            raise AdminError("managed configuration must be an object")
        return {
            "sources": list(data.get("sources", [])) if isinstance(data.get("sources", []), list) else [],
            "rules": list(data.get("rules", [])) if isinstance(data.get("rules", []), list) else [],
        }

    def metadata(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"revision": 0, "updated_at": None, "updated_by": None, "reason": None}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"revision": 0, "updated_at": None, "updated_by": None, "reason": None}
        return {
            "revision": int(data.get("revision", 0)) if isinstance(data, Mapping) else 0,
            "updated_at": data.get("updated_at") if isinstance(data, Mapping) else None,
            "updated_by": data.get("updated_by") if isinstance(data, Mapping) else None,
            "reason": data.get("reason") if isinstance(data, Mapping) else None,
        }

    @property
    def revision(self) -> int:
        return int(self.metadata()["revision"])

    def validate(self, data: Mapping[str, Any]) -> dict[str, Any]:
        sources = data.get("sources", [])
        rules = data.get("rules", [])
        if not isinstance(sources, list) or not isinstance(rules, list):
            raise AdminError("managed configuration sources and rules must be arrays")
        parsed_sources = tuple(_parse_source(item, index) for index, item in enumerate(sources))
        parsed_rules = tuple(_parse_rule(item, index) for index, item in enumerate(rules))
        source_ids = self.base_source_ids | {source.id for source in parsed_sources}
        rule_ids = self.base_rule_ids | {rule.id for rule in parsed_rules}
        if len(source_ids) != len(self.base_source_ids) + len(parsed_sources):
            raise AdminError("managed source IDs duplicate a base source or each other")
        if len(rule_ids) != len(self.base_rule_ids) + len(parsed_rules):
            raise AdminError("managed rule IDs duplicate a base rule or each other")
        unknown = sorted({source_id for rule in parsed_rules for source_id in rule.source_ids} - source_ids)
        if unknown:
            raise AdminError(f"rule references unknown sources: {', '.join(unknown)}")
        return {"sources": len(parsed_sources), "rules": len(parsed_rules), "source_ids": sorted(source_ids), "rule_ids": sorted(rule_ids)}

    def write(self, data: Mapping[str, Any], actor: str = "admin", reason: str = "configuration update") -> int:
        self.validate(data)
        revision = self.revision + 1
        envelope = {
            "format_version": 1,
            "revision": revision,
            "updated_at": int(time.time()),
            "updated_by": actor[:128],
            "reason": reason[:512],
            "sources": list(data["sources"]),
            "rules": list(data["rules"]),
        }
        payload = json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        snapshot = self.history_path / f"{revision:08d}.json"
        snapshot.write_text(payload, encoding="utf-8")
        os.chmod(snapshot, 0o600)
        return revision

    def upsert(self, kind: str, value: Mapping[str, Any]) -> dict[str, Any]:
        _reject_secret_values(value)
        index = "sources" if kind == "source" else "rules"
        parsed = _parse_source(value, 0) if kind == "source" else _parse_rule(value, 0)
        reserved = self.base_source_ids if kind == "source" else self.base_rule_ids
        if parsed.id in reserved:
            raise AdminError(f"{parsed.id} is defined in the base configuration and cannot be overwritten here")
        item = dict(value)
        item["id"] = parsed.id
        data = self.read()
        if kind == "rule":
            known_sources = self.base_source_ids | {
                str(row.get("id")) for row in data["sources"] if isinstance(row, Mapping)
            }
            unknown = sorted(set(parsed.source_ids) - known_sources)
            if unknown:
                raise AdminError(f"rule references unknown sources: {', '.join(unknown)}")
        rows = [row for row in data[index] if isinstance(row, Mapping) and row.get("id") != parsed.id]
        rows.append(item)
        data[index] = rows
        self.write(data)
        return item

    def upsert_bundle(
        self,
        source_value: Mapping[str, Any],
        rule_value: Mapping[str, Any] | None = None,
        actor: str = "admin",
    ) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
        """Save a source and its optional notification rule in one revision."""
        _reject_secret_values(source_value, "source")
        parsed_source = _parse_source(source_value, 0)
        if parsed_source.id in self.base_source_ids:
            raise AdminError(
                f"{parsed_source.id} is defined in the base configuration and cannot be overwritten here"
            )
        source = dict(source_value)
        source["id"] = parsed_source.id
        data = self.read()
        data["sources"] = [
            row
            for row in data["sources"]
            if isinstance(row, Mapping) and row.get("id") != parsed_source.id
        ] + [source]

        rule: dict[str, Any] | None = None
        if rule_value is not None:
            _reject_secret_values(rule_value, "rule")
            parsed_rule = _parse_rule(rule_value, 0)
            if parsed_rule.id in self.base_rule_ids:
                raise AdminError(
                    f"{parsed_rule.id} is defined in the base configuration and cannot be overwritten here"
                )
            if parsed_source.id not in parsed_rule.source_ids:
                raise AdminError("bundle rule must reference the saved source")
            rule = dict(rule_value)
            rule["id"] = parsed_rule.id
            data["rules"] = [
                row
                for row in data["rules"]
                if isinstance(row, Mapping) and row.get("id") != parsed_rule.id
            ] + [rule]

        revision = self.write(
            data,
            actor=actor,
            reason=f"upsert source bundle {parsed_source.id}",
        )
        return source, rule, revision

    def delete_bundle(self, identifier: str, actor: str = "admin") -> tuple[bool, list[str], int]:
        """Delete a managed source and every managed rule that references it."""
        data = self.read()
        existing = [
            row
            for row in data["sources"]
            if isinstance(row, Mapping) and row.get("id") == identifier
        ]
        if not existing:
            return False, [], self.revision
        removed_rules = [
            str(row.get("id"))
            for row in data["rules"]
            if isinstance(row, Mapping) and identifier in row.get("source_ids", [])
        ]
        data["sources"] = [
            row
            for row in data["sources"]
            if not (isinstance(row, Mapping) and row.get("id") == identifier)
        ]
        data["rules"] = [
            row
            for row in data["rules"]
            if not (isinstance(row, Mapping) and identifier in row.get("source_ids", []))
        ]
        revision = self.write(
            data,
            actor=actor,
            reason=f"delete source bundle {identifier}",
        )
        return True, removed_rules, revision

    def delete(self, kind: str, identifier: str) -> bool:
        index = "sources" if kind == "source" else "rules"
        data = self.read()
        if kind == "source":
            references = [
                str(row.get("id")) for row in data["rules"]
                if isinstance(row, Mapping) and identifier in row.get("source_ids", [])
            ]
            if references:
                raise AdminError(f"source is still referenced by rules: {', '.join(references)}")
        before = len(data[index])
        data[index] = [row for row in data[index] if row.get("id") != identifier]
        if len(data[index]) == before:
            return False
        self.write(data)
        return True

    def read_revision(self, revision: int) -> dict[str, list[dict[str, Any]]]:
        if revision == self.revision:
            return self.read()
        snapshot = self.history_path / f"{int(revision):08d}.json"
        if not snapshot.exists():
            raise AdminError(f"configuration revision {revision} does not exist")
        try:
            data = json.loads(snapshot.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AdminError(f"cannot read configuration revision: {exc}") from exc
        if not isinstance(data, Mapping):
            raise AdminError("configuration revision must be an object")
        result = {"sources": data.get("sources", []), "rules": data.get("rules", [])}
        self.validate(result)
        return result

    def rollback(self, revision: int, actor: str = "admin") -> int:
        data = self.read_revision(revision)
        return self.write(data, actor=actor, reason=f"rollback to revision {revision}")


_INDEX_HTML = r"""<!doctype html><html lang=zh-CN><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'><title>EyeOfSauron 管理</title>
<style>
:root{color-scheme:light;--ink:#18201d;--muted:#65706b;--line:#d8dfdb;--bg:#f3f6f4;--panel:#fff;--green:#176b4a;--red:#a33b31;--amber:#9a641c}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(135deg,#eef3f0 0,#f8faf9 45%,#edf2ef 100%);color:var(--ink);font:15px/1.45 system-ui,sans-serif;letter-spacing:0}
header{background:#15231e;color:#fff;padding:18px 24px}.bar,main{max-width:1080px;margin:auto}.bar{display:flex;align-items:center;justify-content:space-between}.brand{font-size:20px;font-weight:700}.sub{color:#b9c8c1;font-size:13px}
main{padding:24px}.status{display:grid;grid-template-columns:repeat(5,1fr);gap:1px;background:var(--line);border:1px solid var(--line);margin-bottom:24px}.metric{background:var(--panel);padding:14px}.metric b{display:block;font-size:22px}.metric span,label small{color:var(--muted)}
h2{font-size:18px;margin:28px 0 12px}form{background:var(--panel);border:1px solid var(--line);padding:18px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;border-radius:8px}.wide{grid-column:1/-1}label{display:grid;gap:5px;font-weight:600}input,select,textarea{width:100%;border:1px solid #b9c3be;background:#fff;padding:9px 10px;font:inherit;color:inherit;border-radius:4px}input[type=checkbox]{width:auto}textarea{min-height:82px;resize:vertical}button{border:0;border-radius:4px;padding:9px 13px;background:var(--green);color:#fff;font:600 14px system-ui;cursor:pointer}button.secondary{background:#e4e9e6;color:var(--ink)}button.danger{background:transparent;color:var(--red);border:1px solid #d9aaa5}.actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.message{min-height:22px;color:var(--muted)}
.items{display:grid;gap:8px}.item{background:var(--panel);border:1px solid var(--line);padding:14px;display:grid;grid-template-columns:1fr auto;gap:10px;align-items:center;border-radius:6px}.item b{display:block}.item code{color:var(--muted)}.item-message{margin:5px 0;color:#39443f;white-space:pre-wrap;overflow-wrap:anywhere}.notice{border-left:4px solid var(--amber);padding:10px 12px;background:#fff9ef;color:#6f4b17;margin:10px 0 14px}
#login{max-width:520px;margin:56px auto}#app[hidden]{display:none}.advanced{font-family:ui-monospace,monospace;font-size:13px}
@media(max-width:760px){main{padding:16px}.status{grid-template-columns:repeat(2,1fr)}form{grid-template-columns:1fr}.wide{grid-column:auto}.item{grid-template-columns:1fr}}
</style>
<header><div class=bar><div><div class=brand>EyeOfSauron</div><div class=sub>监测源、通知规则与定时提醒</div></div><button class=secondary onclick=logout()>锁定</button></div></header>
<main><section id=login><h2>管理认证</h2><form onsubmit='login(event)'><label class=wide>管理 Token<input id=token type=password autocomplete=current-password required><small>只保存在当前浏览器标签页。</small></label><button>解锁后台</button><div id=loginmsg class=message></div></form></section>
<div id=app hidden><section class=status><div class=metric><b id=reminderCount>0</b><span>有效提醒</span></div><div class=metric><b id=sourceCount>0</b><span>托管来源</span></div><div class=metric><b id=observationCount>0</b><span>观察记录</span></div><div class=metric><b id=outboxCount>0</b><span>待投递</span></div><div class=metric><b id=incidentCount>0</b><span>进行中事件</span></div></section>
<h2>定时提醒</h2><div class=notice>提醒会发到现有的 <b>eos</b> 主题，所有已订阅该主题的客户端都能收到。保存后立即生效，不需要重启服务。</div>
<form id=reminderForm onsubmit='saveReminder(event)'><input id=reminderId type=hidden>
<label>标题<input id=reminderTitle maxlength=128 value='定时提醒' required></label>
<label>发送方式<select id=reminderKind onchange=renderReminderFields()><option value=once>指定日期时间</option><option value=after>多久以后</option><option value=daily>每天固定时间</option></select></label>
<div id=reminderDynamic class=wide></div>
<label>通知优先级<select id=reminderPriority><option value=2>低</option><option value=3 selected>普通</option><option value=4>高</option><option value=5>最高</option></select></label>
<label><span><input id=reminderEnabled type=checkbox checked> 保存后启用</span><small>停用的提醒会保留配置，但不会发送。</small></label>
<label class=wide>提醒内容<textarea id=reminderBody maxlength=4096 placeholder='输入届时要发送给 ntfy 订阅用户的消息' required></textarea></label>
<div class='wide actions'><button>保存提醒</button><button type=button class=secondary onclick=resetReminderForm()>清空</button><span id=reminderMessage class=message></span></div></form>
<h2>已安排提醒</h2><div id=reminderItems class=items></div>
<h2>新增或更新监测</h2><form id=sourceForm onsubmit='saveSource(event)'>
<label>类型<select id=kind onchange=renderFields()><option value=market>股票</option><option value=x>X 账号</option><option value=youtube>YouTube 频道</option><option value=rss>RSS / Atom</option><option value=imap>邮箱 IMAP</option></select></label>
<label>配置 ID<input id=id pattern='[a-z][a-z0-9_-]{1,63}' placeholder='例如 stocks_us' required><small>稳定 ID，保存后不要随意更改。</small></label>
<label>显示名称<input id=publisher placeholder='例如 我的美股 / Bloomberg' required></label><label>栏目<input id=section value=Watchlist required></label>
<label><span><input id=enabled type=checkbox> 保存后启用</span><small>未准备好凭据时保持关闭，不会产生失败告警。</small></label>
<div id=dynamic class=wide></div>
<label class=wide>关键词<input id=keywords placeholder='逗号分隔；留空表示每条更新都通知'><small>只匹配标题和摘要。</small></label>
<label class=wide>排除词<input id=excludes placeholder='例如 podcast, sponsored'></label>
<div class='wide actions'><button>保存来源与通知规则</button><button type=button class=secondary onclick='testSource()'>测试连接</button><button type=button class=secondary onclick=resetForm()>清空</button><span id=message class=message></span></div></form>
<h2>已管理来源</h2><div id=items class=items></div>
<h2>配置修订</h2><div id=revisions class=items></div>
<details><summary>高级 JSON</summary><textarea id=advanced class='advanced wide' placeholder='完整来源或规则 JSON'></textarea><div class=actions><button onclick="saveAdvanced('sources')">保存来源</button><button onclick="saveAdvanced('rules')">保存规则</button></div></details></div></main>
<datalist id=timezoneSuggestions><option value=Asia/Shanghai><option value=Asia/Hong_Kong><option value=UTC><option value=America/New_York><option value=Europe/London></datalist>
<script>
let auth=sessionStorage.getItem('eosAdminToken')||'',reminders=[];const $=id=>document.getElementById(id);const escHtml=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const browserZone=Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC';
function headers(json=false){let h={Authorization:'Bearer '+auth};if(json)h['content-type']='application/json';return h}
async function api(path,opt={}){opt.headers={...headers(!!opt.body),...(opt.headers||{})};let r=await fetch(path,opt);let body=await r.json().catch(()=>({error:'响应无法解析'}));if(!r.ok)throw new Error(body.error||('HTTP '+r.status));return body}
async function login(e){e.preventDefault();auth=$('token').value;sessionStorage.setItem('eosAdminToken',auth);try{await refresh();$('login').hidden=true;$('app').hidden=false}catch(e){$('loginmsg').textContent=e.message}}
function logout(){sessionStorage.removeItem('eosAdminToken');location.reload()}
function localDateTimeValue(epoch){let d=new Date(epoch*1000),pad=n=>String(n).padStart(2,'0');return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+'T'+pad(d.getHours())+':'+pad(d.getMinutes())}
function renderReminderFields(){let kind=$('reminderKind').value,html='';if(kind==='once')html=`<label>发送时间<input id=reminderRunAt type=datetime-local required><small>按当前浏览器所在时区解释：${escHtml(browserZone)}</small></label>`;else if(kind==='after')html=`<div style='display:grid;grid-template-columns:2fr 1fr;gap:14px'><label>等待时长<input id=reminderDelay type=number min=1 step=1 value=30 required></label><label>单位<select id=reminderDelayUnit><option value=60>分钟</option><option value=3600>小时</option><option value=86400>天</option></select></label></div>`;else html=`<div style='display:grid;grid-template-columns:1fr 1fr;gap:14px'><label>每天时间<input id=reminderDailyTime type=time value=09:00 required></label><label>时区<input id=reminderTimezone list=timezoneSuggestions required><small>跟随该地区时间，包括夏令时。</small></label></div>`;$('reminderDynamic').innerHTML=html;if(kind==='once')$('reminderRunAt').value=localDateTimeValue(Math.floor(Date.now()/1000)+3600);if(kind==='daily')$('reminderTimezone').value=browserZone}
function buildReminder(){let kind=$('reminderKind').value,result={title:$('reminderTitle').value.trim(),message:$('reminderBody').value.trim(),schedule_kind:kind,timezone:browserZone,enabled:$('reminderEnabled').checked,priority:+$('reminderPriority').value,tags:['alarm_clock']},id=$('reminderId').value;if(id)result.id=id;if(kind==='once'){let value=Math.floor(new Date($('reminderRunAt').value).getTime()/1000);if(!Number.isFinite(value))throw new Error('请选择有效的发送时间');result.run_at=value}else if(kind==='after'){result.delay_seconds=Math.round(+$('reminderDelay').value*+$('reminderDelayUnit').value)}else{result.daily_time=$('reminderDailyTime').value;result.timezone=$('reminderTimezone').value.trim()}return result}
async function saveReminder(e){e.preventDefault();$('reminderMessage').textContent='保存中...';try{await api('/api/reminders',{method:'POST',body:JSON.stringify(buildReminder())});$('reminderMessage').textContent='已保存并立即生效。';resetReminderForm(false);await refresh()}catch(e){$('reminderMessage').textContent=e.message}}
function editReminder(id){let item=reminders.find(r=>r.id===id);if(!item)return;$('reminderId').value=item.id;$('reminderTitle').value=item.title;$('reminderBody').value=item.message;$('reminderPriority').value=String(item.priority);$('reminderEnabled').checked=!!item.enabled;$('reminderKind').value=item.schedule_kind;renderReminderFields();if(item.schedule_kind==='once')$('reminderRunAt').value=localDateTimeValue(item.run_at);else{$('reminderDailyTime').value=item.daily_time;$('reminderTimezone').value=item.timezone}$('reminderForm').scrollIntoView({behavior:'smooth',block:'start'});$('reminderMessage').textContent='正在编辑现有提醒。'}
async function toggleReminder(id,enabled){try{await api('/api/reminders/'+encodeURIComponent(id)+'/'+(enabled?'enable':'disable'),{method:'POST',body:'{}'});await refresh()}catch(e){$('reminderMessage').textContent=e.message}}
async function removeReminder(id){if(!confirm('删除这条提醒？尚未开始发送的待发消息也会取消。'))return;try{await api('/api/reminders/'+encodeURIComponent(id),{method:'DELETE'});resetReminderForm(false);await refresh()}catch(e){$('reminderMessage').textContent=e.message}}
function reminderSchedule(item){return item.schedule_kind==='daily'?'每天 '+escHtml(item.daily_time)+' · '+escHtml(item.timezone):'一次 · '+new Date(item.run_at*1000).toLocaleString()}
function reminderState(item){if(item.next_run_at)return '下次 '+new Date(item.next_run_at*1000).toLocaleString();if(item.last_delivery_status==='pending')return '等待 ntfy 投递';if(item.last_delivery_status==='sending')return '正在投递';if(item.last_delivery_status==='delivered')return '已发送';if(item.completed_at)return '已到期';return item.enabled?'已启用':'已停用'}
function renderReminders(){let active=reminders.filter(r=>r.enabled&&r.next_run_at).length;$('reminderCount').textContent=active;$('reminderItems').innerHTML=reminders.length?reminders.map(r=>{let id=JSON.stringify(r.id),canEnable=!r.enabled&&!r.completed_at;return `<div class=item><div><b>${escHtml(r.title)}</b><div class=item-message>${escHtml(r.message)}</div><code>${reminderSchedule(r)} · ${reminderState(r)} · 优先级 ${r.priority}</code></div><div class=actions><button class=secondary onclick='editReminder(${id})'>编辑</button>${r.enabled?`<button class=secondary onclick='toggleReminder(${id},false)'>停用</button>`:canEnable?`<button class=secondary onclick='toggleReminder(${id},true)'>启用</button>`:''}<button class=danger onclick='removeReminder(${id})'>删除</button></div></div>`}).join(''):'<div class=item>尚未安排提醒。</div>'}
function resetReminderForm(clearMessage=true){$('reminderForm').reset();$('reminderId').value='';$('reminderTitle').value='定时提醒';$('reminderKind').value='once';$('reminderPriority').value='3';$('reminderEnabled').checked=true;renderReminderFields();if(clearMessage)$('reminderMessage').textContent=''}
function escRegex(s){return s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')}
function words(id){return $(id).value.split(',').map(x=>x.trim()).filter(Boolean)}
function common(){return {id:$('id').value.trim(),kind:$('kind').value,publisher:$('publisher').value.trim(),section:$('section').value.trim(),dedupe_scope:$('id').value.trim(),enabled:$('enabled').checked,poll_interval_seconds:300,request_timeout_seconds:20,request_attempts:3,retry_base_seconds:2,max_response_bytes:2097152,settings:{}}}
function renderFields(){let k=$('kind').value;let h='';if(k==='market')h=`<div style='display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px'><label>股票代码<input id=target placeholder='AAPL, MSFT, 0700.HK' required></label><label>API 基址<input id=endpoint value='https://data.alpaca.markets' required></label><label>API Key 环境变量<input id=keyenv value=ALPACA_API_KEY required></label><label>API Secret 环境变量<input id=secretenv value=ALPACA_API_SECRET required></label><label>涨跌阈值 %<input id=threshold type=number min=.1 step=.1 value=5 required></label><label>成交量倍数<input id=volume type=number min=1 step=.1 value=3 required></label><label>跳空阈值 %<input id=gap type=number min=.1 step=.1 value=3 required></label><label>冷却秒数<input id=cooldown type=number min=60 value=1800 required></label></div>`;
else if(k==='x')h=`<label>X numeric user ID<input id=target inputmode=numeric pattern='[0-9]+' required><small>必须是平台数字 ID，不使用显示名匹配。</small></label><label>Bearer Token 环境变量<input id=tokenenv value=X_BEARER_TOKEN required></label>`;
else if(k==='youtube')h=`<label>YouTube channel ID<input id=target required><small>使用 UC 开头的稳定 channel ID。</small></label>`;
else if(k==='rss')h=`<label>官方 RSS / Atom URL<input id=target type=url pattern='https://.*' required><small>必须是 HTTPS；付费墙来源只保存标题、摘要和链接。</small></label>`;
else h=`<div style='display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px'><label>IMAP 主机<input id=target required></label><label>Mailbox<input id=mailbox value=INBOX required></label><label>用户名环境变量<input id=userenv value=IMAP_USERNAME required></label><label>密码环境变量<input id=passenv value=IMAP_PASSWORD required></label><label class=wide>搜索条件<input id=search value=ALL required></label></div>`;$('dynamic').innerHTML=h}
function buildSource(){let s=common(),k=s.kind,t=$('target').value.trim();if(k==='market')s.settings={api_base_url:$('endpoint').value.trim(),path_template:'/v2/stocks/{symbol}/snapshot',api_key_env:$('keyenv').value.trim(),api_secret_env:$('secretenv').value.trim(),symbols:t.split(',').map(x=>x.trim().toUpperCase()).filter(Boolean),price_change_threshold:+$('threshold').value,volume_multiplier:+$('volume').value,gap_threshold:+$('gap').value,cooldown_seconds:+$('cooldown').value};else if(k==='x')s.settings={user_id:t,bearer_token_env:$('tokenenv').value.trim()};else if(k==='youtube')s.settings={channel_id:t};else if(k==='rss'){s.url=t;s.allowed_hosts=[new URL(t).hostname]}else s.settings={host:t,port:993,mailbox:$('mailbox').value.trim(),search:$('search').value.trim(),username_env:$('userenv').value.trim(),password_env:$('passenv').value.trim()};return s}
function buildRule(s){let include=words('keywords'),exclude=words('excludes'),patterns=[];patterns.push({label:include.length?'关注关键词':'全部更新',regex:include.length?'(?i)\\b(?:'+include.map(escRegex).join('|')+')\\b':'.',title_weight:2,summary_weight:1});if(exclude.length)patterns.push({label:'排除内容',regex:'(?i)\\b(?:'+exclude.map(escRegex).join('|')+')\\b',title_weight:-100,summary_weight:-100});return {id:s.id+'_notify',kind:'weighted_text',source_ids:[s.id],threshold:1,max_item_age_seconds:86400,notification_title:s.kind==='market'?'股票异动':s.publisher+' 更新',priority:s.kind==='market'?4:3,tags:s.kind==='market'?['chart_with_upwards_trend']:['bell'],patterns}}
async function saveSource(e){e.preventDefault();$('message').textContent='保存中...';try{let s=buildSource();await api('/api/validate',{method:'POST',body:JSON.stringify({sources:[s],rules:[buildRule(s)]})});await api('/api/sources',{method:'POST',body:JSON.stringify(s)});await api('/api/rules',{method:'POST',body:JSON.stringify(buildRule(s))});$('message').textContent='已保存；重启 argus 后生效。';await refresh()}catch(e){$('message').textContent=e.message}}
async function testSource(){try{$('message').textContent='连接测试中...';let s=buildSource();let result=await api('/api/test-source',{method:'POST',body:JSON.stringify({source:s})});$('message').textContent='测试成功：'+result.observations+' 条新记录，耗时 '+result.elapsed_ms+' ms'}catch(e){$('message').textContent='测试失败：'+e.message}}
async function toggleSource(id,enabled){try{await api('/api/sources/'+encodeURIComponent(id)+'/'+(enabled?'enable':'disable'),{method:'POST',body:'{}'});await refresh()}catch(e){$('message').textContent=e.message}}
async function removeSource(id){if(!confirm('删除 '+id+' 及其通知规则？'))return;await api('/api/rules/'+encodeURIComponent(id+'_notify'),{method:'DELETE'});await api('/api/sources/'+encodeURIComponent(id),{method:'DELETE'});await refresh()}
async function rollback(id){if(!confirm('回滚到修订 '+id+'？'))return;try{await api('/api/revisions/'+id+'/rollback',{method:'POST',body:'{}'});$('message').textContent='已回滚；重启 argus 后生效。';await refresh()}catch(e){$('message').textContent=e.message}}
async function saveAdvanced(kind){try{let v=JSON.parse($('advanced').value);await api('/api/'+kind,{method:'POST',body:JSON.stringify(v)});await refresh()}catch(e){$('message').textContent=e.message}}
async function refresh(){let [d,rr,rv]=await Promise.all([api('/api/config'),api('/api/reminders'),api('/api/revisions')]);let src=d.managed.sources||[];reminders=rr.reminders||[];renderReminders();$('sourceCount').textContent=src.length;$('observationCount').textContent=d.status.observations||0;$('outboxCount').textContent=(d.status.outbox||{}).pending||0;$('incidentCount').textContent=(d.status.incidents||{}).open||0;$('items').innerHTML=src.length?src.map(s=>{let id=JSON.stringify(String(s.id));return `<div class=item><div><b>${escHtml(s.publisher||s.id)}</b><code>${escHtml(s.kind)} · ${escHtml(s.id)} · ${s.enabled===false?'停用':'启用'}</code></div><div class=actions><button class=secondary onclick='toggleSource(${id},${s.enabled===false})'>${s.enabled===false?'启用':'停用'}</button><button class=danger onclick='removeSource(${id})'>删除</button></div></div>`}).join(''):'<div class=item>尚未添加托管来源；Bloomberg 基础来源仍在主配置中运行。</div>';$('revisions').innerHTML=(rv.revisions||[]).length?rv.revisions.map(r=>`<div class=item><div><b>修订 ${r.revision}${r.active?' · 当前':''}</b><code>${escHtml(r.reason||'')} · ${new Date((r.created_at||0)*1000).toLocaleString()}</code></div>${r.active?'':'<button class=secondary onclick="rollback('+r.revision+')">回滚</button>'}</div>`).join(''):'<div class=item>暂无修订记录。</div>'}
function resetForm(){$('sourceForm').reset();renderFields()}renderFields();renderReminderFields();if(auth){refresh().then(()=>{$('login').hidden=true;$('app').hidden=false}).catch(()=>{})}
</script></html>"""


_WEB_ROOT = Path(__file__).with_name("admin_web")
_STATIC_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json",
}


def _static_file(request_path: str) -> Path | None:
    path = urllib.parse.urlsplit(request_path).path
    relative = "index.html" if path in {"/", "/index.html"} else path.lstrip("/")
    if not relative or relative.startswith("."):
        return None
    candidate = (_WEB_ROOT / relative).resolve()
    try:
        candidate.relative_to(_WEB_ROOT.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def make_handler(store: ManagedConfigStore, database: Database, auth_token: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ArgusAdmin/0.6"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _authorized(self) -> bool:
            if not auth_token:
                return True
            supplied = self.headers.get("Authorization", "")
            return secrets.compare_digest(supplied, f"Bearer {auth_token}")

        def _actor(self) -> str:
            value = self.headers.get("X-Argus-Actor", "admin").strip()
            return value[:128] or "admin"

        def _body(self) -> Mapping[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 256 * 1024:
                raise AdminError("request body size is invalid")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, Mapping):
                raise AdminError("request body must be an object")
            return data

        def _json(self, status: int, payload: Any) -> None:
            encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)

        def _static(self, path: Path) -> None:
            encoded = path.read_bytes()
            content_type = _STATIC_CONTENT_TYPES.get(path.suffix.lower())
            if content_type is None:
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header(
                "Cache-Control",
                "no-store" if path.name == "index.html" else "public, max-age=31536000, immutable",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; "
                "img-src 'self' data:; font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; manifest-src 'self'",
            )
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802
            static = _static_file(self.path)
            if static is not None:
                self._static(static)
                return
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            if self.path == "/api/config":
                self._json(HTTPStatus.OK, {"managed": _public(store.read()), "revision": store.metadata(), "status": database.status()})
                return
            if self.path in {"/api/health", "/health"}:
                self._json(HTTPStatus.OK, {"ok": True})
                return
            if self.path in {"/api/metrics", "/metrics"}:
                payload = database.metrics_prometheus().encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path == "/api/revisions":
                self._json(HTTPStatus.OK, {"revisions": database.list_config_revisions()})
                return
            if self.path.startswith("/api/revisions/"):
                try:
                    revision = int(self.path.rsplit("/", 1)[-1])
                except ValueError:
                    revision = -1
                item = database.get_config_revision(revision)
                if item is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "revision not found"})
                else:
                    self._json(HTTPStatus.OK, _public(item))
                return
            if self.path == "/api/incidents":
                self._json(HTTPStatus.OK, {"incidents": database.list_incidents()})
                return
            if self.path == "/api/reminders":
                self._json(HTTPStatus.OK, {"reminders": database.list_reminders(), "server_time": int(time.time())})
                return
            if self.path.startswith("/api/reminders/"):
                identifier = urllib.parse.unquote(self.path[len("/api/reminders/"):].strip())
                item = database.get_reminder(identifier)
                if item is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "reminder not found"})
                else:
                    self._json(HTTPStatus.OK, {"reminder": item, "server_time": int(time.time())})
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            try:
                data = self._body()
                actor = self._actor()
                if self.path == "/api/reminders":
                    now = int(time.time())
                    reminder = parse_reminder(data, now)
                    saved = database.upsert_reminder(reminder, actor, now)
                    self._json(HTTPStatus.OK, {"saved": saved, "restart_required": False})
                    return
                if self.path.startswith("/api/reminders/") and self.path.endswith(("/enable", "/disable")):
                    parts = self.path.strip("/").split("/")
                    identifier = urllib.parse.unquote(parts[2])
                    enabled = self.path.endswith("/enable")
                    saved = database.set_reminder_enabled(identifier, enabled, actor, int(time.time()))
                    self._json(HTTPStatus.OK, {"saved": saved, "restart_required": False})
                    return
                if self.path == "/api/source-bundles":
                    raw_source = data.get("source")
                    raw_rule = data.get("rule")
                    if not isinstance(raw_source, Mapping):
                        raise AdminError("source bundle requires a source object")
                    if raw_rule is not None and not isinstance(raw_rule, Mapping):
                        raise AdminError("source bundle rule must be an object")
                    source, rule, revision = store.upsert_bundle(
                        raw_source,
                        raw_rule,
                        actor=actor,
                    )
                    database.record_config_revision(
                        revision,
                        store.read(),
                        actor,
                        f"upsert source bundle {source['id']}",
                    )
                    self._json(
                        HTTPStatus.OK,
                        {
                            "saved": {"source": _public(source), "rule": _public(rule)},
                            "revision": revision,
                            "restart_required": True,
                        },
                    )
                    return
                if self.path in {"/api/sources", "/api/rules"}:
                    kind = "source" if self.path.endswith("sources") else "rule"
                    item = store.upsert(kind, data)
                    revision = store.revision
                    database.record_config_revision(revision, store.read(), actor, f"upsert {kind} {item['id']}")
                    self._json(HTTPStatus.OK, {"saved": _public(item), "revision": revision, "restart_required": True})
                    return
                if self.path == "/api/validate":
                    result = store.validate(data)
                    self._json(HTTPStatus.OK, {"valid": True, "summary": result})
                    return
                if self.path == "/api/test-source":
                    raw_source = dict(data.get("source", data))
                    raw_source["enabled"] = True
                    parsed = _parse_source(raw_source, 0)
                    collector = build_collector(parsed)
                    if collector is None:
                        raise AdminError("source is disabled")
                    started = time.monotonic()
                    result = collector.fetch(SourceState(parsed.id, False, None, None, None, None, 0, False, None))
                    elapsed = int((time.monotonic() - started) * 1000)
                    self._json(HTTPStatus.OK, {"ok": True, "elapsed_ms": elapsed, "observations": len(result.observations), "not_modified": result.not_modified})
                    return
                if self.path.startswith("/api/sources/") and self.path.endswith(("/enable", "/disable")):
                    parts = self.path.strip("/").split("/")
                    identifier = urllib.parse.unquote(parts[2])
                    current = store.read()
                    source = next((row for row in current["sources"] if row.get("id") == identifier), None)
                    if source is None:
                        raise AdminError("source not found")
                    source = dict(source)
                    source["enabled"] = self.path.endswith("/enable")
                    item = store.upsert("source", source)
                    revision = store.revision
                    database.record_config_revision(revision, store.read(), actor, f"{'enable' if source['enabled'] else 'disable'} source {identifier}")
                    self._json(HTTPStatus.OK, {"saved": _public(item), "revision": revision, "restart_required": True})
                    return
                if self.path.startswith("/api/revisions/") and self.path.endswith(("/rollback", "/activate")):
                    parts = self.path.strip("/").split("/")
                    target = int(parts[2])
                    new_revision = store.rollback(target, actor=actor)
                    database.record_config_revision(new_revision, store.read(), actor, f"rollback to revision {target}")
                    self._json(HTTPStatus.OK, {"revision": new_revision, "rolled_back_from": target, "restart_required": True})
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except (AdminError, ConfigError, ReminderError, ValueError, TypeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except (AdapterError, OSError, RuntimeError) as exc:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            if self.path.startswith("/api/reminders/"):
                identifier = urllib.parse.unquote(self.path[len("/api/reminders/"):].strip())
                removed = database.delete_reminder(identifier, self._actor(), int(time.time()))
                self._json(HTTPStatus.OK, {"removed": removed, "restart_required": False})
                return
            if self.path.startswith("/api/source-bundles/"):
                identifier = urllib.parse.unquote(
                    self.path[len("/api/source-bundles/"):].strip()
                )
                try:
                    removed, removed_rules, revision = store.delete_bundle(
                        identifier,
                        actor=self._actor(),
                    )
                    if removed:
                        database.record_config_revision(
                            revision,
                            store.read(),
                            self._actor(),
                            f"delete source bundle {identifier}",
                        )
                    self._json(
                        HTTPStatus.OK,
                        {
                            "removed": removed,
                            "removed_rules": removed_rules,
                            "revision": revision,
                            "restart_required": removed,
                        },
                    )
                except AdminError as exc:
                    self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            prefix = "/api/sources/" if self.path.startswith("/api/sources/") else "/api/rules/"
            if not self.path.startswith(prefix):
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            identifier = urllib.parse.unquote(self.path[len(prefix):].strip())
            try:
                removed = store.delete("source" if prefix.endswith("sources/") else "rule", identifier)
                if removed:
                    revision = store.revision
                    database.record_config_revision(revision, store.read(), self._actor(), f"delete {identifier}")
                self._json(HTTPStatus.OK, {"removed": removed, "revision": store.revision, "restart_required": removed})
            except AdminError as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})

    return Handler


def serve(config: AdminConfig, store: ManagedConfigStore, database: Database, environment: Mapping[str, str] | None = None) -> None:
    env = os.environ if environment is None else environment
    token = env.get(config.auth_token_env, "") if config.auth_token_env else None
    if config.auth_token_env and not token:
        raise AdminError(f"required environment variable {config.auth_token_env} is missing")
    # A single-threaded server keeps the SQLite connection in its owner thread.
    # Requests are local management calls and intentionally bounded in size.
    server = HTTPServer((config.bind, config.port), make_handler(store, database, token))
    server.serve_forever()


def serve_in_thread(config: AdminConfig, store: ManagedConfigStore, database: Database) -> Thread:
    thread = Thread(target=serve, args=(config, store, database), daemon=True, name="argus-admin")
    thread.start()
    return thread
