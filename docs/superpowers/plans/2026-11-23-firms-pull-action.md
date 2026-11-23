# FIRMS Pull Action Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A scheduled pull action that reads NASA FIRMS VIIRS day files, keeps detections inside the site's boundary, and sends `firms_rep` events to Gundi.

**Architecture:** A pure NASA client (`client.py`) returns parsed records and a per-day cursor. Pure geometry and de-dupe modules sit beside it. A boundary resolver reads the feature group from the connection's EarthRanger destinations, with cached and config-polygon fallbacks. The handler is the only place that touches config models, Gundi, and state.

**Tech Stack:** Python 3.10, pydantic 1.10, httpx, shapely 2, earthranger-client, pytest + pytest-asyncio + respx.

**Spec:** `docs/superpowers/specs/2026-11-23-firms-pull-design.md`. Reference implementation: `/Users/chrisdo/padas/das/das/tracking/models/firms.py` (`develop` at `205274a44`).

## Global Constraints

- Work on a branch `firms-pull-action`, not `main`.
- Only add or edit files under `app/actions/`, `requirements*.in`, the two lock files, `README.md` and `docs/`. Do not edit `app/services/`, routers or settings.
- `app/actions/client.py` imports nothing from `app.*` and takes plain arguments only. It raises its own exceptions and does no retries.
- Credentials in config models are `pydantic.SecretStr`.
- Never send a detection when no boundary resolved.
- Events only. No observations.
- Fixed values: event type `firms_rep`; title `FIRMS Fire Detected`; location rounded to 3 decimals; accepted processing level `NRT` only; schedule `*/15 * * * *`; host `nrt3.modaps.eosdis.nasa.gov`.
- Python 3.10 syntax (no `type X = ...` statements); pydantic v1 validators.
- Run tests with `pytest <path> -v` from the repo root, inside a 3.10 virtualenv with `requirements-dev.txt` installed.

## Review Focus

1. A day file that is empty, header-only, or ends without a newline: no records, no crash, half-written last line left for the next run. (Task 2)
2. Sending to Gundi fails part-way through a day: that day's cursor is not advanced and unsent detections are not marked as sent, so the next run retries them. (Task 6)
3. A feature group whose features are all empty, invalid beyond repair, or not polygons: treated as "no geometry from this destination" and the fallback order continues. (Tasks 3, 4)
4. A destination that is not EarthRanger or has no usable credentials, or ER auth by username and password: skipped or supported, never fatal to the run. (Task 4)
5. UTC midnight rollover: cursors and sent-event keys for days that left the window are dropped, and yesterday's file is still read. (Tasks 2, 5, 6)

## File Structure

| File | Responsibility |
|---|---|
| `app/actions/client.py` | NASA FIRMS: products, URLs, parsing, cursor-validated fetch, app key check |
| `app/actions/boundaries.py` | Pure shapely helpers: GeoJSON in, union, contains |
| `app/actions/boundary_source.py` | Read ER destinations and feature groups; fallback order |
| `app/actions/dedupe.py` | Record of sent events and the "within one cell" rule |
| `app/actions/configurations.py` | `AuthenticateConfig`, `PullFireDetectionsConfig` |
| `app/actions/handlers.py` | `action_auth`, `action_pull_fire_detections` |
| `app/actions/tests/test_*.py` | One test file per module above |

---

### Task 1: Dependencies and FIRMS parsing

**Files:**
- Modify: `requirements.in`, `requirements-test.in`, `requirements.txt`, `requirements-dev.txt`
- Create: `app/actions/client.py`
- Test: `app/actions/tests/test_client_parsing.py`

**Interfaces:**
- Produces (all in `app.actions.client`):
  - `FirmsProduct` frozen dataclass: `key, folder, filename_prefix, data_type_and_name`
  - `FIRMS_PRODUCTS: dict[str, FirmsProduct]` with keys `"noaa-20"`, `"noaa-21"`; `DEFAULT_FIRMS_PRODUCT = "noaa-20"`
  - `FIRMS_REGIONS: tuple[str, ...]` (the 13 names in the spec)
  - `class FirmsError(Exception)`, `FirmsAuthError(FirmsError)`, `FirmsFetchError(FirmsError)` with `status_code: Optional[int]`, `FirmsHeaderError(FirmsError)`
  - `date_index(d: datetime) -> int`
  - `window_date_indexes(now: datetime) -> list[int]` (yesterday, today)
  - `file_url(region: str, product: FirmsProduct, date_index: int) -> str`
  - `parse_header(line: str) -> tuple[str, ...]`
  - `parse_line(line: str, columns: Sequence[str]) -> dict` (raises `ValueError`)
  - `processing_level(version: Any) -> Optional[str]`

- [ ] **Step 1: Branch, virtualenv, dependencies**

```bash
git checkout -b firms-pull-action
python3.10 -m venv .venv && . .venv/bin/activate && pip install pip-tools
```

Append `shapely~=2.0.7` and `earthranger-client>=1.15.0,<2.0.0` to `requirements.in`; append `respx` to `requirements-test.in`. Then:

```bash
pip-compile --output-file=requirements.txt requirements-base.in requirements.in
pip-compile --output-file=requirements-dev.txt requirements-dev.in requirements-test.in
pip install -r requirements-dev.txt
pytest -q
```

Expected: the existing template suite passes.

- [ ] **Step 2: Write the failing tests**

