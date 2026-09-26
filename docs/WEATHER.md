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
26 permits an independently tracked QWeather astronomy channel.

Source review on 2026-09-26:

| Candidate | Result | Decision |
|---|---|---|
| Open-Meteo forecast and geocoding APIs | HTTPS JSON returned 72 hourly values for the Shahe coordinates. Forecast and place search worked from this host without a key. Free API terms allow non-commercial use below 10,000 calls/day and require CC BY 4.0 attribution. | Use for model forecast and location search. One hourly request is about 24/day; place searches are operator initiated. |
| QWeather minute precipitation, hourly v1 and official-alert v1 APIs | All three returned valid JSON; Ed25519 JWT separately verified. Minute precipitation returned 24 slots and a district lightning warning was present. | Use minute precipitation and official alerts. Hourly data was probed but is not yet integrated into decisions. |
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
then every hour after a successful fetch. Failure retries after 20 minutes. An
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
condition, today's temperature range, remaining-day predicted precipitation,
maximum rain probability, gusts, humidity, wind, sunrise/sunset, the day's
maximum UV index when Open-Meteo supplies it, Gregorian and lunar dates, and a
small verified set of fixed-date festivals. Fresh optional
air quality and matching-date QWeather moon data are appended when available;
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
`sqlite_weather.py` owns schema-26 state
and atomic outbox writes, and `service.py` owns scheduling. The admin API uses
the same repository boundary; settings writes require `settings:write`, origin
and CSRF validation, revision match, and an audit record. Read/search requires
an authenticated session. State is retained across restarts; changing location
or timezone resets the forecast and rain baseline. An ordinary settings edit
does not erase the day's sent-notification history.

## QWeather cooperation

QWeather is not an extra vote in a probability average. Open-Meteo retains the
daily/remaining-day forecast role. QWeather minute precipitation adds a short-term
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
an official cancellation. All three QWeather channels retry failures in 5 minutes;
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
about 296 requests/day (144 per minute/warning endpoint and about four astronomy
polls, each requiring three or four requests), rising to about 440/day with wet
minute polling, excluding bounded retries. Check account quota/billing rather
than assuming a shared conversation's free-tier figure is guaranteed.

The Open-Meteo forecast poll also requests optional air quality. Air-quality
failure is logged but does not fail the forecast. QWeather astronomy is checked
at startup and every six hours, with one-hour retry on failure. Neither optional
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
to EOS. This version has no alternate daily-forecast provider, no observed rain
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

The admin Weather page also provides a Leaflet/OpenStreetMap map picker. A map
click updates the draft coordinates without saving; the backend resolves the
coordinates' timezone before the operator can save. Only the latest coordinate
selection may update the timezone, so an older lookup cannot overwrite a newer
map click. If lookup fails, the previous timezone remains but the UI explicitly
requires review. Search and direct coordinate editing remain available if map
tiles are blocked. Direct coordinate edits also require timezone review. Browser location is an
explicit user action: the browser permission prompt is requested on that click,
and a denied/timeout state explains how to retry site permissions. Saving a new
location starts a bounded foreground refresh poll, so the home page updates as
soon as the worker records a successful forecast rather than waiting for the
next hourly cycle. The page title includes the active location, and the daily
UV maximum (when supplied by the forecast) is shown with the other metrics.
The admin Content Security Policy permits images only from the three explicit
OpenStreetMap tile hosts in addition to local/data images; no arbitrary image
origin is allowed. If the bounded foreground poll does not see a fresh forecast,
the page reports that collection continues in the background instead of claiming
the new location is ready. Tile loading failures do not block coordinate search
or saving.
The authenticated read-only `GET /api/weather/place-timezone?lat=...&lon=...`
resolves a map click to an IANA timezone using Open-Meteo's fixed-host forecast
endpoint with `timezone=auto` and a single current field. Coordinates are
strictly bounded before network access; an unavailable or invalid provider
timezone is an explicit failure and never silently reuses the previous zone.
The latest snapshot stores at most 48 hours of hourly records, and the page puts a 24-hour
precipitation timeline before the metrics. Bars identify rain, snow or sleet and
the overlaid line shows hourly temperature; absence of hourly records is shown
explicitly rather than inferred from the daily total.

## Upgrade plan and boundaries

The following order favors accuracy and timeliness per unit of cost. The first
two items are intentionally small and are now implemented; the remaining items
stay disabled until their acceptance evidence exists.

1. **Schedule and observability (implemented):** publish a missed same-day
   report on the next successful forecast and expose astronomy outage/recovery
   independently. No extra provider requests or model calls are introduced.
2. **Bounded provider cross-check (next):** optionally query QWeather hourly
   only when Open-Meteo is stale/invalid or when a configurable material rain or
   temperature disagreement is detected. Persist issue time, horizon and source
   provenance; never average probabilities and never allow disagreement to
   create duplicate alerts. Default remains off until quota and replay tests
   demonstrate a meaningful gain.
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

- QWeather hourly/daily cross-check and fallback: normalize units, product issue
  times and horizons; show disagreements without averaging probabilities.
  Acceptance: either provider can fail independently, with no duplicate daily
  rain-change alerts across products. The hourly endpoint is only probed today.
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
- Provider controls and budgets: persist configured/enabled state and request
  counts, expose per-channel polling and quota limits without exposing secrets.
  Acceptance: an unconfigured provider is explicitly shown as unconfigured,
  quota exhaustion degrades one channel without suppressing other channels.
  Currently credentials are configured by the host operator; admin toggles
  affect condition notifications, not collection or provider-health notices.
