from __future__ import annotations

import json
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
<meta name=viewport content='width=device-width,initial-scale=1'><title>SignalWatch 管理</title>
<style>
:root{color-scheme:light;--ink:#18201d;--muted:#65706b;--line:#d8dfdb;--bg:#f5f7f6;--panel:#fff;--green:#176b4a;--red:#a33b31}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,sans-serif;letter-spacing:0}
header{background:#15231e;color:#fff;padding:18px 24px}.bar,main{max-width:1080px;margin:auto}.bar{display:flex;align-items:center;justify-content:space-between}.brand{font-size:20px;font-weight:700}.sub{color:#b9c8c1;font-size:13px}
main{padding:24px}.status{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--line);border:1px solid var(--line);margin-bottom:24px}.metric{background:var(--panel);padding:14px}.metric b{display:block;font-size:22px}.metric span,label small{color:var(--muted)}
h2{font-size:18px;margin:28px 0 12px}form{background:var(--panel);border:1px solid var(--line);padding:18px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.wide{grid-column:1/-1}label{display:grid;gap:5px;font-weight:600}input,select,textarea{width:100%;border:1px solid #b9c3be;background:#fff;padding:9px 10px;font:inherit;color:inherit;border-radius:4px}textarea{min-height:82px;resize:vertical}button{border:0;border-radius:4px;padding:9px 13px;background:var(--green);color:#fff;font:600 14px system-ui;cursor:pointer}button.secondary{background:#e4e9e6;color:var(--ink)}button.danger{background:transparent;color:var(--red);border:1px solid #d9aaa5}.actions{display:flex;gap:8px;align-items:center}.message{min-height:22px;color:var(--muted)}
.items{display:grid;gap:8px}.item{background:var(--panel);border:1px solid var(--line);padding:14px;display:grid;grid-template-columns:1fr auto;gap:10px;align-items:center;border-radius:6px}.item b{display:block}.item code{color:var(--muted)}
#login{max-width:520px;margin:56px auto}#app[hidden]{display:none}.advanced{font-family:ui-monospace,monospace;font-size:13px}
@media(max-width:680px){main{padding:16px}.status{grid-template-columns:1fr}form{grid-template-columns:1fr}.wide{grid-column:auto}.item{grid-template-columns:1fr}}
</style>
<header><div class=bar><div><div class=brand>SignalWatch</div><div class=sub>本机监测源与通知规则</div></div><button class=secondary onclick=logout()>锁定</button></div></header>
<main><section id=login><h2>管理认证</h2><form onsubmit='login(event)'><label class=wide>管理 Token<input id=token type=password autocomplete=current-password required><small>只保存在当前浏览器标签页。</small></label><button>解锁后台</button><div id=loginmsg class=message></div></form></section>
<div id=app hidden><section class=status><div class=metric><b id=sourceCount>0</b><span>托管来源</span></div><div class=metric><b id=observationCount>0</b><span>观察记录</span></div><div class=metric><b id=outboxCount>0</b><span>待投递</span></div><div class=metric><b id=incidentCount>0</b><span>进行中事件</span></div></section>
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
<script>
let auth=sessionStorage.getItem('signalwatchAdminToken')||'';const $=id=>document.getElementById(id);const escHtml=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function headers(json=false){let h={Authorization:'Bearer '+auth};if(json)h['content-type']='application/json';return h}
async function api(path,opt={}){opt.headers={...headers(!!opt.body),...(opt.headers||{})};let r=await fetch(path,opt);let body=await r.json().catch(()=>({error:'响应无法解析'}));if(!r.ok)throw new Error(body.error||('HTTP '+r.status));return body}
async function login(e){e.preventDefault();auth=$('token').value;sessionStorage.setItem('signalwatchAdminToken',auth);try{await refresh();$('login').hidden=true;$('app').hidden=false}catch(e){$('loginmsg').textContent=e.message}}
function logout(){sessionStorage.removeItem('signalwatchAdminToken');location.reload()}
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
async function saveSource(e){e.preventDefault();$('message').textContent='保存中...';try{let s=buildSource();await api('/api/validate',{method:'POST',body:JSON.stringify({sources:[s],rules:[buildRule(s)]})});await api('/api/sources',{method:'POST',body:JSON.stringify(s)});await api('/api/rules',{method:'POST',body:JSON.stringify(buildRule(s))});$('message').textContent='已保存；重启 signalwatch 后生效。';await refresh()}catch(e){$('message').textContent=e.message}}
async function testSource(){try{$('message').textContent='连接测试中...';let s=buildSource();let result=await api('/api/test-source',{method:'POST',body:JSON.stringify({source:s})});$('message').textContent='测试成功：'+result.observations+' 条新记录，耗时 '+result.elapsed_ms+' ms'}catch(e){$('message').textContent='测试失败：'+e.message}}
async function toggleSource(id,enabled){try{await api('/api/sources/'+encodeURIComponent(id)+'/'+(enabled?'enable':'disable'),{method:'POST',body:'{}'});await refresh()}catch(e){$('message').textContent=e.message}}
async function removeSource(id){if(!confirm('删除 '+id+' 及其通知规则？'))return;await api('/api/rules/'+encodeURIComponent(id+'_notify'),{method:'DELETE'});await api('/api/sources/'+encodeURIComponent(id),{method:'DELETE'});await refresh()}
async function rollback(id){if(!confirm('回滚到修订 '+id+'？'))return;try{await api('/api/revisions/'+id+'/rollback',{method:'POST',body:'{}'});$('message').textContent='已回滚；重启 signalwatch 后生效。';await refresh()}catch(e){$('message').textContent=e.message}}
async function saveAdvanced(kind){try{let v=JSON.parse($('advanced').value);await api('/api/'+kind,{method:'POST',body:JSON.stringify(v)});await refresh()}catch(e){$('message').textContent=e.message}}
async function refresh(){let d=await api('/api/config');let src=d.managed.sources||[];$('sourceCount').textContent=src.length;$('observationCount').textContent=d.status.observations||0;$('outboxCount').textContent=(d.status.outbox||{}).pending||0;$('incidentCount').textContent=(d.status.incidents||{}).open||0;$('items').innerHTML=src.length?src.map(s=>{let id=JSON.stringify(String(s.id));return `<div class=item><div><b>${escHtml(s.publisher||s.id)}</b><code>${escHtml(s.kind)} · ${escHtml(s.id)} · ${s.enabled===false?'停用':'启用'}</code></div><div class=actions><button class=secondary onclick='toggleSource(${id},${s.enabled===false})'>${s.enabled===false?'启用':'停用'}</button><button class=danger onclick='removeSource(${id})'>删除</button></div></div>`}).join(''):'<div class=item>尚未添加托管来源；Bloomberg 基础来源仍在主配置中运行。</div>';let rv=await api('/api/revisions');$('revisions').innerHTML=(rv.revisions||[]).length?rv.revisions.map(r=>`<div class=item><div><b>修订 ${r.revision}${r.active?' · 当前':''}</b><code>${escHtml(r.reason||'')} · ${new Date((r.created_at||0)*1000).toLocaleString()}</code></div>${r.active?'':'<button class=secondary onclick="rollback('+r.revision+')">回滚</button>'}</div>`).join(''):'<div class=item>暂无修订记录。</div>'}
function resetForm(){$('sourceForm').reset();renderFields()}renderFields();if(auth){refresh().then(()=>{$('login').hidden=true;$('app').hidden=false}).catch(()=>{})}
</script></html>"""


def make_handler(store: ManagedConfigStore, database: Database, auth_token: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SignalWatchAdmin/0.3"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _authorized(self) -> bool:
            if not auth_token:
                return True
            supplied = self.headers.get("Authorization", "")
            return secrets.compare_digest(supplied, f"Bearer {auth_token}")

        def _actor(self) -> str:
            value = self.headers.get("X-SignalWatch-Actor", "admin").strip()
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

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/" or self.path == "/index.html":
                encoded = _INDEX_HTML.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
                self.end_headers()
                self.wfile.write(encoded)
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
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            try:
                data = self._body()
                actor = self._actor()
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
            except (AdminError, ConfigError, ValueError, TypeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except (AdapterError, OSError, RuntimeError) as exc:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
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
    thread = Thread(target=serve, args=(config, store, database), daemon=True, name="signalwatch-admin")
    thread.start()
    return thread
