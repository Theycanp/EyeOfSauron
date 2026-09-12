from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import secrets
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Mapping

from . import __version__
from .auth import AdminAuth, AuthContext, AuthError, LoginBlockedError
from .config import AdminConfig, ConfigError, _parse_analysis, _parse_digest, _parse_rule, _parse_source
from .digest import DigestDocument
from .manual_events import ManualEventError, parse_manual_event
from .news_catalog import NEWS_SOURCE_CATALOG
from .persistence import ControlPlaneRepository, ManagedConfigRepository, RevisionConflictError
from .providers import DEFAULT_PROVIDER_REGISTRY, ProviderRegistry
from .reminders import ReminderError, parse_reminder
from .util import sanitize_error


LOGGER = logging.getLogger("argus.admin")


class AdminError(ValueError):
    pass


_SENSITIVE_KEY = re.compile(r"(?:^|_)(?:token|password|secret|key|api_?key|apikey)(?:$|_)")


def _is_sensitive_key(value: object) -> bool:
    name = str(value).lower()
    return not name.endswith("_env") and _SENSITIVE_KEY.search(name) is not None


def _reject_secret_values(value: Any, location: str = "config") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _is_sensitive_key(key):
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
            if not _is_sensitive_key(key)
        }
    if isinstance(value, (list, tuple)):
        return [_public(item) for item in value]
    return value


