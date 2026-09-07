# Providers and news source catalog

## Design boundary

A provider definition is metadata, not a long-lived client object. The immutable
`ProviderSpec` describes a source kind's capabilities, settings schema,
credential references, safe connection-test strategy, URL policy, and whether a
runtime collector exists. `SourceConfig` remains the concrete configured source,
and a collector factory owns network or host access.

This separates four concerns:

1. `providers.py` declares what a provider needs and can do.
2. `config.py` validates a source through the selected registry.
3. `adapters.py` binds source kinds to collector factories.
4. collectors normalize results into the existing `Observation` contract.

The default registry is immutable. An embedding application can call
`DEFAULT_PROVIDER_REGISTRY.extend(spec)` and pass that registry plus a factory to
configuration loading and `build_collector`; adding a provider no longer
requires another core `if kind == ...` branch.

## Built-in provider capabilities

| Kind | Main capabilities | Credential references | Connection test | Runtime |
|---|---|---|---|---|
| `rss` | feed/news items | none | one bounded HTTPS fetch | active |
| `youtube` | feed/video updates | none | one bounded official channel-feed fetch | active |
| `x` | social posts | `bearer_token_env` | read-only API call | active |
| `imap` | email messages | `username_env`, `password_env` | read-only mailbox probe | active |
| `market` | Alpaca US-equity quotes and stateful incidents | `api_key_env`, `api_secret_env` | read-only Alpaca API call | active |
| `host` | host health and stateful incidents | none | non-mutating local probe | active |
| `mqtt` | sensor events | none yet | configuration only | disabled/fail-closed |
| `heartbeat` | outbound liveness | none in source config | configuration only | disabled/fail-closed |

A credential field stores only the environment variable name. Secret values stay
in the service environment and must never enter managed source JSON, the catalog,
logs, or revision history.

Connection tests are required to be side-effect free. A test must use the same
URL allowlist, TLS, timeout, response-size limit, and credential-to-host binding
as the real collector. IMAP tests select mailboxes read-only; host tests do not
start, stop, or reconfigure services.

The legacy-compatible kind name `market` is specifically the Alpaca snapshot
adapter: its authentication headers, URL path and response shape are Alpaca
contracts, not a generic JSON market interface. IBKR, Hong Kong market data, or
another vendor requires a separate provider kind and adapter so symbol semantics,
entitlements, trading calendars and response validation remain honest and
testable.

## Adding a provider

A new provider should be a narrow adapter, not a special pipeline:

1. Declare one `ProviderSpec`, including capabilities and every setting or
   credential reference.
2. Add provider-specific validation only for invariants that generic field
   metadata cannot express.
3. Implement a collector that returns normalized observations and owns a
   resumable cursor if pagination is possible.
4. Register its factory at the composition boundary.
5. Add contract tests for baseline behavior, retry/cursor safety, bounded input,
   credential-host binding, and partial failure.

Do not add a provider if it requires scraping protected article HTML, bypassing a
paywall, storing account cookies, or silently accepting an arbitrary redirect or
credential destination.

## News source catalog

`news_catalog.py` is a reviewed catalog of source templates, verified on
2026-09-05. It is deliberately not an enablement list. Every entry is disabled
by default and requires explicit confirmation before it may be enabled.
Templates store feed metadata and the original article link only; they do not
fetch article pages or circumvent publisher access controls.

| Publisher | Catalog support | Access note |
|---|---|---|
| Bloomberg | Markets, Politics, Technology, Economics official RSS | mixed/public metadata; articles may require subscription |
| The Wall Street Journal | World, Markets, US Business official RSS endpoints | subscription content remains protected |
| The Economist | World this week, Finance & economics, Business, Science & technology endpoints | endpoint availability and subscription policy must be tested |
| Financial Times | World and Global Economy RSS-format endpoints | full articles remain under FT access controls |
| Reuters | no generic public URL pinned | use Reuters Connect/licensed service or a user-confirmed official URL |
| Associated Press | no generic public URL pinned | use AP Media API/licensed service or a user-confirmed official URL |
| Federal Reserve Board | all press releases, monetary policy, speeches | public official RSS |
| U.S. SEC | press releases | public official RSS; use conservative polling |
| European Central Bank | press releases | public official RSS |
| Bank of Japan | official updates | public official RSS; primary source, Japan priority 4 |
| Japan Meteorological Agency | high-frequency disaster and weather notices | public official JMAXML Atom |
| National Development and Reform Commission (China) | press releases | public official RSS; primary source |
| World Health Organization | news releases and statements | public official RSS |
| NASA | news releases | public official RSS |
| U.S. Geological Survey | significant earthquakes | public official Atom |

Publisher endpoints and terms can change after the verification date. Before
activation, review the catalog evidence URL, confirm the exact feed URL and host
allowlist, run the provider's connection test, and inspect a sample of normalized
observations. A failed test must leave the source disabled.

The 2026-09-05 production content check found that the public WSJ feeds return
HTTP 200 but stop at 2025-01-27. Those endpoints remain documented for diagnosis,
not endorsed as working real-time news sources. The configured WSJ sources are
disabled until a current authorized feed is available. Bloomberg, FT and The
Economist had current entries; sparse official monetary-policy announcements
must not be judged on a daily-news publication schedule.

RSS `settings.max_content_age_seconds` optionally rejects a feed whose newest
entry is older than its publishing policy (`0` disables this check). When
enabled, conditional caching is bypassed so repeated 304 responses cannot hide
stale content. Catalog news templates carry conservative publisher-specific
limits; infrequent central-bank and regulator announcements leave it disabled.
The connection test checks content freshness as well as transport/parsing.
Some legacy official feeds, including BOJ, still emit HTTP article links. The
RSS adapter upgrades a stored article link to HTTPS only when its hostname is
already in the exact source allowlist; it never fetches the article as part of
that conversion.

The catalog distinguishes three integration modes:

- `verified_rss`: a reviewed feed template is available.
- `user_confirmed_official_url`: the user must provide and confirm an official
  HTTPS feed URL.
- `licensed_provider`: use an authorized API/feed under the publisher's terms;
  no public URL is invented by EyeOfSauron and it cannot be forced through the
  generic RSS collector.

`NEWS_SOURCE_CATALOG.source_template(...)` returns an ordinary source mapping, so
it passes through the same strict configuration validator as a hand-written TOML
source. Enabling a verified template without `user_confirmed=True` fails. A
future user-supplied official RSS URL must require explicit confirmation and an
exact HTTPS host allowlist. Licensed JSON/API products require a dedicated
provider adapter that implements their authentication, pagination, and rights
metadata instead of pretending to be RSS.
