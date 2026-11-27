# FIRMS fire detections pull action: design

**Date:** 2026-11-23
**Status:** Approved by Chris, 2026-11-23
**Reference:** `das/das/tracking/models/firms.py` on `develop` at `205274a44`,
which includes the ERA-13999 and ERA-14642 hotfixes (2026-11-16 and 11-17).

## Intent

Move EarthRanger's `FirmsPlugin` into this Gundi action runner, so sites get
NASA FIRMS fire detections through Gundi instead of through the ER tracking
app. The behaviour should match the plugin: read the NASA FIRMS VIIRS day files
for the site's region, keep only detections inside the site's boundaries, and
forward them.

## Goals

- One scheduled pull action that fetches, filters and forwards detections.
- Boundaries come from a spatial feature group on the connected EarthRanger
  site, picked from a dropdown, and stay in sync with ER.
- A drawn or pasted polygon in the config is the fallback when ER can't be read.
- Resume where the last run stopped, and never raise the same fire twice.

## Non-goals

- Creating the `firms_rep` event type on the ER site. The ER plugin creates it
  on first use; here it must already exist on the destination (see Open items).
- Migrating existing `FirmsPlugin` rows or their stored cursors.
- The plugin's `--backfill-days` recovery command.
- Infra provisioning in `gundi-integrations-v2-infra` (a follow-up step).

## Anti-goals

- Sending unfiltered detections. If no boundary can be resolved, the run sends
  nothing and says why in the activity log.
- Editing template boilerplate (`app/services/`, routers, settings scaffolding).
  All new code lives under `app/actions/`.

## How the ER plugin works today (the behaviour to preserve)

- **Products:** Suomi NPP stopped delivering on 2026-11-02. The plugin now
  selects a satellite product, default NOAA-20:

  | key | folder | filename prefix | data type |
  |---|---|---|---|
  | `noaa-20` | `noaa-20-viirs-c2` | `J1_VIIRS_C2` | `VJ114IMGTDL_NRT` |
  | `noaa-21` | `noaa-21-viirs-c2` | `J2_VIIRS_C2` | `VJ214IMGTDL_NRT` |
  | `suomi-npp` (retired) | `suomi-npp-viirs-c2` | `SUOMI_VIIRS_C2` | `VNP14IMGTDL_NRT` |

- **File:** `https://nrt3.modaps.eosdis.nasa.gov/api/v2/content/archives/FIRMS/<folder>/<region>/<prefix>_<region>_<data type>_<yyyyddd>.txt`
  with `Authorization: Bearer <app key>`. `<yyyyddd>` is the UTC year and
  day-of-year.
- **Which files:** always yesterday's and today's, because NASA keeps
  rewriting yesterday's for hours after UTC midnight.
- **Resume:** no byte ranges (the archive ignores `If-Range` and changes the
  etag on every append). Each day file has its own cursor: `etag`, byte
  `offset` just past the last complete line, `prefix_sha256` of the bytes
  before that offset, and the header `columns`. The request sends
  `If-None-Match` only when the cursor is resumable. 304 means unchanged; 200
  returns the whole file. If the file still starts with the hashed prefix, only
  lines after the offset are read; otherwise the day is re-read from the start.
  A half-written last line is left for the next run. A cursor advances only
  after all of that file's records are consumed.
- **Failures:** 404 is a missing day (normal). 401, 403, or an HTML login page
  in a 200 mean the app key is missing, expired or revoked. Any other status,
  including 206 and 416, is a fetch error. In every failure the cursor is left
  unchanged.
- **Parsing:** by header column name, exact field count. Required columns:
  latitude, longitude, acq_date, acq_time. `acq_time` is `HH:MM` or `HHMM`,
  UTC. Coordinates must be in range. Malformed lines are skipped and counted.
- **Processing level:** taken from the `version` column (`2.0NRT` gives `NRT`).
  Only `NRT` rows are accepted by default; `RT` and `URT` are skipped, and rows
  with an unparseable version are skipped.
- **Filter:** union of every feature geometry in the group (invalid ones
  repaired with a zero buffer); keep points the union contains.