```python
from datetime import datetime, timezone
import pytest
from app.actions import client as c

NOAA20 = c.FIRMS_PRODUCTS["noaa-20"]
HEADER = "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight"
ROW = "-1.25031,35.12345,338.1,0.43,0.46,2026-11-23,00:42,N20,nominal,2.0NRT,281.5,1.9,N"

def test_products():
    assert (NOAA20.folder, NOAA20.filename_prefix, NOAA20.data_type_and_name) == ("noaa-20-viirs-c2", "J1_VIIRS_C2", "VJ114IMGTDL_NRT")
    n21 = c.FIRMS_PRODUCTS["noaa-21"]
    assert (n21.folder, n21.filename_prefix, n21.data_type_and_name) == ("noaa-21-viirs-c2", "J2_VIIRS_C2", "VJ214IMGTDL_NRT")
    assert set(c.FIRMS_PRODUCTS) == {"noaa-20", "noaa-21"}
    assert len(c.FIRMS_REGIONS) == 13 and "Southern_Africa" in c.FIRMS_REGIONS

def test_date_index_and_window():
    now = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
    assert c.date_index(now) == 2026001
    assert c.window_date_indexes(now) == [2025365, 2026001]

def test_file_url():
    assert c.file_url("Southern_Africa", NOAA20, 2026327) == (
        "https://nrt3.modaps.eosdis.nasa.gov/api/v2/content/archives/FIRMS/noaa-20-viirs-c2/"
        "Southern_Africa/J1_VIIRS_C2_Southern_Africa_VJ114IMGTDL_NRT_2026327.txt")

def test_parse_line():
    rec = c.parse_line(ROW, c.parse_header(HEADER))
    assert rec["latitude"] == -1.25031 and rec["frp"] == 1.9 and rec["confidence"] == "nominal"
    assert rec["recorded_at"] == datetime(2026, 11, 23, 0, 42, tzinfo=timezone.utc)

def test_parse_line_accepts_hhmm():
    rec = c.parse_line(ROW.replace("00:42", "0042"), c.parse_header(HEADER))
    assert rec["recorded_at"].minute == 42

@pytest.mark.parametrize("bad", [
    ROW + ",extra",                       # wrong field count
    ROW.replace("-1.25031", "91.0"),      # latitude out of range
    ROW.replace("00:42", "0:42"),         # bad acq_time shape
    ROW.replace("00:42", "24:00"),        # hour out of range
    ROW.replace("338.1", "n/a"),          # non-numeric float column
])
def test_parse_line_rejects(bad):
    with pytest.raises(ValueError):
        c.parse_line(bad, c.parse_header(HEADER))

def test_parse_header_requires_columns():
    assert c.parse_header(" Latitude , LONGITUDE,acq_date,acq_time\r") == ("latitude", "longitude", "acq_date", "acq_time")
    with pytest.raises(c.FirmsHeaderError):
        c.parse_header("latitude,longitude,acq_date")

@pytest.mark.parametrize("version,level", [("2.0NRT", "NRT"), (" 2.0URT ", "URT"), ("1.0rt", "RT"), ("2.0", None), (None, None), ("", None)])
def test_processing_level(version, level):
    assert c.processing_level(version) == level
```

- [ ] **Step 3: Run to verify failure**

Run: `pytest app/actions/tests/test_client_parsing.py -v`
Expected: FAIL, `app.actions.client` has none of these names.

- [ ] **Step 4: Implement the names in the Interfaces block**

Port from the reference `FirmsClient` (`calculate_date_index`, `window_date_indexes`, `file_url`, `parse_header`, `_parse_line`) and `FirmsPlugin.processing_level`, as module functions. Float columns: latitude, longitude, bright_ti4, bright_ti5, scan, track, frp. Leave out the `suomi-npp` product.

- [ ] **Step 5: Run to verify pass, then commit**

Run: `pytest app/actions/tests/test_client_parsing.py -v` — Expected: PASS

```bash
git add requirements*.in requirements*.txt app/actions/client.py app/actions/tests/test_client_parsing.py
git commit -m "Add FIRMS products, URLs and row parsing"
```

---

### Task 2: Cursor-validated day fetch and app key check

**Files:**
- Modify: `app/actions/client.py`
- Test: `app/actions/tests/test_client_fetch.py`

**Interfaces:**
- Consumes: Task 1 names.
- Produces (in `app.actions.client`):
  - `@dataclass FirmsStats`: int counters `files_read, records_read, malformed_lines, resume_resets, files_not_found`, all default 0
  - `@dataclass DayResult`: `records: list[dict]`, `cursor: Optional[dict]`
  - `async def fetch_day(*, app_key: str, region: str, product: FirmsProduct, date_index: int, cursor: Optional[dict] = None, stats: Optional[FirmsStats] = None, timeout: float = 60.0) -> DayResult`
  - `async def check_app_key(app_key: str, *, now: Optional[datetime] = None, timeout: float = 30.0) -> bool`
- Cursor shape: `{"etag": str | None, "offset": int, "prefix_sha256": str, "columns": list[str]}`.

Behaviour of `fetch_day` (port of `_read_day`, `_fetch_whole_file`, `_start_of_unread`, `_validated_resume_point`):

| Response | Result |
|---|---|
| 304 | `DayResult([], cursor)` (same cursor back) |
| 404 | `DayResult([], cursor)`, `files_not_found += 1` |
| 200, body has no newline yet | `DayResult([], cursor)` |
| 200 | records from the unread complete lines; new cursor |
| 401, 403, any 3xx, or `<!DOCTYPE html>` in a 200 body | raise `FirmsAuthError` |
| any other status, incl. 206 and 416 | raise `FirmsFetchError(status_code=...)` |
| `httpx.HTTPError` (transport) | raise `FirmsFetchError(status_code=None)` |
| header missing a required column | raise `FirmsHeaderError` |

Requests use `Authorization: Bearer <app_key>`, `follow_redirects=False`, and `If-None-Match: <etag>` only when the cursor is resumable (int `offset > 0`, non-empty `prefix_sha256` and `columns`) and has an etag. A cursor whose prefix hash or columns no longer match the file means re-read from after the header and `resume_resets += 1`. Malformed lines are skipped and counted.

`check_app_key` streams a GET for today's `Global` NOAA-20 file and reads at most the first 1024 bytes. Returns `True` for 404, or 200 without `<!DOCTYPE html>`. Returns `False` for 401, 403, 3xx, or an HTML body. Raises `FirmsFetchError` otherwise.