class ManagedConfigStore:
    def __init__(
        self,
        path: Path,
        base_source_ids: set[str] | None = None,
        base_rule_ids: set[str] | None = None,
        database: ManagedConfigRepository | None = None,
        provider_registry: ProviderRegistry = DEFAULT_PROVIDER_REGISTRY,
    ) -> None:
        self.path = path
        self.base_source_ids = base_source_ids or set()
        self.base_rule_ids = base_rule_ids or set()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.database = database
        self.provider_registry = provider_registry
        self.history_path = self.path.with_name(self.path.name + ".revisions")
        self.history_path.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.history_path, 0o700)
        except OSError:
            pass

        self._bootstrap_database()

    def _read_file_envelope(self) -> Mapping[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AdminError(f"cannot read managed configuration: {exc}") from exc
        if not isinstance(data, Mapping):
            raise AdminError("managed configuration must be an object")
        return data

    def _bootstrap_database(self) -> None:
        if self.database is None or self.database.get_active_config_revision() is not None:
            return
        envelope = self._read_file_envelope()
        if envelope is None:
            return
        payload = {
            "sources": list(envelope.get("sources", [])),
            "rules": list(envelope.get("rules", [])),
        }
        if isinstance(envelope.get("analysis"), Mapping):
            payload["analysis"] = dict(envelope["analysis"])
        if isinstance(envelope.get("digest"), Mapping):
            payload["digest"] = dict(envelope["digest"])
        self.validate(payload)
        revision = max(1, int(envelope.get("revision", 1)))
        self.database.record_config_revision(
            revision,
            payload,
            str(envelope.get("updated_by", "migration")),
            "import legacy managed configuration",
            only_if_empty=True,
        )

    def _materialize(
        self,
        data: Mapping[str, Any],
        revision: int,
        actor: str,
        reason: str,
    ) -> None:
        if self.database is not None:
            return
        envelope = {
            "format_version": 2,
            "revision": revision,
            "updated_at": int(time.time()),
            "updated_by": actor[:128],
            "reason": reason[:512],
            "sources": list(data["sources"]),
            "rules": list(data["rules"]),
        }
        if isinstance(data.get("analysis"), Mapping):
            envelope["analysis"] = dict(data["analysis"])
        if isinstance(data.get("digest"), Mapping):
            envelope["digest"] = dict(data["digest"])
        payload = json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        snapshot = self.history_path / f"{revision:08d}.json"
        snapshot.write_text(payload, encoding="utf-8")
        os.chmod(snapshot, 0o600)
    def read(self) -> dict[str, Any]:
        if self.database is not None:
            active = self.database.get_active_config_revision()
            data = active.get("payload", {}) if active is not None else {}
        else:
            data = self._read_file_envelope() or {}
        result: dict[str, Any] = {
            "sources": list(data.get("sources", [])) if isinstance(data.get("sources", []), list) else [],
            "rules": list(data.get("rules", [])) if isinstance(data.get("rules", []), list) else [],
        }
        if isinstance(data.get("analysis"), Mapping):
            result["analysis"] = _public(dict(data["analysis"]))
        if isinstance(data.get("digest"), Mapping):
            result["digest"] = _public(dict(data["digest"]))
        return result

    def metadata(self) -> dict[str, Any]:
        if self.database is not None:
            active = self.database.get_active_config_revision()
            if active is None:
                return {"revision": 0, "updated_at": None, "updated_by": None, "reason": None}
            return {
                "revision": int(active["revision"]),
                "updated_at": active.get("created_at"),
                "updated_by": active.get("actor"),
                "reason": active.get("reason"),
            }
        data = self._read_file_envelope()
        if data is None:
            return {"revision": 0, "updated_at": None, "updated_by": None, "reason": None}
        return {
            "revision": int(data.get("revision", 0)),
            "updated_at": data.get("updated_at"),
            "updated_by": data.get("updated_by"),
            "reason": data.get("reason"),
        }

    @property
    def revision(self) -> int:
        return int(self.metadata()["revision"])

    def validate(self, data: Mapping[str, Any]) -> dict[str, Any]:
        _reject_secret_values(data)
        sources = data.get("sources", [])
        rules = data.get("rules", [])
        if not isinstance(sources, list) or not isinstance(rules, list):
            raise AdminError("managed configuration sources and rules must be arrays")
        if "analysis" in data:
            try:
                _parse_analysis(data["analysis"])
            except (ConfigError, TypeError) as exc:
                raise AdminError(str(exc)) from exc
        if "digest" in data:
            try:
                _parse_digest(data["digest"])
            except (ConfigError, TypeError) as exc:
                raise AdminError(str(exc)) from exc
        parsed_sources = tuple(
            _parse_source(item, index, provider_registry=self.provider_registry)
            for index, item in enumerate(sources)
        )
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

    def _snapshot(self) -> tuple[dict[str, Any], int]:
        if self.database is not None:
            active = self.database.get_active_config_revision()
            payload = active["payload"] if active is not None else {}
            revision = int(active["revision"]) if active is not None else 0
        else:
            payload = self._read_file_envelope() or {}
            revision = int(payload.get("revision", 0))
        result: dict[str, Any] = {
            "sources": list(payload.get("sources", [])),
            "rules": list(payload.get("rules", [])),
        }
        if isinstance(payload.get("analysis"), Mapping):
            result["analysis"] = dict(payload["analysis"])
        if isinstance(payload.get("digest"), Mapping):
            result["digest"] = dict(payload["digest"])
        return result, revision

    def write(
        self,
        data: Mapping[str, Any],
        actor: str = "admin",
        reason: str = "configuration update",
        *,
        expected_revision: int | None = None,
    ) -> int:
        self.validate(data)
        current_revision = self.revision
        expected = current_revision if expected_revision is None else expected_revision
        if self.database is not None:
            revision = self.database.save_managed_config(
                data, actor, reason, expected_revision=expected
            )
        else:
            if expected != current_revision:
                raise RevisionConflictError(
                    f"configuration changed: expected revision {expected}, current revision {current_revision}"
                )
            revision = current_revision + 1
        try:
            self._materialize(data, revision, actor, reason)
        except OSError as exc:
            LOGGER.warning("managed_config_materialize_failed revision=%d error=%s", revision, exc)
        return revision

    def set_analysis(
        self,
        value: Mapping[str, Any],
        actor: str = "admin",
        *,
        expected_revision: int | None = None,
    ) -> int:
        """Replace only the analysis policy without overwriting sources or rules."""
        _reject_secret_values(value, "analysis")
        try:
            _parse_analysis(value)
        except (ConfigError, TypeError) as exc:
            raise AdminError(str(exc)) from exc
        current, revision = self._snapshot()
        current["analysis"] = dict(value)
        return self.write(
            current,
            actor,
            "analysis policy update",
            expected_revision=revision if expected_revision is None else expected_revision,
        )

    def set_digest(
        self,
        value: Mapping[str, Any],
        actor: str = "admin",
        *,
        expected_revision: int | None = None,
    ) -> int:
        _reject_secret_values(value, "digest")
        try:
            _parse_digest(value)
        except (ConfigError, TypeError) as exc:
            raise AdminError(str(exc)) from exc
        current, revision = self._snapshot()
        current["digest"] = dict(value)
        return self.write(
            current,
            actor,
            "digest policy update",
            expected_revision=revision if expected_revision is None else expected_revision,
        )

    def upsert(
        self,
        kind: str,
        value: Mapping[str, Any],
        actor: str = "admin",
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        _reject_secret_values(value)
        index = "sources" if kind == "source" else "rules"
        parsed = (
            _parse_source(value, 0, provider_registry=self.provider_registry)
            if kind == "source"
            else _parse_rule(value, 0)
        )
        reserved = self.base_source_ids if kind == "source" else self.base_rule_ids
        if parsed.id in reserved:
            raise AdminError(f"{parsed.id} is defined in the base configuration and cannot be overwritten here")
        item = dict(value)
        item["id"] = parsed.id
        data, base_revision = self._snapshot()
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
        self.write(
            data,
            actor=actor,
            reason=f"upsert {kind} {parsed.id}",
            expected_revision=(base_revision if expected_revision is None else expected_revision),
        )
        return item

    def upsert_bundle(
        self,
        source_value: Mapping[str, Any],
        rule_value: Mapping[str, Any] | None = None,
        actor: str = "admin",
        *,
        expected_revision: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, int]:
        """Save a source and its optional notification rule in one revision."""
        _reject_secret_values(source_value, "source")
        parsed_source = _parse_source(
            source_value, 0, provider_registry=self.provider_registry
        )
        if parsed_source.id in self.base_source_ids:
            raise AdminError(
                f"{parsed_source.id} is defined in the base configuration and cannot be overwritten here"
            )
        source = dict(source_value)
        source["id"] = parsed_source.id
        data, base_revision = self._snapshot()
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
            expected_revision=(base_revision if expected_revision is None else expected_revision),
        )
        return source, rule, revision

    def delete_bundle(
        self,
        identifier: str,
        actor: str = "admin",
        *,
        expected_revision: int | None = None,
    ) -> tuple[bool, list[str], int]:
        """Delete a managed source and every managed rule that references it."""
        data, base_revision = self._snapshot()
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
            expected_revision=(base_revision if expected_revision is None else expected_revision),
        )
        return True, removed_rules, revision

    def delete(
        self,
        kind: str,
        identifier: str,
        actor: str = "admin",
        *,
        expected_revision: int | None = None,
    ) -> bool:
        index = "sources" if kind == "source" else "rules"
        data, base_revision = self._snapshot()
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
        self.write(
            data,
            actor=actor,
            reason=f"delete {kind} {identifier}",
            expected_revision=(base_revision if expected_revision is None else expected_revision),
        )
        return True

    def read_revision(self, revision: int) -> dict[str, list[dict[str, Any]]]:
        if revision == self.revision:
            return self.read()
        if self.database is not None:
            item = self.database.get_config_revision(revision)
            if item is None:
                raise AdminError(f"configuration revision {revision} does not exist")
            data = item.get("payload", {})
            if not isinstance(data, Mapping):
                raise AdminError("configuration revision payload is invalid")
            result = {"sources": data.get("sources", []), "rules": data.get("rules", [])}
            self.validate(result)
            return result
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

    def rollback(
        self,
        revision: int,
        actor: str = "admin",
        *,
        expected_revision: int | None = None,
    ) -> int:
        base_revision = self.revision
        data = self.read_revision(revision)
        return self.write(
            data,
            actor=actor,
            reason=f"rollback to revision {revision}",
            expected_revision=(base_revision if expected_revision is None else expected_revision),
        )


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
    is_spa_route = any(
        path == root or path.startswith(f"{root}/") for root in ("/digests", "/events")
    )
    relative = (
        "index.html" if path in {"/", "/index.html"} or is_spa_route else path.lstrip("/")
    )
    if not relative or relative.startswith("."):
        return None
    candidate = (_WEB_ROOT / relative).resolve()
    try:
        candidate.relative_to(_WEB_ROOT.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _digest_payload(digest: DigestDocument, *, details: bool) -> dict[str, Any]:
    """Build the stable HTTP representation without exposing persistence details."""
    payload: dict[str, Any] = {
        "digest_key": digest.digest_key,
        "version": digest.version,
        "period_start": digest.period_start,
        "period_end": digest.period_end,
        "timezone": digest.timezone,
        "title": digest.title,
        "summary": digest.summary,
        "generation_kind": digest.generation_kind,
        "status": digest.status,
        "created_at": digest.created_at,
        "published_at": digest.published_at,
        "item_count": len(digest.items),
        "source_count": len(digest.coverage),
        "web_path": f"/digests/{urllib.parse.quote(digest.digest_key, safe='')}",
    }
    if not details:
        return payload
    payload["items"] = [
        {
            "cluster_key": item.cluster_key,
            "title": item.title,
            "summary": item.summary,
            "score": item.score,
            "importance": item.importance,
            "urgency": item.urgency,
            "relevance": item.relevance,
            "confidence": item.confidence,
            "published_at": item.published_at,
            "regions": list(item.regions),
            "topics": list(item.topics),
            "source_ids": list(item.source_ids),
            "observation_ids": list(item.observation_ids),
            "links": list(item.links),
            "handling": item.handling,
        }
        for item in digest.items
    ]
    payload["coverage"] = [
        {
            "source_id": item.source_id,
            "status": item.status,
            "observation_count": item.observation_count,
            "last_attempt_at": item.last_attempt_at,
            "last_success_at": item.last_success_at,
            "consecutive_failures": item.consecutive_failures,
        }
        for item in digest.coverage
    ]
    return payload


def make_handler(
    store: ManagedConfigStore,
    database: ControlPlaneRepository,
    auth_token: str | None,
    *,
    heartbeat_timeout_seconds: int = 90,
    provider_registry: ProviderRegistry | None = None,
    notification_topic: str = "eos",
):
    registry = provider_registry or store.provider_registry
    authenticator = AdminAuth(database, auth_token)
    class Handler(BaseHTTPRequestHandler):
        server_version = f"ArgusAdmin/{__version__}"

        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(15.0)
            self.request_id = secrets.token_hex(8)


        def log_message(self, format: str, *args: object) -> None:
            LOGGER.info(
                "admin_http request_id=%s remote=%s message=%s",
                self.request_id,
                self.client_address[0],
                sanitize_error(format % args),
            )

        def _authenticate(self) -> AuthContext | None:
            direct_loopback = (
                self.client_address[0] in {"127.0.0.1", "::1"}
                and not self.headers.get("X-Real-IP")
            )
            context = authenticator.authenticate(
                self.headers,
                now=int(time.time()),
                allow_emergency=direct_loopback,
            )
            self.auth_context = context
            return context

        def _authorized(self, permission: str = "read") -> bool:
            context = getattr(self, "auth_context", None) or self._authenticate()
            return bool(context and context.allows(permission))

        def _actor(self) -> str:
            context = getattr(self, "auth_context", None)
            return context.username if context is not None else "anonymous"

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin", "")
            host = self.headers.get("Host", "").strip().lower()
            if not origin or not host:
                return False
            parsed = urllib.parse.urlsplit(origin)
            forwarded = self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
            scheme = forwarded if forwarded in {"http", "https"} else "http"
            return bool(
                parsed.scheme == scheme
                and parsed.netloc.lower() == host
                and not parsed.username
                and not parsed.password
                and parsed.path in {"", "/"}
                and not parsed.query
                and not parsed.fragment
            )

        @staticmethod
        def _write_permission(path: str) -> str:
            if path == "/api/auth/logout":
                return "read"
            if path.startswith("/api/users"):
                return "users:manage"
            if path.startswith("/api/reminders"):
                return "reminders:write"
            if path == "/api/events":
                return "events:write"
            if path.startswith("/api/source-quality"):
                return "quality:write"
            if path.startswith(("/api/outbox", "/api/jobs")):
                return "operations:write"
            if path.startswith(("/api/sources", "/api/source-bundles", "/api/rules", "/api/test-source")):
                return "sources:write"
            return "settings:write"

        def _body(self) -> Mapping[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 256 * 1024:
                raise AdminError("request body size is invalid")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, Mapping):
                raise AdminError("request body must be an object")
            return data

        def _expected_revision(self) -> int | None:
            value = self.headers.get("If-Match")
            if value is None:
                return None
            normalized = value.strip().strip('"')
            if not normalized.isdigit():
                raise AdminError("If-Match must contain a configuration revision")
            return int(normalized)

        def _json(
            self, status: int, payload: Any, *, headers: tuple[tuple[str, str], ...] = ()
        ) -> None:
            encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Request-ID", self.request_id)
            for name, value in headers:
                self.send_header(name, value)
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
            self.send_header("X-Request-ID", self.request_id)
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; "
                "img-src 'self' data:; font-src 'self'; form-action 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; manifest-src 'self'",
            )
            self.end_headers()
            self.wfile.write(encoded)

        def _health(self) -> dict[str, Any]:
            status = database.status()
            engine = status.get("engine") or {}
            desired_revision = store.revision
            applied_revision = engine.get("applied_revision")
            heartbeat_age = engine.get("heartbeat_age_seconds")
            engine_online = (
                engine.get("state") in {"running", "degraded"}
                and isinstance(heartbeat_age, int)
                and heartbeat_age <= heartbeat_timeout_seconds
            )
            revision_applied = (
                (desired_revision == 0 and applied_revision is None)
                or applied_revision == desired_revision
            )
            stale_sources = [
                str(source["source_id"])
                for source in status.get("sources", [])
                if source.get("configured_enabled")
                and (
                    source.get("runtime_status") not in {"active", "degraded"}
                    or int(time.time()) - max(
                        int(source.get("last_attempt_at") or 0),
                        int(source.get("registered_at") or 0),
                    ) > max(90, int(source.get("expected_interval_seconds") or 60) * 3)
                )
            ]
            return {
                "ok": bool(engine_online and revision_applied and not stale_sources),
                "engine_online": engine_online,
                "desired_revision": desired_revision,
                "applied_revision": applied_revision,
                "config_pending": not revision_applied,
                "heartbeat_age_seconds": heartbeat_age,
                "stale_sources": stale_sources,
                "status": status,
            }

        def do_GET(self) -> None:  # noqa: N802
            static = _static_file(self.path)
            if static is not None:
                self._static(static)
                return
            request = urllib.parse.urlsplit(self.path)
            path = request.path
            required = "users:manage" if path.startswith("/api/users") else "read"
            if not self._authorized(required):
                status = HTTPStatus.FORBIDDEN if getattr(self, "auth_context", None) else HTTPStatus.UNAUTHORIZED
                self._json(status, {"error": "forbidden", "code": "forbidden"})
                return
            query = urllib.parse.parse_qs(request.query)
            if path == "/api/auth/session":
                assert self.auth_context is not None
                self._json(HTTPStatus.OK, {"user": self.auth_context.public()})
                return
            if path == "/api/users":
                self._json(HTTPStatus.OK, {"users": database.list_admin_users()})
                return
            if path == "/api/users/audit":
                self._json(HTTPStatus.OK, {"audit": database.list_admin_auth_audit()})
                return
            if path == "/api/config":
                status = database.status()
                for name in ("engine", "runtime"):
                    if status.get(name) is not None:
                        status[name]["heartbeat_timeout_seconds"] = heartbeat_timeout_seconds
                self._json(HTTPStatus.OK, {
                    "managed": _public(store.read()),
                    "revision": store.metadata(),
                    "status": status,
                })
                return
            if path in {"/api/health", "/health"}:
                health = self._health()
                self._json(HTTPStatus.OK, {"ok": True, "engine": health})
                return
            if path in {"/api/readiness", "/ready"}:
                health = self._health()
                self._json(HTTPStatus.OK if health["ok"] else HTTPStatus.SERVICE_UNAVAILABLE, health)
                return
            if path in {"/api/metrics", "/metrics"}:
                payload = database.metrics_prometheus().encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Request-ID", self.request_id)
                self.end_headers()
                self.wfile.write(payload)
                return
            if path == "/api/providers":
                self._json(HTTPStatus.OK, {"providers": registry.describe()})
                return
            if path == "/api/news-catalog":
                self._json(HTTPStatus.OK, {"sources": NEWS_SOURCE_CATALOG.describe()})
                return
            if path == "/api/prompts":
                self._json(HTTPStatus.OK, {"prompts": _public(database.list_prompts())})
                return
            if path == "/api/source-quality":
                self._json(HTTPStatus.OK, {
                    "profiles": database.list_source_quality(),
                    "logic": {
                        "half_life_days": 90,
                        "min_effective_samples": 30,
                        "min_span_days": 90,
                        "weight_range": [0.70, 1.15],
                        "max_change_per_30_days": 0.05,
                        "scope": "digest_ranking_only",
                    },
                })
                return
            if path.startswith("/api/source-quality/") and path.endswith("/audit"):
                source_id = urllib.parse.unquote(path[len("/api/source-quality/") : -len("/audit")]).strip("/")
                self._json(HTTPStatus.OK, {"source_id": source_id, "audit": database.list_source_quality_audit(source_id)})
                return
            if path == "/api/digests":
                status_filter = query.get("status", ["published"])[0]
                try:
                    limit = int(query.get("limit", ["30"])[0])
                    if status_filter not in {"published", "draft", "superseded", "all"}:
                        raise ValueError("digest status is invalid")
                    if not 1 <= limit <= 100:
                        raise ValueError("digest limit is out of range")
                    rows = database.list_digests(
                        status=None if status_filter == "all" else status_filter,
                        limit=limit + 1,
                    )
                except ValueError as exc:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": str(exc), "code": "invalid_query"},
                    )
                    return
                truncated = len(rows) > limit
                visible = rows[:limit]
                self._json(
                    HTTPStatus.OK,
                    {
                        "digests": [
                            _digest_payload(digest, details=False) for digest in visible
                        ],
                        "pagination": {"total": len(visible), "truncated": truncated},
                    },
                )
                return
            if path.startswith("/api/digests/"):
                digest_key = urllib.parse.unquote(path[len("/api/digests/") :])
                try:
                    if not digest_key or "/" in digest_key or len(digest_key) > 128:
                        raise ValueError("digest key is invalid")
                    raw_version = query.get("version", [None])[0]
                    version = int(raw_version) if raw_version is not None else None
                    if version is not None and version < 1:
                        raise ValueError("digest version is invalid")
                    digest = database.get_digest(
                        digest_key,
                        version,
                        published_only=version is None,
                    )
                except ValueError as exc:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": str(exc), "code": "invalid_query"},
                    )
                    return
                if digest is None:
                    self._json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "digest not found", "code": "not_found"},
                    )
                else:
                    self._json(
                        HTTPStatus.OK,
                        {"digest": _digest_payload(digest, details=True)},
                    )
                return
            if path == "/api/revisions":
                rows = database.list_config_revisions()
                self._json(HTTPStatus.OK, {"revisions": rows, "pagination": {"total": len(rows)}})
                return
            if path.startswith("/api/revisions/"):
                try:
                    revision = int(path.rsplit("/", 1)[-1])
                except ValueError:
                    revision = -1
                item = database.get_config_revision(revision)
                if item is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "revision not found", "code": "not_found"})
                else:
                    self._json(HTTPStatus.OK, _public(item))
                return
            if path == "/api/incidents":
                rows = database.list_incidents()
                self._json(HTTPStatus.OK, {"incidents": rows, "pagination": {"total": len(rows)}})
                return
            if path.startswith("/api/alerts/"):
                raw_alert_id = path[len("/api/alerts/") :]
                try:
                    if not raw_alert_id.isdigit():
                        raise ValueError("alert ID is invalid")
                    alert_id = int(raw_alert_id)
                    if alert_id < 1:
                        raise ValueError("alert ID is invalid")
                except ValueError as exc:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": str(exc), "code": "invalid_query"},
                    )
                    return
                detail = database.get_alert_detail(alert_id)
                if detail is None:
                    self._json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "notification not found", "code": "not_found"},
                    )
                else:
                    self._json(HTTPStatus.OK, detail)
                return
            if path == "/api/reminders":
                rows = database.list_reminders()
                self._json(HTTPStatus.OK, {
                    "reminders": rows,
                    "server_time": int(time.time()),
                    "pagination": {"total": len(rows)},
                })
                return
            if path.startswith("/api/reminders/"):
                identifier = urllib.parse.unquote(path[len("/api/reminders/"):].strip())
                item = database.get_reminder(identifier)
                if item is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "reminder not found", "code": "not_found"})
                else:
                    self._json(HTTPStatus.OK, {"reminder": item, "server_time": int(time.time())})
                return
            if path.startswith("/api/jobs/"):
                job_id = urllib.parse.unquote(path[len("/api/jobs/"):].strip())
                item = database.get_admin_job(job_id)
                if item is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "job not found", "code": "not_found"})
                else:
                    self._json(HTTPStatus.OK, {"job": _public(item)})
                return
            if path == "/api/outbox":
                status_filter = query.get("status", [None])[0]
                try:
                    limit = int(query.get("limit", ["100"])[0])
                    before_id = query.get("before_id", [None])[0]
                    before = int(before_id) if before_id is not None else None
                    rows = database.list_alerts(status=status_filter, limit=limit, before_id=before)
                except ValueError as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc), "code": "invalid_query"})
                    return
                next_cursor = rows[-1]["id"] if len(rows) == max(1, min(limit, 500)) else None
                self._json(HTTPStatus.OK, {
                    "alerts": _public(rows),
                    "pagination": {"next_cursor": next_cursor, "truncated": next_cursor is not None},
                })
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found", "code": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            path = urllib.parse.urlsplit(self.path).path
            try:
                data = self._body()
                if path == "/api/auth/login":
                    if not self._same_origin():
                        self._json(
                            HTTPStatus.FORBIDDEN,
                            {"error": "origin validation failed", "code": "origin_failed"},
                        )
                        return
                    username = str(data.get("username", ""))
                    password = str(data.get("password", ""))
                    remote = self.headers.get("X-Real-IP", self.client_address[0])[:128]
                    try:
                        context, session_token, csrf_token = authenticator.login(
                            username, password, remote, now=int(time.time())
                        )
                    except LoginBlockedError:
                        raise
                    except AuthError:
                        self._json(
                            HTTPStatus.UNAUTHORIZED,
                            {"error": "invalid username or password", "code": "login_failed"},
                        )
                        return
                    cookies = tuple(
                        ("Set-Cookie", value)
                        for value in authenticator.cookie_headers(session_token, csrf_token)
                    )
                    self.auth_context = context
                    self._json(HTTPStatus.OK, {"user": context.public()}, headers=cookies)
                    return
                if not self._authorized(self._write_permission(path)):
                    status = HTTPStatus.FORBIDDEN if getattr(self, "auth_context", None) else HTTPStatus.UNAUTHORIZED
                    self._json(status, {"error": "forbidden", "code": "forbidden"})
                    return
                assert self.auth_context is not None
                if not self.auth_context.emergency and not self._same_origin():
                    self._json(
                        HTTPStatus.FORBIDDEN,
                        {"error": "origin validation failed", "code": "origin_failed"},
                    )
                    return
                if not authenticator.validate_csrf(self.auth_context, self.headers):
                    self._json(HTTPStatus.FORBIDDEN, {"error": "CSRF validation failed", "code": "csrf_failed"})
                    return
                expected_revision = self._expected_revision()
                actor = self._actor()
                if path == "/api/auth/logout":
                    if self.auth_context.session_hash:
                        database.revoke_admin_session(self.auth_context.session_hash, int(time.time()))
                    cookies = tuple(
                        ("Set-Cookie", value) for value in authenticator.clear_cookie_headers()
                    )
                    self._json(HTTPStatus.OK, {"logged_out": True}, headers=cookies)
                    return
                if path == "/api/users":
                    if not all(isinstance(data.get(key), str) for key in (
                        "username", "display_name", "password", "role"
                    )):
                        raise AdminError("user identity, password, and role must be strings")
                    username = authenticator.validate_username(data["username"])
                    password_hash = authenticator.hash_password(data["password"])
                    role = data["role"]
                    saved = database.create_admin_user(
                        username, data["display_name"], password_hash,
                        role, actor, int(time.time()),
                    )
                    self._json(HTTPStatus.CREATED, {"user": saved})
                    return
                if path.startswith("/api/users/"):
                    parts = path.strip("/").split("/")
                    if len(parts) not in {3, 4}:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "not found", "code": "not_found"})
                        return
                    user_id = int(parts[2])
                    if len(parts) == 4 and parts[3] == "password":
                        if not isinstance(data.get("password"), str):
                            raise AdminError("password must be a string")
                        changed = database.set_admin_user_password(
                            user_id, authenticator.hash_password(data["password"]),
                            actor, int(time.time()),
                        )
                        if not changed:
                            raise AdminError("admin user not found")
                        self._json(HTTPStatus.OK, {"changed": True})
                        return
                    if len(parts) == 4 and parts[3] == "revoke-sessions":
                        count = database.revoke_admin_user_sessions(user_id, actor, int(time.time()))
                        self._json(HTTPStatus.OK, {"revoked": count})
                        return
                    if len(parts) != 3:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "not found", "code": "not_found"})
                        return
                    if (
                        not isinstance(data.get("display_name"), str)
                        or not isinstance(data.get("role"), str)
                        or not isinstance(data.get("enabled"), bool)
                    ):
                        raise AdminError("display_name and role must be strings; enabled must be boolean")
                    saved = database.update_admin_user(
                        user_id, data["display_name"], data["role"], data["enabled"],
                        actor, int(time.time()),
                    )
                    self._json(HTTPStatus.OK, {"user": saved})
                    return
                if path == "/api/reminders":
                    now = int(time.time())
                    reminder = parse_reminder(data, now)
                    saved = database.upsert_reminder(reminder, actor, now)
                    self._json(HTTPStatus.OK, {"saved": saved, "restart_required": False})
                    return
                if path == "/api/events":
                    now = int(time.time())
                    event = parse_manual_event(data)
                    detail = database.create_manual_event(
                        event, actor, now, notification_topic
                    )
                    self._json(HTTPStatus.CREATED, detail)
                    return
                if path == "/api/prompts":
                    prompt_id = data.get("prompt_id")
                    version = data.get("version")
                    system_text = data.get("system_text")
                    if not isinstance(prompt_id, str) or not isinstance(version, int) or isinstance(version, bool) or not isinstance(system_text, str):
                        raise AdminError("prompt requires prompt_id, integer version, and system_text")
                    database.save_prompt(prompt_id, version, system_text, actor, int(time.time()))
                    self._json(HTTPStatus.OK, {"saved": _public(database.get_prompt(prompt_id, version)), "restart_required": False})
                    return
                if path.startswith("/api/source-quality/"):
                    suffix = path[len("/api/source-quality/") :]
                    if suffix.endswith("/feedback"):
                        source_id = urllib.parse.unquote(suffix[: -len("/feedback")]).strip("/")
                        observation_id = data.get("observation_id")
                        feedback_id = database.record_source_quality_feedback(
                            source_id,
                            int(data.get("signal")),
                            str(data.get("reason", "")),
                            actor,
                            int(time.time()),
                            int(observation_id) if observation_id is not None else None,
                        )
                        self._json(HTTPStatus.CREATED, {"id": feedback_id, "profile": database.list_source_quality(source_ids=[source_id])[0]})
                        return
                    if suffix.endswith("/override"):
                        source_id = urllib.parse.unquote(suffix[: -len("/override")]).strip("/")
                        database.set_source_quality_override(
                            source_id,
                            float(data.get("weight")),
                            str(data.get("reason", "")),
                            actor,
                            int(time.time()),
                        )
                        self._json(HTTPStatus.OK, {"profile": database.list_source_quality(source_ids=[source_id])[0]})
                        return
                if path == "/api/analysis":
                    revision = store.set_analysis(
                        data,
                        actor=actor,
                        expected_revision=expected_revision,
                    )
                    self._json(HTTPStatus.OK, {
                        "saved": _public(store.read().get("analysis", {})),
                        "revision": revision,
                        "restart_required": True,
                    })
                    return
                if path == "/api/digest-config":
                    revision = store.set_digest(
                        data, actor=actor, expected_revision=expected_revision
                    )
                    self._json(HTTPStatus.OK, {
                        "saved": _public(store.read().get("digest", {})),
                        "revision": revision,
                        "restart_required": True,
                    })
                    return
                if path.startswith("/api/reminders/") and path.endswith(("/enable", "/disable")):
                    parts = path.strip("/").split("/")
                    identifier = urllib.parse.unquote(parts[2])
                    enabled = path.endswith("/enable")
                    saved = database.set_reminder_enabled(identifier, enabled, actor, int(time.time()))
                    self._json(HTTPStatus.OK, {"saved": saved, "restart_required": False})
                    return
                if path == "/api/source-bundles":
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
                        expected_revision=expected_revision,
                    )
                    self._json(HTTPStatus.OK, {
                        "saved": {"source": _public(source), "rule": _public(rule)},
                        "revision": revision,
                        "restart_required": True,
                    })
                    return
                if path in {"/api/sources", "/api/rules"}:
                    kind = "source" if path.endswith("sources") else "rule"
                    item = store.upsert(
                        kind,
                        data,
                        actor=actor,
                        expected_revision=expected_revision,
                    )
                    self._json(HTTPStatus.OK, {
                        "saved": _public(item),
                        "revision": store.revision,
                        "restart_required": True,
                    })
                    return
                if path == "/api/validate":
                    result = store.validate(data)
                    self._json(HTTPStatus.OK, {"valid": True, "summary": result})
                    return
                if path == "/api/test-source":
                    raw = data.get("source", data)
                    if not isinstance(raw, Mapping):
                        raise AdminError("test-source requires a source object")
                    _reject_secret_values(raw, "source")
                    raw_source = dict(raw)
                    raw_source["enabled"] = True
                    _parse_source(raw_source, 0, provider_registry=registry)
                    job = database.create_admin_job(
                        "test_source", {"source": raw_source}, actor, int(time.time())
                    )
                    self._json(HTTPStatus.ACCEPTED, {"job": _public(job)})
                    return
                if path.startswith("/api/sources/") and path.endswith(("/enable", "/disable")):
                    parts = path.strip("/").split("/")
                    identifier = urllib.parse.unquote(parts[2])
                    current = store.read()
                    source = next((row for row in current["sources"] if row.get("id") == identifier), None)
                    if source is None:
                        raise AdminError("source not found")
                    source = dict(source)
                    source["enabled"] = path.endswith("/enable")
                    item = store.upsert(
                        "source",
                        source,
                        actor=actor,
                        expected_revision=expected_revision,
                    )
                    self._json(HTTPStatus.OK, {
                        "saved": _public(item),
                        "revision": store.revision,
                        "restart_required": True,
                    })
                    return
                if path.startswith("/api/revisions/") and path.endswith(("/rollback", "/activate")):
                    parts = path.strip("/").split("/")
                    target = int(parts[2])
                    new_revision = store.rollback(
                        target,
                        actor=actor,
                        expected_revision=expected_revision,
                    )
                    self._json(HTTPStatus.OK, {
                        "revision": new_revision,
                        "rolled_back_from": target,
                        "restart_required": True,
                    })
                    return
                if path.startswith("/api/outbox/") and path.endswith(("/retry", "/cancel")):
                    parts = path.strip("/").split("/")
                    alert_id = int(parts[2])
                    changed = (
                        database.retry_alert(alert_id, int(time.time()))
                        if path.endswith("/retry")
                        else database.cancel_alert(alert_id, int(time.time()))
                    )
                    if not changed:
                        self._json(HTTPStatus.CONFLICT, {
                            "error": "alert state does not allow this action",
                            "code": "invalid_state",
                        })
                    else:
                        self._json(HTTPStatus.OK, {"changed": True, "alert": database.get_alert(alert_id)})
                    return
                if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                    job_id = urllib.parse.unquote(path[len("/api/jobs/"):-len("/cancel")].strip("/"))
                    changed = database.cancel_admin_job(job_id, int(time.time()))
                    self._json(HTTPStatus.OK, {"changed": changed})
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found", "code": "not_found"})
            except RevisionConflictError as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc), "code": "revision_conflict"})
            except LoginBlockedError as exc:
                self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": str(exc), "code": "login_blocked"})
            except (
                AdminError, AuthError, ConfigError, ManualEventError, ReminderError,
                KeyError, ValueError, TypeError,
            ) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc), "code": "invalid_request"})
            except (OSError, RuntimeError) as exc:
                LOGGER.error("admin_request_failed request_id=%s error=%s", self.request_id, sanitize_error(exc))
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {
                    "error": "internal management error",
                    "code": "internal_error",
                })

        def do_DELETE(self) -> None:  # noqa: N802
            path = urllib.parse.urlsplit(self.path).path
            if not self._authorized(self._write_permission(path)):
                status = HTTPStatus.FORBIDDEN if getattr(self, "auth_context", None) else HTTPStatus.UNAUTHORIZED
                self._json(status, {"error": "forbidden", "code": "forbidden"})
                return
            assert self.auth_context is not None
            if not self.auth_context.emergency and not self._same_origin():
                self._json(
                    HTTPStatus.FORBIDDEN,
                    {"error": "origin validation failed", "code": "origin_failed"},
                )
                return
            if not authenticator.validate_csrf(self.auth_context, self.headers):
                self._json(HTTPStatus.FORBIDDEN, {"error": "CSRF validation failed", "code": "csrf_failed"})
                return
            try:
                expected_revision = self._expected_revision()
                if path.startswith("/api/outbox/"):
                    alert_id = int(urllib.parse.unquote(path[len("/api/outbox/"):].strip()))
                    removed = database.discard_alert(alert_id)
                    if removed:
                        self._json(HTTPStatus.OK, {"removed": True})
                    else:
                        self._json(HTTPStatus.CONFLICT, {
                            "error": "alert state does not allow this action",
                            "code": "invalid_state",
                            "removed": False,
                        })
                    return
                if path.startswith("/api/reminders/"):
                    identifier = urllib.parse.unquote(path[len("/api/reminders/"):].strip())
                    removed = database.delete_reminder(identifier, self._actor(), int(time.time()))
                    self._json(HTTPStatus.OK, {"removed": removed, "restart_required": False})
                    return
                if path.startswith("/api/source-quality/") and path.endswith("/override"):
                    source_id = urllib.parse.unquote(path[len("/api/source-quality/") : -len("/override")]).strip("/")
                    changed = database.clear_source_quality_override(source_id, self._actor(), int(time.time()))
                    profiles = database.list_source_quality(source_ids=[source_id])
                    self._json(HTTPStatus.OK, {"changed": changed, "profile": profiles[0] if profiles else None})
                    return
                if path.startswith("/api/source-bundles/"):
                    identifier = urllib.parse.unquote(path[len("/api/source-bundles/"):].strip())
                    removed, removed_rules, revision = store.delete_bundle(
                        identifier,
                        actor=self._actor(),
                        expected_revision=expected_revision,
                    )
                    self._json(HTTPStatus.OK, {
                        "removed": removed,
                        "removed_rules": removed_rules,
                        "revision": revision,
                        "restart_required": removed,
                    })
                    return
                prefix = "/api/sources/" if path.startswith("/api/sources/") else "/api/rules/"
                if not path.startswith(prefix):
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found", "code": "not_found"})
                    return
                identifier = urllib.parse.unquote(path[len(prefix):].strip())
                removed = store.delete(
                    "source" if prefix.endswith("sources/") else "rule",
                    identifier,
                    actor=self._actor(),
                    expected_revision=expected_revision,
                )
                self._json(HTTPStatus.OK, {
                    "removed": removed,
                    "revision": store.revision,
                    "restart_required": removed,
                })
            except RevisionConflictError as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc), "code": "revision_conflict"})
            except AdminError as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc), "code": "conflict"})
            except (ValueError, TypeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc), "code": "invalid_request"})

    return Handler


def serve(
    config: AdminConfig,
    store: ManagedConfigStore,
    database: ControlPlaneRepository,
    environment: Mapping[str, str] | None = None,
    *,
    heartbeat_timeout_seconds: int = 90,
    notification_topic: str = "eos",
) -> None:
    env = os.environ if environment is None else environment
    token = env.get(config.auth_token_env, "") if config.auth_token_env else None
    if config.auth_token_env and not token:
        raise AdminError(f"required environment variable {config.auth_token_env} is missing")
    # A single-threaded server keeps the SQLite connection in its owner thread.
    # Requests are local management calls and intentionally bounded in size.
    server = HTTPServer(
        (config.bind, config.port),
        make_handler(
            store,
            database,
            token,
            heartbeat_timeout_seconds=heartbeat_timeout_seconds,
            notification_topic=notification_topic,
        ),
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
