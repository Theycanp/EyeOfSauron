# Local weather: sources, polling, and alert policy

## Scope and source decision

The initial default subscription was Beijing University of Posts and Telecommunications,
Shahe campus (`40.1561163, 116.2835626`, `Asia/Shanghai`). The operator can change
the active location; use the admin page and deployment handover for its current
value rather than treating these initial coordinates as production state. Tiananmen is only a
suggested example for future manual setup. A browser may supply coordinates, or
an operator may search for a place and edit coordinates in the admin Weather page.
There is one subscription; this is not a multi-user weather service. Schema 23
introduced the subscription, schema 24 adds typed optional observations, schema
25 adds the distinct approximate solar-noon angle and sample time, and schema
26 permits an independently tracked QWeather astronomy channel. Schema 28 adds
persistent provider policies and per-UTC-day HTTP usage.

Source review on 2026-09-26:

| Candidate | Result | Decision |
|---|---|---|
| Open-Meteo forecast and geocoding APIs | HTTPS JSON returned 72 hourly values for the Shahe coordinates. Forecast and place search worked from this host without a key. Free API terms allow non-commercial use below 10,000 calls/day and require CC BY 4.0 attribution. | Use for model forecast and location search. One hourly request is about 24/day; place searches are operator initiated. |
| QWeather minute precipitation, hourly and official-alert APIs | All three returned valid JSON; Ed25519 JWT separately verified. Minute precipitation returned 24 slots and a district lightning warning was present. | Minute precipitation and official alerts are independent channels; `/v7/weather/24h` is a conditional forecast fallback, not an always-on second vote. |
| Open-Meteo Air Quality API | Current PM2.5, PM10, European AQI and US AQI returned for the subscribed coordinates. Values are model estimates, not station measurements. | Optional independent six-hour-freshness snapshot; not used to trigger official alerts. EU and US indices retain separate labels. |
| QWeather sun, moon and solar elevation APIs | JWT requests returned local sunrise/sunset, moonrise/moonset, hourly phase/illumination and angles at requested times. | Optional local-date-matched astronomy snapshot. Request the sunrise/sunset midpoint separately as approximate solar noon; keep the poll-time angle for admin inspection. Either angle request may fail without discarding sun/moon data. Moon angle was not verified and is not claimed. |
| China Meteorological Administration / National Meteorological Center public pages | Public pages were reachable, but no verified, authorized machine-readable warning feed was established; the NMC page's reuse restriction rules out treating HTML scraping as an authorized integration. | Do not scrape or represent a model forecast as an official warning. |
| US NWS and Japan JMA | Official warning products exist but their jurisdiction is not Beijing. | Not a Beijing local-weather provider. Existing JMA news monitoring remains a separate international-disaster signal. |