- [ ] **Step 1: Write the failing tests** (respx; `URL = c.file_url("Southern_Africa", NOAA20, 2026327)`; `HEADER` and `ROW` as in Task 1; `ROW2` a second valid row; `BODY = f"{HEADER}\n{ROW}\n"`)

```python
async def fetch(cursor=None, stats=None):
    return await c.fetch_day(app_key="k", region="Southern_Africa", product=NOAA20, date_index=2026327, cursor=cursor, stats=stats)

@respx.mock
@pytest.mark.asyncio
async def test_first_read_returns_records_and_cursor():
    route = respx.get(URL).respond(200, text=BODY, headers={"ETag": '"a"'})
    stats = c.FirmsStats()
    result = await fetch(stats=stats)
    assert [r["latitude"] for r in result.records] == [-1.25031]
    assert result.cursor == {"etag": '"a"', "offset": len(BODY.encode()),
                             "prefix_sha256": hashlib.sha256(BODY.encode()).hexdigest(),
                             "columns": list(c.parse_header(HEADER))}
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer k" and "If-None-Match" not in req.headers and "Range" not in req.headers
    assert (stats.files_read, stats.records_read) == (1, 1)

@respx.mock
@pytest.mark.asyncio
async def test_resume_reads_only_appended_lines():
    respx.get(URL).respond(200, text=BODY, headers={"ETag": '"a"'})
    first = await fetch()
    route = respx.get(URL).respond(200, text=BODY + ROW2 + "\n", headers={"ETag": '"b"'})
    second = await fetch(cursor=first.cursor)
    assert len(second.records) == 1 and second.cursor["etag"] == '"b"'
    assert route.calls.last.request.headers["If-None-Match"] == '"a"'

@respx.mock
@pytest.mark.asyncio
async def test_changed_prefix_rereads_whole_day():
    respx.get(URL).respond(200, text=BODY, headers={"ETag": '"a"'})
    first = await fetch()
    respx.get(URL).respond(200, text=f"{HEADER}\n{ROW2}\n{ROW}\n", headers={"ETag": '"b"'})
    stats = c.FirmsStats()
    second = await fetch(cursor=first.cursor, stats=stats)
    assert len(second.records) == 2 and stats.resume_resets == 1

@respx.mock
@pytest.mark.asyncio
async def test_half_written_last_line_is_left_for_next_run():
    respx.get(URL).respond(200, text=BODY + ROW2[:20])
    result = await fetch()
    assert len(result.records) == 1 and result.cursor["offset"] == len(BODY.encode())

@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["", HEADER, HEADER + "\n"])
async def test_empty_or_header_only_file(body):
    respx.get(URL).respond(200, text=body)
    assert (await fetch()).records == []

@respx.mock
@pytest.mark.asyncio
async def test_malformed_line_is_skipped_and_counted():
    respx.get(URL).respond(200, text=f"{HEADER}\nnot,a,row\n{ROW}\n")
    stats = c.FirmsStats()
    assert len((await fetch(stats=stats)).records) == 1 and stats.malformed_lines == 1

@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("status", [304, 404])
async def test_unchanged_or_missing_keeps_cursor(status):
    cur = {"etag": '"a"', "offset": 5, "prefix_sha256": "x", "columns": ["latitude"]}
    respx.get(URL).respond(status)
    result = await fetch(cursor=cur)
    assert result.records == [] and result.cursor == cur

@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,exc", [
    (401, "", c.FirmsAuthError), (403, "", c.FirmsAuthError), (303, "", c.FirmsAuthError),
    (200, "<!DOCTYPE html><html>login</html>", c.FirmsAuthError),
    (206, BODY, c.FirmsFetchError), (416, "", c.FirmsFetchError), (500, "", c.FirmsFetchError),
    (200, "latitude,longitude\n1,2\n", c.FirmsHeaderError),
])
async def test_failures_raise(status, body, exc):
    respx.get(URL).respond(status, text=body)
    with pytest.raises(exc):
        await fetch()

@respx.mock
@pytest.mark.asyncio
async def test_transport_error_is_fetch_error():
    respx.get(URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(c.FirmsFetchError) as e:
        await fetch()
    assert e.value.status_code is None

@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("status,body,valid", [(200, BODY, True), (404, "", True), (401, "", False), (303, "", False), (200, "<!DOCTYPE html>", False)])
async def test_check_app_key(status, body, valid):
    now = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
    respx.get(c.file_url("Global", NOAA20, 2026327)).respond(status, text=body)
    assert await c.check_app_key("k", now=now) is valid
```

- [ ] **Step 2: Run to verify failure** — `pytest app/actions/tests/test_client_fetch.py -v`; Expected: FAIL, `fetch_day` not defined.
- [ ] **Step 3: Implement `FirmsStats`, `DayResult`, `fetch_day`, `check_app_key`** per the table above, with `httpx.AsyncClient`.
- [ ] **Step 4: Run to verify pass** — same command; Expected: PASS.
- [ ] **Step 5: Commit**

```bash
git add app/actions/client.py app/actions/tests/test_client_fetch.py
git commit -m "Fetch FIRMS day files with validated resume cursors"
```

---

### Task 3: Boundary geometry helpers

**Files:**
- Create: `app/actions/boundaries.py`
- Test: `app/actions/tests/test_boundaries.py`

**Interfaces:**
- Produces (in `app.actions.boundaries`):
  - `class BoundaryError(ValueError)`
  - `geometry_from_feature_group(group: dict) -> BaseGeometry` — `group["features"]` is a list whose items are GeoJSON FeatureCollections (ER serialises each feature as a one-feature collection) or bare Features. Keeps Polygon and MultiPolygon geometries only, repairs invalid ones with `buffer(0)`, skips ones that still fail, unions the rest. Raises `BoundaryError` when nothing usable remains.
  - `geometry_from_polygon(value: Union[dict, str]) -> BaseGeometry` — a GeoJSON Polygon geometry as a dict or a JSON string. Raises `BoundaryError` for anything else.
  - `union_geometries(geometries: Iterable[BaseGeometry]) -> BaseGeometry` — raises `BoundaryError` when empty.
  - `to_geojson(geometry: BaseGeometry) -> dict`, `from_geojson(value: dict) -> BaseGeometry`
  - `make_filter(geometry: BaseGeometry) -> Callable[[float, float], bool]` — returns `inside(lat, lon)`, using a prepared geometry and `contains`.

