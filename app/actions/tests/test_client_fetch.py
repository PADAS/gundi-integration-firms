import hashlib
from datetime import datetime, timezone

import httpx
import pytest
import respx

from app.actions import client as c

NOAA20 = c.FIRMS_PRODUCTS["noaa-20"]
HEADER = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight"
)
ROW = "-1.25031,35.12345,338.1,0.43,0.46,2026-11-23,00:42,N20,nominal,2.0NRT,281.5,1.9,N"
ROW2 = "-1.30000,35.20000,330.0,0.40,0.41,2026-11-23,00:43,N20,high,2.0NRT,280.0,3.2,N"
BODY = f"{HEADER}\n{ROW}\n"
URL = c.file_url("Southern_Africa", NOAA20, 2026327)


async def fetch(cursor=None, stats=None):
    return await c.fetch_day(
        app_key="k", region="Southern_Africa", product=NOAA20, date_index=2026327, cursor=cursor, stats=stats
    )


@respx.mock
@pytest.mark.asyncio
async def test_first_read_returns_records_and_cursor():
    route = respx.get(URL).respond(200, text=BODY, headers={"ETag": '"a"'})
    stats = c.FirmsStats()
    result = await fetch(stats=stats)
    assert [r["latitude"] for r in result.records] == [-1.25031]
    assert result.cursor == {
        "etag": '"a"',
        "offset": len(BODY.encode()),
        "prefix_sha256": hashlib.sha256(BODY.encode()).hexdigest(),
        "columns": list(c.parse_header(HEADER)),
    }
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer k"
    assert "If-None-Match" not in req.headers and "Range" not in req.headers
    assert (stats.files_read, stats.records_read) == (1, 1)


@respx.mock
@pytest.mark.asyncio
async def test_resume_reads_only_appended_lines():
    respx.get(URL).respond(200, text=BODY, headers={"ETag": '"a"'})
    first = await fetch()
    route = respx.get(URL).respond(200, text=BODY + ROW2 + "\n", headers={"ETag": '"b"'})
    second = await fetch(cursor=first.cursor)
    assert [r["confidence"] for r in second.records] == ["high"]
    assert second.cursor["etag"] == '"b"'
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
async def test_cursor_that_is_not_resumable_sends_no_etag():
    route = respx.get(URL).respond(200, text=BODY)
    await fetch(cursor={"etag": '"a"', "offset": 0, "prefix_sha256": "", "columns": []})
    assert "If-None-Match" not in route.calls.last.request.headers


@respx.mock
@pytest.mark.asyncio
async def test_malformed_line_is_skipped_and_counted():
    respx.get(URL).respond(200, text=f"{HEADER}\nnot,a,row\n{ROW}\r\n")
    stats = c.FirmsStats()
    assert len((await fetch(stats=stats)).records) == 1 and stats.malformed_lines == 1


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("status", [304, 404])
async def test_unchanged_or_missing_keeps_cursor(status):
    cur = {"etag": '"a"', "offset": 5, "prefix_sha256": "x", "columns": ["latitude"]}
    respx.get(URL).respond(status)
    stats = c.FirmsStats()
    result = await fetch(cursor=cur, stats=stats)
    assert result.records == [] and result.cursor == cur
    assert stats.files_not_found == (1 if status == 404 else 0)


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,exc",
    [
        (401, "", c.FirmsAuthError),
        (403, "", c.FirmsAuthError),
        (303, "", c.FirmsAuthError),
        (200, "<!DOCTYPE html><html>login</html>", c.FirmsAuthError),
        (206, BODY, c.FirmsFetchError),
        (416, "", c.FirmsFetchError),
        (500, "", c.FirmsFetchError),
        (200, "latitude,longitude\n1,2\n", c.FirmsHeaderError),
    ],
)
async def test_failures_raise(status, body, exc):
    respx.get(URL).respond(status, text=body)
    with pytest.raises(exc):
        await fetch()


@respx.mock
@pytest.mark.asyncio
async def test_fetch_error_carries_status_code():
    respx.get(URL).respond(500)
    with pytest.raises(c.FirmsFetchError) as e:
        await fetch()
    assert e.value.status_code == 500


@respx.mock
@pytest.mark.asyncio
async def test_transport_error_is_fetch_error():
    respx.get(URL).mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(c.FirmsFetchError) as e:
        await fetch()
    assert e.value.status_code is None


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body,valid",
    [(200, BODY, True), (404, "", True), (401, "", False), (303, "", False), (200, "<!DOCTYPE html>", False)],
)
async def test_check_app_key(status, body, valid):
    now = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
    route = respx.get(c.file_url("Global", NOAA20, 2026327)).respond(status, text=body)
    assert await c.check_app_key("k", now=now) is valid
    assert route.calls.last.request.headers["Authorization"] == "Bearer k"


@respx.mock
@pytest.mark.asyncio
async def test_check_app_key_raises_when_nasa_is_unavailable():
    now = datetime(2026, 11, 23, 12, tzinfo=timezone.utc)
    respx.get(c.file_url("Global", NOAA20, 2026327)).respond(503)
    with pytest.raises(c.FirmsFetchError):
        await c.check_app_key("k", now=now)