Open-Meteo documents: [forecast API](https://open-meteo.com/en/docs),
[geocoding API](https://open-meteo.com/en/docs/geocoding-api),
[terms](https://open-meteo.com/en/terms), and
[licence](https://open-meteo.com/en/licence). This is an automated model forecast,
not observed precipitation at the campus and not an official emergency warning.
The notification and admin page say so and credit Open-Meteo. Open-Meteo's `dust`
variable describes Saharan dust; it is **not** used for Beijing sandstorm alerts.
QWeather adds a distinct channel for relayed official warnings, including dust
when an issuing authority provides such an alert at the queried coordinates.
This is not blanket coverage or an inference from the Open-Meteo dust variable.

## Update and notification state machine

Argus fetches the current conditions and 72 hourly forecast slots at startup and
then according to its channel interval (one hour by default). An
admin settings revision is observed by the worker within one second, so a
location change starts the next fetch promptly across the separate admin and
Argus processes. QWeather's independent channels use the same revision check
to refresh promptly. The request
is limited to fixed Open-Meteo HTTPS hosts, 12 seconds, no redirects, JSON only,
and 512 KiB. The response must have a current observation no older than two
hours, valid finite values and contiguous hourly coverage through the next 24
hours. Invalid or stale data counts as failure; it never updates the dry/rainy
baseline. Three consecutive failures send one provider-outage alert, and the
first successful fetch after that sends one recovery alert.

At or after the configured local time (default 07:00), the first successful fetch
on that same local calendar day queues one daily forecast per date. It reports current
condition, the local 06:00-to-next-day-06:00 temperature range, remaining-window predicted precipitation,
maximum rain probability, gusts, humidity, wind, sunrise/sunset, the day's
maximum UV index when Open-Meteo supplies it, Gregorian and lunar dates, and a
small verified set of fixed-date festivals. Since 0.29.6, new daily messages use
short lines: conditions/temperature, window/remaining precipitation, wind/humidity,
sun/UV, calendar, then compact moon and air quality when available. The moon
illumination, solar angles, PM10 and a clearly labelled AQI standard remain on the
weather page; full labelled test snapshots retain the detailed text. Dry weather
is “无降水”, not “0 mm”; partial dry coverage says missing hours are unknown.
Fresh optional air quality and matching-date QWeather moon data are appended when available;
missing optional data is labelled missing, not invented. Qingming and other
solar-term or movable festivals are not yet calculated. The daily-date key is durable; retries and
restarts do not duplicate it. If the scheduled time is missed, the first
successful forecast later on the same local calendar day still publishes that
day's report; it is not silently discarded after a nominal six-hour window. The
daily notification can be disabled separately
from change alerts.

Remaining-day rain is `expected` when the model predicts at least 1 mm and a
maximum hourly probability of at least 60%. Once expected, it remains so until
the forecast drops below 0.3 mm or 35% (hysteresis). The first fetch each local
day establishes a baseline; a later dry-to-rain transition queues one ordinary
change alert for that date. Persisted rain state and a unique daily dedupe key
prevent a repeat if forecasts oscillate, Argus restarts, or the next hourly
fetch still says rain. A new day gets a new baseline. A significant-rain alert
supersedes the ordinary rain-change alert for the same update.

Separate forecast hazards cover the next six hours: predicted precipitation at
least 5 mm and probability at least 75%; gust at least 55 km/h; or an 8 C fall
between current temperature and roughly 24 hours later. Rain >=30 mm or gust
>=85 km/h raises the priority from 4 to 5. Active hazards are stateful and do
not alert again on every poll; severity upgrades may alert. A resolved hazard
that reappears after six hours can alert again. These thresholds are cautious
product heuristics, not official warning levels. They do not detect rain that
is absent from the model or guarantee notice before an event begins.

The `WeatherProvider` and `WeatherRepository` protocols isolate the forecast
source and persistent state. `open_meteo.py` is the network adapter,
`weather.py` owns validation, bounded hourly forecast projection and rules,
`sqlite_weather.py` owns forecast/alert state; `sqlite_weather_policy.py` owns
request budgets and usage,
and atomic outbox writes, and `service.py` owns scheduling. The admin API uses
the same repository boundary; settings writes require `settings:write`, origin
and CSRF validation, revision match, and an audit record. Read/search requires
an authenticated session. State is retained across restarts; changing location
or timezone resets the forecast and rain baseline. An ordinary settings edit
does not erase the day's sent-notification history.

## QWeather cooperation

QWeather is not an extra vote in a probability average. Open-Meteo is the
primary daily/remaining-day provider. QWeather minute precipitation adds a short-term
change channel, even when the earlier forecast missed rain; official warnings
never require model agreement. Each channel has independent failure state.

Minute precipitation is queried every 10 minutes, or every 5 minutes after a wet
signal. A signal requires at least 0.3 mm in wet slots (each >=0.05 mm/5 min),
and a predicted start within 45 minutes. One rain/snow notification is sent when
the signal appears; continued wet forecasts are silent, while rain-to-snow may
notify. Thirty minutes without a fresh wet signal ends the active state; a new
same-phase episode has a four-hour cooldown. This is a forecast, not observation.

Official alerts are queried every 10 minutes. First success establishes a
baseline for old moderate warnings; severe/extreme alerts issued within six
hours may notify during baseline. Later new moderate-or-higher warnings,
severity upgrades, and explicit cancellation of previously announced warnings
notify. IDs and supersedes chains provide durable dedupe. Disappearance is not
an official cancellation. Minute precipitation and alerts retry failures in five
minutes (or their configured interval when shorter); astronomy retries after
one hour (or its configured interval when shorter). All requests remain bounded
by their daily HTTP budget;
three consecutive failures notify once and recovery once. The change-alert
toggle suppresses weather-condition notifications while polls maintain state.
Provider outage/recovery notices remain enabled independently of that toggle.
Orange-to-red and severe-to-extreme changes notify even when delivery priority
is already 5. Recent explicit cancellation can notify after its expiry time;
old cancellations beyond six hours do not replay at startup.

The protected service environment uses `QWEATHER_API_HOST`,
`QWEATHER_DEVELOPER_ID`, `QWEATHER_PROJECT_ID`, `QWEATHER_CREDENTIAL_ID` and
`QWEATHER_PRIVATE_KEY_FILE` in `/etc/argus/qweather.env`. The Ed25519 PEM stays outside Git and uses a private
`/etc/argus` path in production because systemd has `ProtectHome=true`. JWTs
expire in 15 minutes and travel only in Authorization headers. No credential,
JWT, or identifier is returned by admin status. The probe API Key is not used
by the adapter. `qweather.py` isolates provider-specific parsing and signing;
the service and repository consume typed minute and warning records.

References: [authentication](https://dev.qweather.com/en/docs/configuration/authentication/),
[minute precipitation](https://dev.qweather.com/en/docs/api/minutely/minutely-precipitation/),
[hourly forecast](https://dev.qweather.com/en/docs/api/weather/weather-hourly-forecast/),
[official alerts](https://dev.qweather.com/en/docs/api/warning/weather-alert/).
QWeather data may be delayed; safety decisions must refer to the issuer's latest
warning. No AI call is used by the weather module. QWeather base polling is
about 304 requests/day (144 per minute/warning endpoint and about four astronomy
polls, each requiring three or four requests), rising to about 440/day with wet
minute polling, excluding bounded retries. Check account quota/billing rather
than assuming a shared conversation's free-tier figure is guaranteed.

The Open-Meteo forecast poll also requests optional air quality. Air-quality
failure is logged but does not fail the forecast. QWeather astronomy is checked
at startup, immediately after the subscribed location's local calendar date
changes, and every six hours, with one-hour retry on failure. A date rollover
does not bypass the request budget and a failed rollover attempt retains the
normal retry backoff. The independent channels also wake when their UTC daily
budget resets (08:00 in Beijing), so a skipped six-hour query does not leave a
morning gap after requests become available again. The Weather page explicitly
labels channel timestamps as their last query, not a forecast of rain/snow at
that time. Neither optional
provider has permission to generate a weather hazard or official warning. The
Weather page marks an old forecast as historical and shows the subscription
timezone; optional air quality expires after six hours and astronomy must match
the forecast's local date. A newly activated release can publish its first
daily forecast before independent optional polling finishes; those fields then
show as unavailable until the next normal daily report. This is not fabricated
as complete data. The daily notification uses a separate QWeather angle query
at the midpoint between local sunrise and sunset, an approximation of solar
noon and the day's highest solar elevation. It is not a mathematically exact
maximum; no noon angle is shown when the day length or angle response is invalid.
The admin also shows the separate poll-time elevation and azimuth. QWeather
angle requests use `alt=0`, a sea-level reference,
because subscription elevation is not currently measured or stored; displayed
solar elevation and azimuth are therefore approximations rather than site-survey
values. Each angle displays its own sample time. Schema-24 rows have no noon
sample until the next astronomy poll, without deriving one from an unrelated
poll-time angle.

## Operations and limitations

The Weather page shows last success, last error and consecutive failures. The
minute, official-alert and astronomy channels each have independent health
state; an astronomy failure no longer disappears from diagnostics. Both
notification toggles can be turned off in the admin; collection continues so
the page can show fresh data and failures. A direct provider outage or bad data
does not replace the last good forecast. The page's timestamp distinguishes it
from current data. For safety decisions, use official local alerts in addition
to EOS. Conditional hourly fallback is described below; there is no observed rain
gauge, no calibrated model voting, and no multi-location delivery routing.

Tests: `tests/test_weather.py` covers schema migration, validation, late daily
delivery, independent astronomy failure/recovery, rain oscillation across restart, hazards, outage recovery, official
warning color upgrades, explicit cancellation and atomic failure rollback.
`tests/test_qweather.py` covers JWT signature/claims, bounded gzip, stale data,
typed warning chains, fixed provider hosts and incomplete configuration.
`tests/test_admin_auth_http.py` covers weather read/write authentication,
CSRF and viewer RBAC. Frontend tests and desktop/mobile E2E cover saving and
responsive controls. Production acceptance must check one real forecast poll,
correct local coordinates, current schema, no duplicate outbox rows, and the public
admin entry. `argus weather-test` creates a labelled snapshot through the normal
outbox without changing rain or daily state; use only on explicit operator request.

The admin Weather page also provides a Leaflet/OpenStreetMap map picker. It uses
the official `tile.openstreetmap.org` endpoint and sends the EOS origin as
Referer without the page path, as the tile usage policy requires. Both the
Nginx template and application static responses use
`Referrer-Policy: strict-origin-when-cross-origin`; `no-referrer` blocks tiles.
The location marker is bundled explicitly because Leaflet's default icon-path
inference is unreliable after production bundling. A map click updates the
draft coordinates without saving; the backend resolves the
coordinates' timezone before the operator can save. Only the latest coordinate
selection may update the timezone, so an older lookup cannot overwrite a newer
map click. If lookup fails, the previous timezone remains but the UI explicitly
requires review. Search and direct coordinate editing remain available if map
tiles are blocked. Failed tile loads show a fallback; a provider-supplied block
image can still return HTTP 200, so acceptance checks actual map pixels and
response headers, not only status codes. Direct coordinate edits also require
timezone review. Browser location is an
explicit user action: the browser permission prompt is requested on that click,
and a denied/timeout state explains how to retry site permissions. Saving a new
location starts a bounded foreground refresh poll, so the home page updates as
soon as the worker records a successful forecast rather than waiting for the
next hourly cycle. The page title includes the active location, and the daily
UV maximum (when supplied by the forecast) is shown with the other metrics.
The admin Content Security Policy permits images only from the official
OpenStreetMap tile host in addition to local/data images; no arbitrary image
origin is allowed. If the bounded foreground poll does not see a fresh forecast,
the page reports that collection continues in the background instead of claiming
the new location is ready. Tile loading failures do not block coordinate search
or saving.

Map and browser selections resolve the chosen WGS84 coordinates to a readable
nearby city/district name through the separate `PlaceProvider` port in
`places.py`. QWeather GeoAPI currently implements that port using the existing
JWT transport; its returned city-center coordinates are never substituted for
the pin. For the Shahe test coordinates, the real response names Changping in
Beijing, not a verified campus or shop. The operator can edit the name and
re-run lookup with the name-field button. The heading shows the saved name;
coordinates and timezone appear on a secondary line. Legacy generated
coordinate labels are shortened in the heading, without rewriting history.

`GET /api/weather/place-resolution?lat=...&lon=...` requires `settings:write`
and accepts only finite, in-range coordinates. A successful response includes
`label`, `timezone`, `provider` and `precision=administrative`. Without configured
or working QWeather, Open-Meteo resolves timezone and `label` is null; the UI
asks for manual naming. Total provider timeout is bounded (QWeather 5 seconds,
Open-Meteo fallback 12 seconds). There is no continuous reverse lookup or
background location tracking. Selection is debounced for 350 ms; superseded
responses and unmounted pages cannot alter the draft, and edits to name/timezone
made while a request is pending are preserved.

The admin unit loads the same optional `/etc/argus/qweather.env` as the daemon.
All users share a maximum of 60 uncached selections per rolling hour in the
single admin process. Cache keys round only for lookup reuse to six decimals;
saved coordinates retain the user's original precision. The process cache is
bounded to 128 entries: 24 hours for names, 60 seconds for timezone-only fallback.
HTTP 429 leaves manual naming/timezone available. Cache and request accounting
are disposable and reset on process restart; this is separate from persistent
background forecast budgets. No schema migration is required.
GeoAPI documentation: [coordinate city lookup](https://dev.qweather.com/en/docs/api/geoapi/city-lookup/).

The authenticated read-only `GET /api/weather/place-timezone?lat=...&lon=...`
remains available for timezone-only callers. It uses Open-Meteo's fixed-host forecast
endpoint with `timezone=auto` and a single current field. Coordinates are
strictly bounded before network access; an unavailable or invalid provider
timezone is an explicit failure and never silently reuses the previous zone.
New snapshots store the hourly window from the subscription's local 06:00
through the next day at 06:00 (end exclusive). Before local 06:00 the active
window starts at yesterday's 06:00. This is 24 hours in Asia/Shanghai; a
daylight-saving transition can make the elapsed window 23 or 25 hours.
`window_start_at`, `window_end_at` and `window_complete` describe the
same typed summary projection as `forecast_hours`, not a separate persistent
schedule or subscription. No schema change or additional API polling is needed.
The daily message and temperature reference lines use this window's available
hours; future precipitation/gust totals exclude hours before the current query.
The window's final 00:00-06:00 precipitation is separately summarized in the
daily message. The calendar date and astronomy remain tied to the observation's
local date, even before 06:00 when the weather window started yesterday.
Forecast hours before the current time are model data, not observations. They
are shown with muted precipitation bars. Missing past
hours, especially with the future-only QWeather fallback, remain blank with a
partial-coverage label; they are not filled from zero or another provider.
Before 06:00, Open-Meteo's current-day forecast can omit the prior day's
06:00-00:00 hours; this is expected partial coverage, not a failed collection.
The axis uses actual timestamps and distinguishes tomorrow's hours; missing
temperature hours break the line instead of inventing a connecting trajectory.
On narrow screens the timeline scrolls horizontally at a readable fixed plot
width rather than shrinking all hours and labels into tiny text.
Since 0.29.6, temperature is always shown even on dry days. `ForecastChart.tsx`
renders separate temperature (°C) and rain/snow (mm/h) plots on one time axis.
The temperature line and available-hour extrema use the same data; rain bars
do not share the temperature scale. Missing values stay blank and disconnected.
Dry/unknown precipitation has explicit text rather than an empty card. Clicking
the plot or using the labelled keyboard-accessible slider shows an hour's
temperature, phase, quantity and probability; slider changes also bring that
hour into view on phones. Refresh selects a valid hour from the replacement
snapshot. Historical forecasts are explicitly labelled. The overview highlights
current temperature, followed by the timeline, daily-life metrics and separate
sun/moon/calendar metrics; provider and subscription controls follow below.
`weatherPresentation.ts` holds shared time/phase formatting, without extra
requests, chart libraries or persistent presentation state.
Frontend unit and desktop/mobile E2E tests include dry, snow and partial forecasts;
review their screenshots before submitting, including both light and dark themes.
Older snapshots without window metadata retain the legacy rolling-24-hour view
until the next successful poll; existing notifications are not rewritten.
Rain-change alerts still use the remaining local calendar day, severe-weather
detectors still use their near-term horizons, and official/minute warnings are
unchanged. UV maxima, calendar and astronomy remain scoped to today's date.
Displayed times use the saved subscription timezone, never an unsaved form edit;
the calendar date label uses the local observation date, not the weather-window
start date. Existing saved notifications keep their original text; the next
successful weather poll produces the new window without a database migration.

## Saved notification details (0.28.0)

New weather notifications open `/events/<alert-id>` instead of the live weather
homepage. The authenticated notification API exposes the existing `rule_id`;
`weather.*` uses a dedicated weather-message reader. It shows the saved title,
complete body, creation/delivery times and delivery status once, with a
`天气主页` link to `/#/weather`. The ntfy secondary action uses the same label.
Both daily reports and temporary rain/snow, forecast-change, warning, outage,
recovery and test messages use this behavior. Login retains the detail URL.

The saved outbox/alert body is the source of truth, not the latest forecast or
new provider requests. Location changes and later forecasts do not overwrite
it. No new store, table, public API or duplicated notification history is added;
the normal alert retention policy (90 days by default) still applies. Removed
messages return the existing authenticated not-found response. A missing public
admin URL retains the existing weather-home target. Already-delivered ntfy
notifications retain their old link and are not resent merely to change it.

Rollback to 0.27.0 requires no schema downgrade: new summary metadata is
additive and old alert rows are unchanged. That version returns to the old
weather-home notification click and legacy hourly display. Existing reminder
repeat intervals remain unchanged in either direction.

## Upgrade plan and boundaries

The following order favors accuracy and timeliness per unit of cost. The first
two items are intentionally small and are now implemented; the remaining items
stay disabled until their acceptance evidence exists.

1. **Schedule and observability (implemented):** publish a missed same-day
   report on the next successful forecast and expose astronomy outage/recovery
   independently. No extra provider requests or model calls are introduced.
2. **Bounded provider fallback (implemented in 0.27.0):** query QWeather hourly
   only after a failed/invalid primary forecast or expiry of retained data when
   the primary channel cannot fetch. Persist issue time, horizon, metric units
   and source provenance; never average probabilities or reset notification
   dedupe. Disagreement cross-checking is not implemented.
3. **Alert completeness (later):** add typed AQI, heat, UV, visibility and
   official-warning detail detectors only with explicit thresholds, hysteresis,
   source timestamps and independent dedupe. Model AQI alone must not be called
   an official pollution warning.
4. **Measured accuracy (later):** require an independent station or sensor,
   seasonal/horizon buckets and months of shadow evaluation before changing
   provider weights. Forecast agreement is not ground truth.
5. **Multiple locations (later):** add location-scoped routing and per-location
   schedules only after the single-location state machine and notification
   contracts are stable; do not duplicate the weather worker per location.

The module deliberately does not add a message broker, local ML model, or a
second always-on forecast provider for the current single subscription. Those
would increase requests and failure surfaces without demonstrated benefit.

## Follow-up Work (Not Implemented)

- Detailed place search, reverse geocoding and browser accuracy UX: deferred
  until the operator supplies a map Key; see the location upgrade plan below.
- QWeather daily enrichment and disagreement cross-check: hourly fallback is
  implemented, but routine dual querying and forecast averaging are not.
- Full precipitation lifecycle: extend current near-term episode state with
  WATCH/APPROACHING/ENDING and bounded timing/intensity updates. Do not infer
  observed ACTIVE from a forecast. Acceptance: fixture replays for onset drift,
  rain/snow ambiguity and genuinely distinct later episodes.
- Explicit ECMWF/CMA/GFS comparison: verify model availability and run times
  before adding queries. Fused products are not independent votes. Acceptance:
  provenance, stale-model handling and evidence of improved decisions.
- Local accuracy learning: requires independent measured weather, location,
  season and horizon buckets, months of samples, and shadow evaluation before
  adjusting weights. Model agreement is not accuracy ground truth.
- Official-warning detail/history: further cover supersedes branches, area
  changes, downgrades and instructions, with replay fixtures and an auditable
  reader. Never infer cancellation from missing data or failed requests.
- Air-quality/heat/UV/visibility alerts and multiple locations: add typed
  detector/routing contracts with operator controls and tests. Air-quality
  display exists, but no such alert is currently generated by its estimate.

## Location Search Upgrade (Planned, Awaiting Key)

Research date: 2026-09-29. This is a deferred plan, not implemented or enabled.
The operator will obtain a Key later; do not purchase services, replace the map,
or change the active subscription as part of recording this plan.

Current search uses Open-Meteo's settlement-oriented geocoding API. A read-only
query for Beijing University of Posts and Telecommunications Shahe campus in
Chinese returned no results, while `Beijing` returned city results. The current
Leaflet/OpenStreetMap picker supports manual coordinates and QWeather
city/district naming, but does not provide detailed reverse geocoding.
Browser location requests currently allow a
ten-minute cached result and do not request high accuracy.

Prefer a Tencent proof of concept for personal use; keep Amap as an alternative.
Both document school/shop POI search and detailed address resolution, but neither
has been tested with a map Key for this project. Do not claim a coverage winner.

| Reviewed personal-developer allowance | Tencent | Amap |
| --- | --- | --- |
| Place search | 200/day | Search-category pool: 5,000/month |
| Input suggestions | 6,000/day | Shares the search-category pool |
| Forward/reverse geocoding | 6,000/day each | Basic-service pool: 150,000/month |
| Web map | Free for non-commercial use | Free quota subject to account terms |

Amap's reviewed terms specify one year of free monthly allowances from personal
verification. Tencent's reviewed page does not state that one-year limit;
neither snapshot guarantees permanent rights or an individual account's quota.
Verify current console limits, use eligibility, attribution, privacy, allowed
storage and use of place data with other weather providers before integration.
Use one provider's map and place results together unless cross-provider display
is explicitly permitted. Public Nominatim is not a drop-in autocomplete option:
its policy prohibits autocomplete and limits aggregate application traffic to
one request/second. Do not self-host a large POI database for this scope.

Implementation and acceptance:

1. Obtain the chosen provider's required browser-map and server API credentials
   as applicable. Keep secrets outside Git and admin responses; restrict browser
   credentials to the EOS origin and backend calls to authenticated users.
2. Test the Shahe campus, a shop, same-name places in different cities and an
   empty query result. Show candidate names, addresses and city context; never
   silently select the first match. Set explicit search/request limits.
3. Separate place search/reverse-geocoding adapters from `WeatherProvider`.
   Define coordinate-system metadata and a verified conversion path before
   mixing browser/OSM WGS84 with domestic-map GCJ-02. Preserve existing locations
   with explicit migration rules; do not guess their coordinate system.
4. Resolve map clicks to an address without snapping the user's pin to a nearby
   POI. Request fresh high-accuracy browser coordinates and show accuracy radius
   and timestamp for confirmation. A prior denial needs site-permission guidance,
   not a promise that the website can force another permission prompt. Never
   silently replace the chosen position with an IP-derived location.
5. Call place services only for user actions, with debouncing and allowed caching,
   not on weather polls. On quota exhaustion, timeout or blocked tiles, retain
   manual coordinates and the last saved location. Keep current RBAC, CSRF,
   revision checks and audit boundaries; do not add continuous location tracking.
6. Verify coordinate alignment, rapid-selection stale-response protection,
   save-triggered forecast refresh, failure states and secret redaction. Inspect
   actual desktop/mobile screenshots before PR. More precise place selection
   does not improve the forecast product's underlying spatial resolution.

Official references: [Tencent quotas and authorization](https://lbs.qq.com/quotaImprove),
[Tencent POI search](https://lbs.qq.com/service/webService/webServiceGuide/search/webServiceSearch),
[Tencent reverse geocoding](https://lbs.qq.com/service/webService/webServiceGuide/webServiceGcoder),
[Amap pricing](https://lbs.amap.com/upgrade),
[Amap terms](https://lbs.amap.com/home/terms/),
[Amap coordinate conversion](https://lbs.amap.com/api/webservice/guide/api/convert),
[Nominatim policy](https://operations.osmfoundation.org/policies/nominatim/).

## Provider Controls and Conditional Fallback (0.27.0)

The Weather page has a source budget panel; `GET /api/weather/providers` returns
policies, engine-reported configured state, current UTC budget day, requests,
last request/error and an explanatory status. `POST
/api/weather/providers/{provider}/{kind}` accepts `enabled`,
`interval_seconds` (60..86400), and `daily_budget` (1..10000).
It requires `settings:write`, origin and CSRF validation; writes are audited and
do not require a restart or change the managed config revision.

| Provider/channel | Default interval | Daily HTTP budget |
| --- | --- | --- |
| Open-Meteo forecast | 3600 s | 48 |
| Open-Meteo air_quality | 3600 s | 48 |
| QWeather minutely | 600 s; wet periods may shorten to 300 s | 288 |
| QWeather alerts | 600 s | 288 |
| QWeather astronomy | 21600 s | 24 |
| QWeather hourly fallback | 3600 s; conditional only | 24 |

All policies start enabled; credentials are still installed only by the host
operator. Unconfigured, disabled and exhausted channels are distinguishable;
budget refusal makes no network request and does not create a false provider
outage. Requests are atomically reserved before HTTP, survive restarts and reset
by UTC date, not the subscription timezone. Failed attempted requests count.
The cooldown's last request survives the UTC-day boundary as well as restart;
only the daily count resets, so midnight cannot immediately bypass the interval.
Astronomy reserves separately for each of its up to four HTTP requests; optional
angles are not one fictitious bundled call. Accounting is marshalled to the
service event loop, not written from network threads. Usage follows the normal
90-day retention policy. Operator-initiated place search/timezone lookup is not
part of background forecast quota accounting.

Hourly fallback calls `/v7/weather/24h` only when required and within its own
interval/budget. A valid primary forecast never causes a second hourly query.
The adapter validates source update time (at most two hours old), zoned timestamps,
contiguous hourly records, bounded numeric values and at least 23 hours of future
coverage. The primary product retains its 24-hour minimum. Probability is percent,
precipitation mm, temperature C and wind km/h; wind direction uses `wind360`, not
textual `windDir`. Snapshot provenance includes provider, units, issue time,
coverage and `conditions_basis=hourly_forecast`: the first hourly forecast is not
a current station measurement. QWeather does not supply gust in this product;
gust and missing UV/sun data are displayed as unavailable, not guessed from
sustained wind. Daily ranges from fallback describe its available forecast
hours, not measured full-day extrema.

Switching products does not reset daily/rain/hazard state. One daily date and one
dry-to-rain key are shared across providers, including restarts. Both products
failing retain the last good snapshot. A forecast failure does not disable the
independently budgeted air-quality, official-warning or minute channels.
Primary and hourly fallback use independent scheduling clocks: a healthy primary
does not trigger routine fallback requests; primary failure, disablement,
exhaustion or an active fallback snapshot keeps the conditional hourly clock
alive. UTC midnight prompts a budget re-evaluation without making a healthy
primary poll early.
Tests include parsing/freshness, actual HTTP counts, concurrency/migration,
budget exhaustion, product switching, independent intervals and restart dedupe.

Read-only acceptance probe on 2026-09-27 used the protected production JWT
configuration and current subscription coordinates: QWeather returned 24 valid
hourly records through the new adapter and passed the freshness/coverage checks.
This one manual probe did not write production state or emit notifications and
is outside the daemon's persisted request counter. Normal operation remains
conditional, not permanent dual-provider fetching.