- [ ] **Step 1: Write the failing tests**

```python
SQUARE = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}
SQUARE2 = {"type": "Polygon", "coordinates": [[[2, 2], [2, 3], [3, 3], [3, 2], [2, 2]]]}
BOWTIE = {"type": "Polygon", "coordinates": [[[5, 5], [6, 6], [6, 5], [5, 6], [5, 5]]]}
LINE = {"type": "LineString", "coordinates": [[0, 0], [9, 9]]}

def fc(geom): return {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": geom}]}

def test_group_unions_features():
    inside = b.make_filter(b.geometry_from_feature_group({"features": [fc(SQUARE), fc(SQUARE2)]}))
    assert inside(0.5, 0.5) and inside(2.5, 2.5) and not inside(1.5, 1.5)

def test_filter_argument_order_is_lat_lon():
    wide = {"type": "Polygon", "coordinates": [[[30, 0], [30, 1], [40, 1], [40, 0], [30, 0]]]}
    inside = b.make_filter(b.geometry_from_polygon(wide))
    assert inside(0.5, 35.0) and not inside(35.0, 0.5)

def test_invalid_feature_is_repaired_and_lines_ignored():
    geom = b.geometry_from_feature_group({"features": [fc(BOWTIE), fc(LINE), {"type": "Feature", "geometry": SQUARE}]})
    assert geom.is_valid and b.make_filter(geom)(0.5, 0.5)

@pytest.mark.parametrize("group", [{}, {"features": []}, {"features": [fc(LINE)]}, {"features": [fc(None)]}])
def test_group_without_usable_polygons_raises(group):
    with pytest.raises(b.BoundaryError):
        b.geometry_from_feature_group(group)

def test_polygon_from_dict_or_string():
    assert b.geometry_from_polygon(SQUARE).equals(b.geometry_from_polygon(json.dumps(SQUARE)))

@pytest.mark.parametrize("bad", ["", "not json", "[1,2,3,4]", {"type": "Point", "coordinates": [0, 0]}, {"typed_edges": ["1", "2"]}, None])
def test_polygon_rejects(bad):
    with pytest.raises(b.BoundaryError):
        b.geometry_from_polygon(bad)

def test_geojson_round_trip():
    geom = b.geometry_from_polygon(SQUARE)
    assert b.from_geojson(json.loads(json.dumps(b.to_geojson(geom)))).equals(geom)
```

- [ ] **Step 2: Run to verify failure** — `pytest app/actions/tests/test_boundaries.py -v`; Expected: FAIL, module missing.
- [ ] **Step 3: Implement the Interfaces block** with `shapely.geometry.shape`, `mapping`, `shapely.ops.unary_union`, `shapely.prepared.prep`.
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit**

```bash
git add app/actions/boundaries.py app/actions/tests/test_boundaries.py
git commit -m "Add boundary geometry helpers for FIRMS filtering"
```

---

### Task 4: Boundary from EarthRanger, with fallbacks

**Files:**
- Create: `app/actions/boundary_source.py`
- Test: `app/actions/tests/test_boundary_source.py`

**Interfaces:**
- Consumes: `app.actions.boundaries` (Task 3).
- Produces (in `app.actions.boundary_source`):
  - `@dataclass ERDestination`: `id: str, base_url: str, token: Optional[str] = None, username: Optional[str] = None, password: Optional[str] = None`
  - `async def get_er_destinations(integration_id: str) -> list[ERDestination]` — uses `gundi_client_v2.GundiClient`: `get_connection_details(integration_id)`, then `get_integration_details(dest.id)` and `get_action_config("auth")` for each destination, reading `data["token"]`, `data["username"]`, `data["password"]`. A destination with no auth config, or with neither a token nor a username and password, is left out. Returns `[]` when the connection has no destinations or the connection lookup answers 404.
  - `async def fetch_feature_group(destination: ERDestination, group_id: str) -> dict` — builds `erclient.AsyncERClient(service_root=f"{site_root}/api/v1.0", token=..., username=..., password=..., token_url=f"{site_root}/oauth2/token", client_id="das_web_client")`, where `site_root` is the scheme and host of `base_url`, and returns `await client.get_feature_group(group_id)`.
  - `@dataclass ResolvedBoundary`: `geometry: Optional[BaseGeometry]`, `source: str` (`"earthranger"`, `"cached"`, `"config"` or `"none"`), `warnings: list[str]`, `cache: dict`
  - `async def resolve_boundary(*, integration_id: str, group_id: Optional[str], fallback_area: Union[dict, str, None], cache: dict) -> ResolvedBoundary`
- Cache shape: `{destination_id: {"group_id": str, "geometry": <GeoJSON dict>}}`.

Rules for `resolve_boundary`:
1. If `group_id` is set, for each destination: try the live read and `geometry_from_feature_group`; on success store it in the returned cache. On any exception, use that destination's cache entry if its `group_id` matches, and add a warning naming the destination's host and the exception class (never the exception text, which can carry credentials). Otherwise the destination contributes nothing, with a warning.
2. Union what the destinations contributed. `source` is `"earthranger"` if every contribution was live, `"cached"` if any was cached.
3. If nothing was contributed, use `fallback_area` through `geometry_from_polygon`; `source = "config"`. Add a warning that the fallback area is in use, unless `group_id` was never set.
4. If that also fails or is absent, `geometry=None`, `source="none"`.
5. A failure of `get_er_destinations` itself counts as "no destinations" plus a warning. Cache entries for a different `group_id` are dropped from the returned cache; entries for destinations not seen this run are kept.

