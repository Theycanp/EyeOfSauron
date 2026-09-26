# EyeOfSauron documentation map

This directory is the operational handoff for EyeOfSauron (EOS). A maintainer
should be able to understand, rebuild, operate, and recover the service from the
repository plus separately supplied secrets and production data.

Start here:

1. `SYSTEM.md` — product scope, end-to-end data flow, modules, and extension ports.
2. `CONFIGURATION.md` — configuration authority, feature switches, secrets, and
   production defaults.
3. `DIGESTS.md` — deterministic selection, AI synthesis, Prompt versions,
   fallback, retry, and failure escalation.
4. `DATA.md` — SQLite ownership, schema, migrations, retention, and backup.
5. `SOURCES.md` — source catalog policy, activation, first-run behavior, and the
   current reviewed source set.
6. `CONTENT.md` — 正文抓取、版权边界、失败降级、域名退避和任务诊断。
7. `REPRODUCTION.md` — build a fresh instance from source and verify it.
8. `OPERATIONS.md` — day-two commands, admin operation, deployment, and recovery.
9. `EVENTS.md` — event-centric data model, clustering, report relationships, and
   the staged migration from legacy digest clusters.
10. `WORKFLOW.md` — the required implementation, documentation, PR, deployment,
   and production-acceptance checklist for every substantive update.
11. `ROADMAP.md` — current production findings, staged improvements, ownership
    boundaries, and acceptance criteria.
12. `WEATHER.md` — local-weather source research, forecast validation, polling,
    notification state, operational limits, and official-warning gaps.

Reference documents:

- `ARCHITECTURE.md` records durable design contracts and invariants.
- `PROVIDERS.md` defines provider adapters and the reviewed catalog.
- `RELEASES.md` defines versioning, artifacts, rollout, and rollback.
- `COMPATIBILITY.md` records supported runtime versions.
- `AUDIT-0.7.md` is historical audit evidence, not current configuration.

Secrets are intentionally absent. The authoritative production configuration is
the active SQLite configuration revision plus `/etc/argus/config.toml`; the docs
describe locations and variable names only.
