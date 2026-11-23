"""NASA FIRMS near-real-time archive client.

Standalone by design: plain arguments in, parsed records out, its own exceptions, no retries
and no imports from the action-runner template.
"""

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx

logger = logging.getLogger(__name__)

FIRMS_HOST = "nrt3.modaps.eosdis.nasa.gov"


@dataclass(frozen=True)
class FirmsProduct:
    key: str
    folder: str
    filename_prefix: str
    data_type_and_name: str


# Suomi NPP stopped delivering on 2026-11-02, so it is not offered.
FIRMS_PRODUCTS: Dict[str, FirmsProduct] = {
    product.key: product
    for product in (
        FirmsProduct("noaa-20", "noaa-20-viirs-c2", "J1_VIIRS_C2", "VJ114IMGTDL_NRT"),
        FirmsProduct("noaa-21", "noaa-21-viirs-c2", "J2_VIIRS_C2", "VJ214IMGTDL_NRT"),
    )
}
DEFAULT_FIRMS_PRODUCT = "noaa-20"

FIRMS_REGIONS: Tuple[str, ...] = (
    "Alaska",
    "Australia_NewZealand",
    "Canada",
    "Central_America",
    "Europe",
    "Global",
    "Northern_and_Central_Africa",
    "Russia_Asia",
    "SouthEast_Asia",
    "South_America",
    "South_Asia",
    "Southern_Africa",
    "USA_contiguous_and_Hawaii",
)

REQUIRED_COLUMNS = ("latitude", "longitude", "acq_date", "acq_time")
FLOAT_COLUMNS = frozenset({"latitude", "longitude", "bright_ti4", "bright_ti5", "scan", "track", "frp"})
_PROCESSING_LEVEL_RE = re.compile(r"^\s*\d+(?:\.\d+)*\s*([A-Za-z]+)\s*$")
# The archive writes `HH:MM`; the public CSVs write `HHMM`.
_ACQ_TIME_RE = re.compile(r"^(?:(\d{2}):(\d{2})|(\d{2})(\d{2}))$")


class FirmsError(Exception):
    """Base for everything this client raises."""


class FirmsAuthError(FirmsError):
    """NASA refused the app key, or answered with its login page."""