- **Events:** a kept detection becomes a `firms_rep` event titled "FIRMS Fire
  Detected" when its confidence is in the alert levels (default `nominal`,
  `high`) and it is newer than the alert window (default 24 hours). The event
  location is the position rounded to 3 decimal places (about 110 m). Details:
  confidence, frp, bright_ti4, bright_ti5, scan, track, reported_latitude,
  reported_longitude (the unrounded position).
- **De-duplication:** no event is created if one already exists with the same
  acquisition time and a location within one rounding cell (0.001 degrees) on
  each axis. This absorbs NASA's intraday geolocation revisions while keeping
  adjacent 375 m pixels (about 0.0034 degrees apart) separate.
- **Observations:** every kept detection is also written as an observation on a
  per-region, per-product source, stored with `get_or_create(source,
  recorded_at)`. FIRMS times are to the minute, so only the first detection of
  each minute is kept. Nothing else in ER reads these (see Open items).
- **Run summary:** counters for files read, records read, skipped levels,
  malformed lines, header errors, resume resets, files not found, fetch errors,
  outside area, events created, events de-duplicated.
- **Schedule:** every hour, on the hour. *(Was every 15 minutes until 2026-11-25, changed to cut activity-log volume.)*
- **Regions:** Alaska, Australia_NewZealand, Canada, Central_America, Europe,
  Global, Northern_and_Central_Africa, Russia_Asia, SouthEast_Asia,
  South_America, South_Asia, Southern_Africa, USA_contiguous_and_Hawaii.

## Approaches for the boundary

1. **Picked: dropdown picks the group, runner reads ER directly.** A reference
   action on the ER runner fills the dropdown; each run the FIRMS runner reads
   that group's features from ER with the destination's credentials. Stays in
   sync with ER. Costs a change in two repos.
2. Reference action returns the geometry and the portal copies it into the
   config. Rejected: needs new portal behaviour and the copy goes stale.
3. Runner-to-runner call at run time. Rejected: no such plumbing exists.

Chris picked option 1 on 2026-11-23, with a config polygon as fallback.

## Design

### Boundary resolution (decided)

Each run resolves the boundary in this order and uses the first that works:

1. Live read of the selected spatial feature group from ER.
2. The last geometry successfully read from ER, kept in the runner's state.
3. The polygon drawn or pasted in the config.
4. None: skip the run, log a warning to the activity log, send nothing.

Steps 2 to 4 apply when no group is selected, there is no ER destination, ER is
unreachable or rejects the credentials, or the group is missing or empty.
Whenever a run uses step 2 or 3 because step 1 failed, it logs a warning naming
which boundary was used and why.

### Config the portal user sees

**Auth action**
- `use_own_key`: boolean, off by default. Off means the service's shared key
  (the `FIRMS_APP_KEY` environment variable) is used. *(added 2026-11-24)*
- `app_key`: NASA Earthdata app key, optional `SecretStr`, password widget.
  Required only when `use_own_key` is on. The auth action checks the key in
  use with a small request against today's `Global` NOAA-20 file,
  since region and product live on the pull config. An HTML body or 401/403
  means invalid; a 404 means the key was accepted. *(my call)*

**Pull action** (`PullActionConfiguration`, so it has `run_on_schedule`)
- `region`: dropdown of the 13 regions above.
- `satellite`: dropdown of NOAA-20 (default) and NOAA-21. Suomi NPP is not
  offered, since it no longer delivers. *(my call)*
- `spatial_feature_group`: optional. ER feature group id, annotated with
  `gundi:reference` `{action: "list_spatial_feature_groups", target:
  "destination", allow_free_text: true}`.
- `fallback_area`: optional. Rendered with the portal's existing `polygon`
  widget (`gundi-portal/src/shared/components/forms/SchemaFormTemplates/`
  `MapAreaWidget.tsx` and `polygon.ts`): draw a rectangle or polygon, or import
  pasted GeoJSON. The stored value is one GeoJSON Polygon geometry without
  holes, `{"type": "Polygon", "coordinates": [[[lng, lat], ...]]}`. A field
  typed "object" stores the geometry itself; any other type stores it as a
  JSON string. No MultiPolygon.