- [ ] **Step 1: Write the failing tests** (patch `boundary_source.get_er_destinations` and `boundary_source.fetch_feature_group` with `mocker.patch(..., new=AsyncMock(...))`; `GROUP = {"features": [fc(SQUARE)]}`; `A = ERDestination("a", "https://a.pamdas.org", token="t")`, `B` likewise for `b`)

```python
async def resolve(group_id="g1", fallback=None, cache=None):
    return await bs.resolve_boundary(integration_id="i", group_id=group_id, fallback_area=fallback, cache=cache or {})

async def test_live_read_wins_and_is_cached(dests, fetch):          # dests -> [A]; fetch -> GROUP
    r = await resolve(fallback=SQUARE2)
    assert r.source == "earthranger" and r.warnings == []
    assert r.cache == {"a": {"group_id": "g1", "geometry": b.to_geojson(r.geometry)}}
    assert b.make_filter(r.geometry)(0.5, 0.5)

async def test_unreachable_destination_uses_its_cache(dests, fetch):  # fetch raises httpx.ConnectError("secret-token")
    cache = {"a": {"group_id": "g1", "geometry": SQUARE}}
    r = await resolve(cache=cache, fallback=SQUARE2)
    assert r.source == "cached" and b.make_filter(r.geometry)(0.5, 0.5)
    assert len(r.warnings) == 1 and "a.pamdas.org" in r.warnings[0] and "secret-token" not in r.warnings[0]
    assert r.cache == cache

async def test_cache_for_another_group_is_ignored(dests, fetch):      # fetch raises
    r = await resolve(cache={"a": {"group_id": "old", "geometry": SQUARE}}, fallback=SQUARE2)
    assert r.source == "config" and b.make_filter(r.geometry)(2.5, 2.5) and "a" not in r.cache

async def test_two_destinations_union_live_and_cached(dests, fetch):  # dests -> [A, B]; A ok, B raises
    r = await resolve(cache={"b": {"group_id": "g1", "geometry": SQUARE2}})
    inside = b.make_filter(r.geometry)
    assert r.source == "cached" and inside(0.5, 0.5) and inside(2.5, 2.5)

async def test_group_with_no_polygons_falls_back(dests, fetch):       # fetch -> {"features": []}
    r = await resolve(fallback=json.dumps(SQUARE2))
    assert r.source == "config" and r.warnings

async def test_no_group_selected_uses_config_without_warning(dests, fetch):
    r = await resolve(group_id=None, fallback=SQUARE2)
    assert r.source == "config" and r.warnings == [] and fetch.await_count == 0

async def test_nothing_resolves(dests, fetch):                        # dests -> []
    r = await resolve(fallback=None)
    assert r.geometry is None and r.source == "none"

async def test_destination_lookup_failure_is_not_fatal(dests, fetch): # dests raises RuntimeError
    r = await resolve(fallback=SQUARE2)
    assert r.source == "config" and r.warnings
```

Also test `get_er_destinations` with a mocked `GundiClient`: three destinations (token auth, username and password auth, one with no auth config) return two `ERDestination`s with the right fields; a connection with no destinations returns `[]`.

- [ ] **Step 2: Run to verify failure** — `pytest app/actions/tests/test_boundary_source.py -v`; Expected: FAIL, module missing.
- [ ] **Step 3: Implement the Interfaces block.** If `get_feature_group` (ER path `spatialfeaturegroup/<id>`, deprecated but served) is rejected by a live site, switch to `client._get(f"featuregroup/{group_id}")`; both return the same `features` shape.
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit**

```bash
git add app/actions/boundary_source.py app/actions/tests/test_boundary_source.py
git commit -m "Resolve the FIRMS boundary from EarthRanger with cached and config fallbacks"
```

---

### Task 5: Sent-event record

**Files:**
- Create: `app/actions/dedupe.py`
- Test: `app/actions/tests/test_dedupe.py`

**Interfaces:**
- Produces (in `app.actions.dedupe`):
  - `LOCATION_PRECISION = 3`
  - `class SentEvents`:
    - `__init__(self, keys: Optional[Iterable[str]] = None)`
    - `seen(self, recorded_at: datetime, latitude: float, longitude: float) -> bool` — true when the same minute has a recorded key in the same cell or any of the eight neighbouring cells
    - `add(self, recorded_at: datetime, latitude: float, longitude: float) -> None`
    - `prune(self, before: datetime) -> None` — drop keys whose time is earlier than `before`
    - `to_state(self) -> list[str]` — sorted
- Key format: `"<UTC ISO minute>|<round(lat*1000)>|<round(lon*1000)>"`, e.g. `"2026-11-23T00:42+00:00|-1250|35123"`. Cells are integers so neighbours are exact.

- [ ] **Step 1: Write the failing tests**

```python
T = datetime(2026, 11, 23, 0, 42, tzinfo=timezone.utc)

def test_same_and_revised_position_are_seen():
    s = SentEvents(); s.add(T, -1.25031, 35.12345)
    assert s.seen(T, -1.25031, 35.12345)
    assert s.seen(T, -1.25049, 35.12351)          # same cell
    assert s.seen(T, -1.25051, 35.12449)          # neighbouring cell on both axes

def test_adjacent_viirs_pixel_and_other_minute_are_new():
    s = SentEvents(); s.add(T, -1.25031, 35.12345)
    assert not s.seen(T, -1.25031 + 0.0034, 35.12345)
    assert not s.seen(T + timedelta(minutes=1), -1.25031, 35.12345)

def test_state_round_trip_and_prune():
    s = SentEvents(); s.add(T, 1.0, 2.0); s.add(T - timedelta(days=4), 1.0, 2.0)
    s.prune(before=T - timedelta(days=3))
    assert s.to_state() == ["2026-11-23T00:42+00:00|1000|2000"]
    assert SentEvents(s.to_state()).seen(T, 1.0, 2.0)

def test_unreadable_stored_keys_are_dropped():
    assert SentEvents(["garbage", None, 5]).to_state() == []
```

