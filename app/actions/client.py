"""NASA FIRMS near-real-time archive client.

Standalone by design: plain arguments in, parsed records out, its own exceptions, no retries
and no imports from the action-runner template.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