class FirmsFetchError(FirmsError):
    """A day file could not be fetched: an unexpected status, or no response at all."""

    def __init__(self, message: str = "", status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class FirmsHeaderError(FirmsError):
    """A day file whose header row lacks a column the client cannot do without."""


def date_index(d: datetime) -> int:
    """The archive's day key: UTC year * 1000 + day of year."""
    t = d.astimezone(timezone.utc).timetuple()
    return t.tm_year * 1000 + t.tm_yday


def window_date_indexes(now: datetime) -> List[int]:
    """Yesterday's and today's day files: NASA keeps rewriting yesterday's for hours after UTC midnight."""
    return [date_index(now - timedelta(days=1)), date_index(now)]


def file_url(region: str, product: FirmsProduct, date_index: int) -> str:
    filename = f"{product.filename_prefix}_{region}_{product.data_type_and_name}_{date_index}.txt"
    return f"https://{FIRMS_HOST}/api/v2/content/archives/FIRMS/{product.folder}/{region}/{filename}"


def parse_header(line: str) -> Tuple[str, ...]:
    columns = tuple(column.strip().lower() for column in line.strip().split(","))
    missing = [column for column in REQUIRED_COLUMNS if column not in columns]
    if missing:
        raise FirmsHeaderError(f"FIRMS header is missing {', '.join(missing)}: {line!r}")
    return columns


def parse_line(line: str, columns: Sequence[str]) -> Dict[str, Any]:
    """One data row as a dict keyed by column, plus `recorded_at` (UTC). Raises ValueError on a malformed row."""
    values = line.split(",")
    if len(values) != len(columns):
        raise ValueError(f"expected {len(columns)} fields, got {len(values)}")
    record: Dict[str, Any] = {
        column: float(value) if column in FLOAT_COLUMNS else value.strip() for column, value in zip(columns, values)
    }
    # Written so that NaN fails too.
    if not (abs(record["latitude"]) <= 90 and abs(record["longitude"]) <= 180):
        raise ValueError("coordinates out of range")
    acq_time = _ACQ_TIME_RE.match(record["acq_time"])
    if acq_time is None:
        raise ValueError(f"unrecognised acq_time {record['acq_time']!r}")
    hour, minute = acq_time[1] or acq_time[3], acq_time[2] or acq_time[4]
    # replace() rejects an hour above 23 or a minute above 59.
    record["recorded_at"] = datetime.strptime(record["acq_date"], "%Y-%m-%d").replace(
        hour=int(hour), minute=int(minute), tzinfo=timezone.utc
    )
    return record


def processing_level(version: Any) -> Optional[str]:
    """`2.0NRT` -> `NRT`; None when the version doesn't carry a recognisable level."""
    match = _PROCESSING_LEVEL_RE.match(version) if isinstance(version, str) else None
    return match.group(1).upper() if match else None


@dataclass
class FirmsStats:
    files_read: int = 0
    records_read: int = 0
    malformed_lines: int = 0
    resume_resets: int = 0
    files_not_found: int = 0


@dataclass
class DayResult:
    """New records from one day file, and the cursor to store once they have all been handled.

    The cursor records how far the file has been read: its `etag`, the byte `offset` just past the last complete
    line, the `prefix_sha256` of the bytes before that offset, and the header `columns`.
    """

    records: List[Dict[str, Any]] = field(default_factory=list)
    cursor: Optional[Dict[str, Any]] = None


_HTML_MARKER = b"<!DOCTYPE html>"
_AUTH_STATUSES = (401, 403)


def _is_resumable(cursor: Optional[Dict[str, Any]]) -> bool:
    """Whether `cursor` holds everything needed to validate and resume a file: offset, prefix hash and columns."""
    if not isinstance(cursor, dict):
        return False
    offset = cursor.get("offset")
    return (
        isinstance(offset, int)
        and not isinstance(offset, bool)
        and offset > 0
        and bool(cursor.get("prefix_sha256"))
        and bool(cursor.get("columns"))
    )


def _is_refusal(status_code: int) -> bool:
    # Unauthenticated requests are redirected to an HTML login page. Redirects are not followed, so the bearer
    # token is never sent on to another host.
    return status_code in _AUTH_STATUSES or 300 <= status_code < 400 and status_code != 304


async def fetch_day(
    *,
    app_key: str,
    region: str,
    product: FirmsProduct,
    date_index: int,
    cursor: Optional[Dict[str, Any]] = None,
    stats: Optional[FirmsStats] = None,
    timeout: float = 60.0,
) -> DayResult:
    """Read the records added to one day file since `cursor`.

    No byte ranges: the archive ignores If-Range and changes the etag on every append, so a partial body could
    never be validated. The whole file is fetched (or a 304 answered), and the cursor's prefix hash decides
    between reading only the new lines and re-reading the day.

    Returns the cursor unchanged when there is nothing new (304), the day file does not exist yet (404), or its
    header line is still being written. Raises FirmsAuthError, FirmsFetchError or FirmsHeaderError otherwise.
    """
    stats = stats if stats is not None else FirmsStats()
    url = file_url(region, product, date_index)
    resumable = _is_resumable(cursor)
    headers = {"Authorization": f"Bearer {app_key}"}
    # A 304 skips the file, so only a cursor we can resume from may ask for one.
    if resumable and cursor.get("etag"):
        headers["If-None-Match"] = cursor["etag"]

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as http:
            response = await http.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise FirmsFetchError(f"FIRMS request failed: {type(exc).__name__}") from exc

    status = response.status_code
    if status == 304:
        return DayResult([], cursor)
    if status == 404:
        stats.files_not_found += 1
        return DayResult([], cursor)
    if _is_refusal(status):
        raise FirmsAuthError(f"FIRMS refused the request with HTTP {status}")
    if status != 200:
        raise FirmsFetchError(f"FIRMS answered HTTP {status}", status_code=status)
    content = response.content
    if _HTML_MARKER in content:
        raise FirmsAuthError("FIRMS returned an HTML login page instead of data")

    header_end = content.find(b"\n")
    if header_end == -1:
        return DayResult([], cursor)
    columns = list(parse_header(content[:header_end].decode("utf-8", errors="replace")))

    read_from = header_end + 1
    if resumable:
        offset = cursor["offset"]
        if (
            cursor["columns"] == columns
            and offset <= len(content)
            and hashlib.sha256(content[:offset]).hexdigest() == cursor["prefix_sha256"]
        ):
            read_from = offset
        else:
            stats.resume_resets += 1
            logger.info("FIRMS file changed under its cursor; re-reading it from the start. url=%s", url)
    stats.files_read += 1

    # Stop at the last complete line: a half-written trailing record is re-read next time.
    complete_end = max(content.rfind(b"\n") + 1, read_from)
    records = []
    for raw in content[read_from:complete_end].decode("utf-8", errors="replace").split("\n"):
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        try:
            records.append(parse_line(line, columns))
        except ValueError as exc:
            stats.malformed_lines += 1
            logger.warning("Skipping malformed FIRMS line: %s. line=%r", exc, line)
    stats.records_read += len(records)

    return DayResult(
        records,
        {
            "etag": response.headers.get("ETag"),
            "offset": complete_end,
            "prefix_sha256": hashlib.sha256(content[:complete_end]).hexdigest(),
            "columns": columns,
        },
    )


async def check_app_key(app_key: str, *, now: Optional[datetime] = None, timeout: float = 30.0) -> bool:
    """Whether NASA accepts `app_key`, judged from the start of today's Global NOAA-20 day file.

    A 404 (today's file not published yet) still means the key was accepted. Raises FirmsFetchError when NASA
    cannot be reached or answers with anything else unexpected.
    """
    now = now or datetime.now(tz=timezone.utc)
    url = file_url("Global", FIRMS_PRODUCTS[DEFAULT_FIRMS_PRODUCT], date_index(now))
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as http:
            async with http.stream("GET", url, headers={"Authorization": f"Bearer {app_key}"}) as response:
                status = response.status_code
                start = b""
                if status == 200:
                    # The Global file is large; its first bytes are enough to tell data from a login page.
                    async for chunk in response.aiter_bytes():
                        start += chunk
                        if len(start) >= 1024:
                            break
    except httpx.HTTPError as exc:
        raise FirmsFetchError(f"FIRMS request failed: {type(exc).__name__}") from exc
    if status == 404:
        return True
    if _is_refusal(status):
        return False
    if status != 200:
        raise FirmsFetchError(f"FIRMS answered HTTP {status}", status_code=status)
    return _HTML_MARKER not in start[:1024]