- [ ] **Step 2: Run to verify failure** — `pytest app/actions/tests/test_dedupe.py -v`; Expected: FAIL.
- [ ] **Step 3: Implement `SentEvents`** over a `set[str]`.
- [ ] **Step 4: Run to verify pass.**
- [ ] **Step 5: Commit**

```bash
git add app/actions/dedupe.py app/actions/tests/test_dedupe.py
git commit -m "Track sent FIRMS events to avoid raising a fire twice"
```

---

### Task 6: Config models and handlers

**Files:**
- Modify: `app/actions/configurations.py`, `app/actions/handlers.py`, `README.md`
- Test: `app/actions/tests/test_configurations.py`, `app/actions/tests/test_handlers.py`

**Interfaces:**
- Consumes: `client.fetch_day`, `client.check_app_key`, `client.FIRMS_PRODUCTS`, `client.FIRMS_REGIONS`, `client.window_date_indexes`, `client.processing_level`, the `Firms*` exceptions; `boundaries.make_filter`; `boundary_source.resolve_boundary`; `dedupe.SentEvents`; template `send_events_to_gundi`, `IntegrationStateManager`, `log_action_activity`, `activity_logger`, `crontab_schedule` (`app.services.action_scheduler`), `IntegrationAuthError`, `IntegrationBadResponseError`, `generate_batches`.
- Produces:
  - `AuthenticateConfig(AuthActionConfiguration, ExecutableActionMixin)`: `app_key: SecretStr` (title "NASA Earthdata app key", `format="password"`, `UIOptions(widget="password")`).
  - `PullFireDetectionsConfig(PullActionConfiguration)`:
    - `region: FirmsRegion` (str Enum of `FIRMS_REGIONS`), required
    - `satellite: FirmsSatellite` (str Enum `noaa-20`, `noaa-21`), default `noaa-20`
    - `spatial_feature_group: Optional[str] = None`
    - `fallback_area: Optional[str] = None`, `UIOptions(widget="polygon")`; a dict is accepted and stored as its JSON string
    - `confidence_alert_levels: List[ConfidenceLevel]` (Enum `low`, `nominal`, `high`), default `[nominal, high]`, `unique_items=True`
    - `alert_window_hours: int = 24`, `ge=1`, `le=168`
    - root validator: at least one of `spatial_feature_group` (non-blank) or `fallback_area`; `fallback_area`, when given, must pass `boundaries.geometry_from_polygon`
    - `ui_global_options` order: region, satellite, spatial_feature_group, fallback_area, confidence_alert_levels, alert_window_hours, run_on_schedule
    - `ui_schema()` override adds `{"gundi:reference": {"action": "list_spatial_feature_groups", "target": "destination", "params": {}, "allow_free_text": True}}` under `spatial_feature_group`, and sets no `ui:widget` there
  - `async def action_auth(integration, action_config: AuthenticateConfig) -> dict`
  - `async def action_pull_fire_detections(integration, action_config: PullFireDetectionsConfig) -> dict`
- State (one document, `action_id="pull_fire_detections"`): `{"cursors": {"<product key>": {"<date index>": <cursor>}}, "boundary_cache": {...}, "sent_events": [...]}`.

`action_auth`: returns `{"valid_credentials": True}` or `{"valid_credentials": False, "error": "NASA Earthdata rejected the app key."}`; a `FirmsFetchError` becomes `{"valid_credentials": False, "error": "Could not reach NASA FIRMS to check the app key."}`.

`action_pull_fire_detections`, in order:
1. Take a run lease with `state_manager.set_if_absent(integration_id, "pull_fire_detections_lock", ttl_seconds=600)`. If not acquired, return `{"skipped": "another run is in progress"}`. Release it with `delete_state` in a `finally`.
2. Load state; the app key comes from the integration's `auth` action config, parsed with `AuthenticateConfig`.
3. `resolve_boundary(...)`. Log each warning with `log_action_activity(level=LogLevel.WARNING)`. Save the returned cache. If `geometry is None`: log a WARNING titled `"No boundary available for FIRMS; nothing was sent."` and return `{"skipped": "no boundary", "events_sent": 0}`.
4. For each date index in `window_date_indexes(now)` (cursor looked up under the configured product): `fetch_day`. For each record: skip unless `processing_level(version) == "NRT"`; skip if outside the boundary; skip unless confidence is in the configured levels and `recorded_at >= now - alert_window_hours`; skip if `SentEvents.seen`, or if a second, run-local `SentEvents` of detections already queued in this run has seen it (queued ones are not in the saved record until their batch is sent). Build the event (below).
5. Send that day's events with `send_events_to_gundi` in batches of 100. After each batch succeeds, `add` its detections to `SentEvents`. After the whole day succeeds, store the day's new cursor. Save state after each day.
6. `FirmsAuthError` stops the run: save state, raise `IntegrationAuthError("NASA Earthdata rejected the app key.")`. `FirmsFetchError` or `FirmsHeaderError` on one day leaves that day's cursor alone and the run continues with the other day; after saving state, raise `IntegrationBadResponseError` naming how many day files failed.
7. Before saving: drop cursors for date indexes outside the window; `prune(before=now - timedelta(hours=alert_window_hours) - timedelta(days=2))`.
8. Return `{"events_sent", "events_deduplicated", "outside_area", "skipped_processing_level", "boundary_source", **asdict(stats)}`.

Event payload:

```python
{
    "title": "FIRMS Fire Detected",
    "event_type": "firms_rep",
    "recorded_at": record["recorded_at"].isoformat(),
    "location": {"lat": round(lat, 3), "lon": round(lon, 3)},
    "event_details": {**{k: record[k] for k in ("confidence", "frp", "bright_ti4", "bright_ti5", "scan", "track") if k in record},
                      "reported_latitude": lat, "reported_longitude": lon},
}
```

`now` comes from a module-level `_utcnow()` in `handlers.py` so tests can patch it.

- [ ] **Step 1: Write the failing config tests**

