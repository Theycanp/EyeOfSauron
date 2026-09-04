from __future__ import annotations

import json
import os
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, Mapping

from .config import AdminConfig, ConfigError, _parse_rule, _parse_source
from .database import Database


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

    def write(self, data: Mapping[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)

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
<div id=app hidden><section class=status><div class=metric><b id=sourceCount>0</b><span>托管来源</span></div><div class=metric><b id=observationCount>0</b><span>观察记录</span></div><div class=metric><b id=outboxCount>0</b><span>待投递</span></div></section>
<h2>新增或更新监测</h2><form id=sourceForm onsubmit='saveSource(event)'>
<label>类型<select id=kind onchange=renderFields()><option value=market>股票</option><option value=x>X 账号</option><option value=youtube>YouTube 频道</option><option value=rss>RSS / Atom</option><option value=imap>邮箱 IMAP</option></select></label>
<label>配置 ID<input id=id pattern='[a-z][a-z0-9_-]{1,63}' placeholder='例如 stocks_us' required><small>稳定 ID，保存后不要随意更改。</small></label>
<label>显示名称<input id=publisher placeholder='例如 我的美股 / Bloomberg' required></label><label>栏目<input id=section value=Watchlist required></label>
<label><span><input id=enabled type=checkbox> 保存后启用</span><small>未准备好凭据时保持关闭，不会产生失败告警。</small></label>
<div id=dynamic class=wide></div>
<label class=wide>关键词<input id=keywords placeholder='逗号分隔；留空表示每条更新都通知'><small>只匹配标题和摘要。</small></label>
<label class=wide>排除词<input id=excludes placeholder='例如 podcast, sponsored'></label>
<div class='wide actions'><button>保存来源与通知规则</button><button type=button class=secondary onclick=resetForm()>清空</button><span id=message class=message></span></div></form>
<h2>已管理来源</h2><div id=items class=items></div>
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
async function saveSource(e){e.preventDefault();$('message').textContent='保存中...';try{let s=buildSource();await api('/api/sources',{method:'POST',body:JSON.stringify(s)});await api('/api/rules',{method:'POST',body:JSON.stringify(buildRule(s))});$('message').textContent='已保存；重启 signalwatch 后生效。';await refresh()}catch(e){$('message').textContent=e.message}}
async function removeSource(id){if(!confirm('删除 '+id+' 及其通知规则？'))return;await api('/api/rules/'+encodeURIComponent(id+'_notify'),{method:'DELETE'});await api('/api/sources/'+encodeURIComponent(id),{method:'DELETE'});await refresh()}
async function saveAdvanced(kind){try{let v=JSON.parse($('advanced').value);await api('/api/'+kind,{method:'POST',body:JSON.stringify(v)});await refresh()}catch(e){$('message').textContent=e.message}}
async function refresh(){let d=await api('/api/config');let src=d.managed.sources||[];$('sourceCount').textContent=src.length;$('observationCount').textContent=d.status.observations||0;$('outboxCount').textContent=(d.status.outbox||{}).pending||0;$('items').innerHTML=src.length?src.map(s=>`<div class=item><div><b>${escHtml(s.publisher||s.id)}</b><code>${escHtml(s.kind)} · ${escHtml(s.id)} · ${s.enabled===false?'停用':'启用'}</code></div><button class=danger onclick="removeSource('${s.id}')">删除</button></div>`).join(''):'<div class=item>尚未添加托管来源；Bloomberg 基础来源仍在主配置中运行。</div>'}
function resetForm(){$('sourceForm').reset();renderFields()}renderFields();if(auth){refresh().then(()=>{$('login').hidden=true;$('app').hidden=false}).catch(()=>{})}
</script></html>"""


def make_handler(store: ManagedConfigStore, database: Database, auth_token: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SignalWatchAdmin/0.2"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _authorized(self) -> bool:
            if not auth_token:
                return True
            supplied = self.headers.get("Authorization", "")
            return secrets.compare_digest(supplied, f"Bearer {auth_token}")

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
                self._json(HTTPStatus.OK, {"managed": _public(store.read()), "status": database.status()})
                return
            if self.path == "/api/health":
                self._json(HTTPStatus.OK, {"ok": True})
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            if self.path not in {"/api/sources", "/api/rules"}:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 256 * 1024:
                    raise AdminError("request body size is invalid")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, Mapping):
                    raise AdminError("request body must be an object")
                item = store.upsert("source" if self.path.endswith("sources") else "rule", data)
                self._json(HTTPStatus.OK, {"saved": _public(item), "restart_required": True})
            except (AdminError, ConfigError, ValueError, TypeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def do_DELETE(self) -> None:  # noqa: N802
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            prefix = "/api/sources/" if self.path.startswith("/api/sources/") else "/api/rules/"
            if not self.path.startswith(prefix):
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            identifier = self.path[len(prefix):].strip()
            try:
                removed = store.delete("source" if prefix.endswith("sources/") else "rule", identifier)
                self._json(HTTPStatus.OK, {"removed": removed, "restart_required": removed})
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
