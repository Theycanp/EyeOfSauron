# Source lifecycle and reviewed coverage

## Catalog and adapters

The catalog in `news_catalog.py` is inert reviewed metadata. It declares exact
HTTPS endpoints, host allowlists, access model, content policy, region, source
tier, default importance, and evidence. Runtime sources are managed configuration
and are instantiated through provider/collector ports. A catalog entry alone does
not run a collector.

Before a source is enabled by a rollout, the operations script performs a real
network fetch and parser pass. If any new source fails, no configuration revision
is written. First success establishes a baseline, so adding an existing feed does
not flood ntfy with historical entries.

## Current policy

Commercial sources retain feed metadata/excerpts and original links. Public
first-party sources may queue bounded text extraction for published HTML/PDF.
Exact host allowlists, public-address DNS checks, manual redirect validation,
timeouts, compressed/output size limits, and content-type checks constrain access.

Source regions use the macro taxonomy `EAST_ASIA`, `NORTH_AMERICA`, `EUROPE`,
`AUSTRALIA_OCEANIA`, `SOUTHEAST_ASIA`, `MIDDLE_EAST`, `SOUTH_AMERICA`, and
`AFRICA`, with `GLOBAL`/`OTHER` fallbacks. Historical `CN`, `JP`, and `US`
values remain accepted and are mapped to their macro families only for ranking;
there is no forced region quota.

Normal government, science, and corporate announcements enter the digest.
Auditable weighted rules send immediate messages only for high-signal patterns
such as major emergencies, material monetary-policy changes, severe disasters,
or embassy security alerts. A source being primary does not mean every item is
urgent.

## 2026-09-15 additions

- U.S. Embassy and Consulates in China official Alerts RSS remains a reviewed
  catalog candidate. It previously returned parseable items, but on 2026-09-16
  consistently returned HTTP 403 to the bounded EOS collector. It is excluded
  from the default atomic rollout because one broken endpoint would roll back
  every otherwise healthy source addition. The dedicated security rule remains
  available for a future stable official endpoint.
- NVIDIA Newsroom official RSS:
  `https://nvidianews.nvidia.com/rss.xml`. It provides first-party AI,
  semiconductor, product, and corporate announcements and is added to the daily
  technology coverage. It cannot replace independent reporting about customer
  restrictions or confidential enterprise policy.

The referenced Sina story, “英伟达，开始限制 Claude 使用了”, is a secondary
technology report about enterprises limiting model use around sensitive data.
Sina's discoverable public technology/finance RSS endpoints were tested but their
newest items were from 2018, so they were not enabled as if current. The system
instead adds the verified NVIDIA primary feed and retains its pluggable catalog
for future current Chinese technology sources. A source must be genuinely live;
headline relevance alone is not enough to waive freshness and licensing checks.

The UN OCHA RSS candidate is handled the same way: its endpoint returned HTTP
406 (bot-activity rejection) to the EOS collector on 2026-09-16. It remains in
the reviewed catalog but is not part of the default activation set.

## Activation and change control

Use `scripts/operations/activate_news_sources.py` with the active configuration
revision. Run without `--apply` first; it lists additions and probes every one.
The planner is idempotent and extends existing reviewed rules without replacing
operator-edited thresholds or patterns. With `--apply`, one validated immutable
configuration revision is written. Then wait for Argus to acknowledge that exact
revision and verify every new source baseline and failure counter.

Disabling a source is an operator choice and rollout code must preserve it. A feed
that becomes stale, forbidden, or structurally invalid should be disabled or
marked unavailable rather than silently scraped through an unofficial mirror.