- `confidence_alert_levels`: multi-select of low / nominal / high, default
  nominal and high.
- `alert_window_hours`: default 24, minimum 6 (shorter saved values are read as 6).
- Validation: at least one of `spatial_feature_group` or `fallback_area`.

Not exposed, fixed at the plugin's defaults *(my call)*: accepted processing
levels (`NRT` only), unknown levels skipped, de-dupe precision of 3 decimals.

### Parts in this repo

- `app/actions/client.py`: FIRMS client. Plain arguments only (app key, region,
  product, cursors); httpx; raises its own exceptions (auth error, header
  error, fetch error); no retries and no template imports. Returns parsed
  records plus the updated cursors and counters.
- `app/actions/boundaries.py`: reads destination ER site URL and credentials
  through the Gundi client, fetches the group from ER, unions geometries with
  shapely, and implements the fallback order. Supports both ER auth types
  (token, username and password), following `_build_er_client` in
  `gundi-integration-earthranger`. *(my call; Africam's helper is token only.)*
- `app/actions/handlers.py`: `action_auth` and `action_pull_fire_detections`,
  the latter with `@crontab_schedule("0 * * * *")` and no `@activity_logger()`: it
  writes one completion record per run, with the result and no configuration.
  Retry, fallback and de-dupe orchestration live here. The run's counters go in
  the action result so they show in the portal.
- `app/actions/configurations.py`: the models above.
- `requirements.in`: `shapely`, `earthranger-client`.
- Tests beside the code, with `respx` for NASA and ER.

### State

Per integration:
- Day-file cursors, per product and day index, for yesterday and today only.
- The cached boundary geometry per ER destination, with the group id it came
  from. A cached geometry for a different group id than the one configured is
  ignored.
- De-dupe keys for events already sent (below).

### De-duplication without ER's database *(my call)*

The plugin de-dupes by querying ER events. The runner can't, so it keeps its
own record: for each event sent, the acquisition time and the position rounded
to 3 decimals. Before sending, it checks that key and its eight neighbouring
cells, which gives the plugin's "within one cell" rule. Keys older than the
two-day file window plus the alert window are dropped. This also covers a day
file re-read from the start, and overlapping runs.

### Output

A `firms_rep` event per confident, recent, not-yet-sent detection: title "FIRMS
Fire Detected", time is the acquisition time, location rounded to 3 decimals,
the eight detail fields above. No observations are sent (decided, see Open
items).

### With several ER destinations *(my call)*

The group id is looked up on each ER destination. Geometries from every
destination that has it are unioned into one boundary for the connection.
The cached geometry is kept per destination, and the fallback order runs per
destination: one that can't be read live contributes its cached geometry. The
config polygon is used only when no destination yields a geometry.

### Change in `gundi-integration-earthranger` (separate PR)

Add reference action `list_spatial_feature_groups`: lists the site's feature
groups from ER's `featuregroups/` endpoint; option value is the group id, label
is the name, description is the feature count. Follows the existing
`action_list_event_types` pattern. Until it ships, the portal field degrades to
free text and an id can be typed.

## Left to the builder

- Which ER endpoint to read a group's features from. ER serves
  `featuregroup/<id>` (detail, includes features); `earthranger-client`'s
  `get_feature_group` calls `spatialfeaturegroup/<id>`. Verify against a live
  site and call the endpoint directly if the client method is wrong.
- Where the de-dupe keys live (the action state or a Redis set with expiry).
- Batch sizes for sending to Gundi and exact exception names.

## Open items

- **Observations as well as events?** The plugin writes both, but the
  observations look like a leftover: the plugin began in 2015 as a tracking
  plugin, a framework whose output is observations; only the first detection
  per minute survives; and nothing else in ER reads the `firms` source type.
  **Decided 2026-11-23 (Chris): events only, no observations.**
- **`firms_rep` on the destination.** New ER sites that never ran the plugin
  don't have this event type, and older ones may lack the
  `reported_latitude` / `reported_longitude` fields the plugin now adds.
  Someone has to create or update it there, or ER will reject the events.