```python
def test_auth_key_is_secret():
    assert "abc" not in repr(AuthenticateConfig(app_key="abc"))

def test_pull_defaults():
    cfg = PullFireDetectionsConfig(region="Southern_Africa", spatial_feature_group="g1")
    assert cfg.satellite.value == "noaa-20" and [l.value for l in cfg.confidence_alert_levels] == ["nominal", "high"]
    assert cfg.alert_window_hours == 24 and cfg.run_on_schedule is True

@pytest.mark.parametrize("kwargs", [
    {}, {"spatial_feature_group": "  "}, {"fallback_area": "not json"},
    {"fallback_area": json.dumps({"type": "Point", "coordinates": [0, 0]})},
    {"spatial_feature_group": "g1", "satellite": "suomi-npp"},
    {"spatial_feature_group": "g1", "alert_window_hours": 0},
])
def test_pull_rejects(kwargs):
    with pytest.raises(pydantic.ValidationError):
        PullFireDetectionsConfig(region="Southern_Africa", **kwargs)

def test_region_must_be_known():
    with pytest.raises(pydantic.ValidationError):
        PullFireDetectionsConfig(region="Atlantis", spatial_feature_group="g1")

def test_fallback_area_accepts_dict_and_string():
    a = PullFireDetectionsConfig(region="Europe", fallback_area=SQUARE)
    b_ = PullFireDetectionsConfig(region="Europe", fallback_area=json.dumps(SQUARE))
    assert json.loads(a.fallback_area) == json.loads(b_.fallback_area) == SQUARE

def test_ui_schema_reference_and_widget():
    ui = PullFireDetectionsConfig.ui_schema()
    assert ui["spatial_feature_group"]["gundi:reference"] == {
        "action": "list_spatial_feature_groups", "target": "destination", "params": {}, "allow_free_text": True}
    assert "ui:widget" not in ui["spatial_feature_group"]
    assert ui["fallback_area"]["ui:widget"] == "polygon"
```

- [ ] **Step 2: Run, verify failure, implement `configurations.py`, verify pass, commit**

Run: `pytest app/actions/tests/test_configurations.py -v`

```bash
git add app/actions/configurations.py app/actions/tests/test_configurations.py
git commit -m "Add FIRMS auth and pull configuration models"
```

- [ ] **Step 3: Write the failing handler tests**

Fixtures: `now = 2026-11-23 12:00 UTC` patched onto `handlers._utcnow`; an in-memory fake for `handlers.state_manager` (`get_state`, `set_state`, `set_if_absent` returning True, `delete_state`); `AsyncMock`s patched over `handlers.resolve_boundary`, `handlers.fetch_day`, `handlers.send_events_to_gundi`, `handlers.log_action_activity`, `handlers.check_app_key`. `rec(lat, lon, minute, confidence="nominal", version="2.0NRT")` builds a parsed record at `2026-11-23 00:<minute>` UTC with `frp=1.9, bright_ti4=338.1, bright_ti5=281.5, scan=0.43, track=0.46, satellite="N20", daynight="N"`. The boundary is the unit square (0..1). `fetch_day` returns `DayResult([], None)` for yesterday and the records under test for today, with cursor `CUR`.

```python
async def test_sends_event_for_confident_recent_detection_inside():
    result = await run([rec(0.51234, 0.49876, 42)])
    (events,), kwargs = send.await_args
    assert events == [{
        "title": "FIRMS Fire Detected", "event_type": "firms_rep",
        "recorded_at": "2026-11-23T00:42:00+00:00",
        "location": {"lat": 0.512, "lon": 0.499},
        "event_details": {"confidence": "nominal", "frp": 1.9, "bright_ti4": 338.1, "bright_ti5": 281.5,
                          "scan": 0.43, "track": 0.46, "reported_latitude": 0.51234, "reported_longitude": 0.49876}}]
    assert kwargs["integration_id"] == str(integration.id)
    assert result["events_sent"] == 1 and result["boundary_source"] == "earthranger"
    assert state["cursors"]["noaa-20"]["2026327"] == CUR
    assert state["sent_events"] == ["2026-11-23T00:42+00:00|512|499"]

@pytest.mark.parametrize("record", [
    rec(5.0, 5.0, 1),                              # outside the boundary
    rec(0.5, 0.5, 1, confidence="low"),            # confidence not in alert levels
    rec(0.5, 0.5, 1, version="2.0URT"),            # not NRT
    rec(0.5, 0.5, 1, version="2.0"),               # unknown processing level
])
async def test_filtered_detections_send_nothing(record):
    assert (await run([record]))["events_sent"] == 0 and send.await_count == 0

async def test_detection_older_than_alert_window_is_not_sent():
    old = rec(0.5, 0.5, 1); old["recorded_at"] = NOW - timedelta(hours=25)
    assert (await run([old]))["events_sent"] == 0

async def test_second_run_and_revised_position_are_deduplicated():
    await run([rec(0.51234, 0.49876, 42)])
    result = await run([rec(0.51234, 0.49876, 42), rec(0.51251, 0.49949, 42)])
    assert result["events_sent"] == 0 and result["events_deduplicated"] == 2

async def test_duplicates_within_one_file_send_once():
    assert (await run([rec(0.5, 0.5, 7), rec(0.5, 0.5, 7)]))["events_sent"] == 1

async def test_no_boundary_sends_nothing_and_warns():
    resolve.return_value = ResolvedBoundary(None, "none", ["x"], {})
    result = await run([rec(0.5, 0.5, 1)])
    assert result == {"skipped": "no boundary", "events_sent": 0}
    assert fetch.await_count == 0 and send.await_count == 0
    assert any(c.kwargs.get("level") == LogLevel.WARNING for c in log.await_args_list)

async def test_fallback_boundary_logs_each_warning_and_saves_cache():
    resolve.return_value = ResolvedBoundary(UNIT_SQUARE, "cached", ["ER unreachable"], {"a": {"group_id": "g1", "geometry": SQUARE}})
    await run([])
    assert state["boundary_cache"] == {"a": {"group_id": "g1", "geometry": SQUARE}}
    assert [c.kwargs["title"] for c in log.await_args_list] == ["ER unreachable"]

async def test_send_failure_keeps_cursor_and_does_not_mark_sent():
    send.side_effect = RuntimeError("gundi down")
    with pytest.raises(RuntimeError):
        await run([rec(0.5, 0.5, 1)])
    assert "2026327" not in state.get("cursors", {}).get("noaa-20", {}) and state.get("sent_events", []) == []
    assert lock_released()

async def test_auth_failure_raises_integration_auth_error():
    fetch.side_effect = FirmsAuthError("x")
    with pytest.raises(IntegrationAuthError):
        await run([])

async def test_one_bad_day_file_does_not_block_the_other():
    fetch.side_effect = [FirmsFetchError("x", status_code=500), DayResult([rec(0.5, 0.5, 1)], CUR)]
    with pytest.raises(IntegrationBadResponseError):
        await run(None)
    assert send.await_count == 1 and "2026326" not in state["cursors"]["noaa-20"]

async def test_rollover_drops_stale_cursors_and_keys():
    state.update({"cursors": {"noaa-20": {"2026320": CUR}}, "sent_events": ["2026-11-16T00:42+00:00|500|500"]})
    await run([])
    assert "2026320" not in state["cursors"]["noaa-20"] and state["sent_events"] == []
    assert [c.kwargs["date_index"] for c in fetch.await_args_list] == [2026326, 2026327]

async def test_cursor_is_read_for_the_configured_satellite():
    state["cursors"] = {"noaa-21": {"2026327": CUR}}
    await run([], satellite="noaa-21")
    assert fetch.await_args_list[1].kwargs["cursor"] == CUR and fetch.await_args_list[1].kwargs["product"].key == "noaa-21"

async def test_overlapping_run_is_skipped():
    fake_state.set_if_absent = AsyncMock(return_value=False)
    assert await run([rec(0.5, 0.5, 1)]) == {"skipped": "another run is in progress"}
    assert fetch.await_count == 0

async def test_more_than_100_events_are_sent_in_batches():
    await run([rec(0.01 * i, 0.5, 1) for i in range(1, 99)] + [rec(0.5, 0.01 * i, 2) for i in range(1, 99)])
    assert [len(c.args[0]) for c in send.await_args_list] == [100, 96]

@pytest.mark.parametrize("outcome,expected", [
    (True, {"valid_credentials": True}),
    (False, {"valid_credentials": False, "error": "NASA Earthdata rejected the app key."}),
    (FirmsFetchError("x"), {"valid_credentials": False, "error": "Could not reach NASA FIRMS to check the app key."}),
])
async def test_action_auth(outcome, expected): ...

def test_schedule_and_discovery():
    assert set(get_actions()) == {"auth", "pull_fire_detections"}
    assert handlers.action_pull_fire_detections.crontab_schedule.minute == "*/15"   # adjust to CrontabSchedule's field name
```

- [ ] **Step 4: Run to verify failure** — `pytest app/actions/tests/test_handlers.py -v`; Expected: FAIL, handlers not defined.
- [ ] **Step 5: Implement `handlers.py`** per the ordered behaviour above; decorate the pull action with `@crontab_schedule("*/15 * * * *")` and `@activity_logger()`.
- [ ] **Step 6: Run the whole suite** — `pytest -q`; Expected: PASS, including the template's own tests.
- [ ] **Step 7: Add a "FIRMS" section at the top of `README.md`**: what the integration does, where to get an app key (https://nrt3.modaps.eosdis.nasa.gov/, Profile menu), the config fields, the boundary fallback order, and the two prerequisites below.
- [ ] **Step 8: Commit**

```bash
git add app/actions/handlers.py app/actions/tests/test_handlers.py README.md
git commit -m "Add FIRMS auth and scheduled fire detection pull actions"
```

---

### Task 7: Feature group reference action in the EarthRanger runner (separate repo, separate PR)

Independent of Tasks 1 to 6. Until it ships, the portal field is a free-text group id.

**Files** (in `/Users/chrisdo/padas/gundi-integration-earthranger`, on a new branch):
- Modify: `app/actions/configurations.py`, `app/actions/handlers.py`
- Test: `app/actions/tests/test_handlers.py` (beside the existing reference-action tests)

**Interfaces:**
- Produces: `class ListSpatialFeatureGroupsQuery(ReferenceActionConfiguration)` (no fields) and `async def action_list_spatial_feature_groups(integration, action_config: ListSpatialFeatureGroupsQuery) -> dict`. The action id `list_spatial_feature_groups` is what Task 6's `gundi:reference` names.

- [ ] **Step 1: Write the failing test**, following the existing `action_list_event_categories` tests: ER answers `featuregroups` with `[{"id": "g1", "name": "Park boundary", "feature_count": 3}, {"id": "g2", "name": "Buffer", "feature_count": 1}]`; the action returns options sorted by label, `[{"value": "g2", "label": "Buffer", "description": "1 feature", ...}, {"value": "g1", "label": "Park boundary", "description": "3 features", ...}]`, `truncated` false. A second case: ER answers a paginated `{"results": [...]}` body and the result is the same. A third: an ER 401 surfaces as the same `IntegrationAuthError` the other reference actions raise.
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement**, inside `async with _translating_er_client(integration) as earth_ranger`, reading ER's `featuregroups` list endpoint (`earth_ranger._get("featuregroups")`; `erclient` has no public method for it) and returning `ReferenceDataResponse(options=..., truncated=False).dict()`.
- [ ] **Step 4: Run that repo's suite; verify pass.**
- [ ] **Step 5: Commit** — `git commit -m "Add list_spatial_feature_groups reference action"`.

---

## After the plan (not tasks)

- `firms_rep` must exist on each destination ER site with `reported_latitude` and `reported_longitude` in its schema, or ER rejects the events.
- Infra: `./create_integration.py firms` in `gundi-integrations-v2-infra`, then decide deployment settings.
