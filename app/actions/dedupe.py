"""The runner's own record of fire events already sent.

EarthRanger's FIRMS plugin skips a detection when an incident already exists for the same acquisition time within
one rounding cell of its position, which absorbs NASA's intraday geolocation revisions. The runner cannot query
EarthRanger's events, so it remembers what it sent: the acquisition minute and the position's cell.
"""

from datetime import datetime, timezone
from typing import Iterable, List, Optional, Set, Tuple

# 3 decimal places is ~110 m: coarse enough to absorb a geolocation revision, fine enough to keep adjacent 375 m
# VIIRS pixels (~0.0034 degrees apart) separate.
LOCATION_PRECISION = 3
_SCALE = 10**LOCATION_PRECISION


def _minute(recorded_at: datetime) -> str:
    return recorded_at.astimezone(timezone.utc).isoformat(timespec="minutes")


def _cell(latitude: float, longitude: float) -> Tuple[int, int]:
    return round(latitude * _SCALE), round(longitude * _SCALE)


def _key(minute: str, lat_cell: int, lon_cell: int) -> str:
    return f"{minute}|{lat_cell}|{lon_cell}"


def _key_time(key: str) -> Optional[datetime]:
    """The acquisition time in a stored key, or None when the key is not one of ours."""
    if not isinstance(key, str):
        return None
    parts = key.split("|")
    if len(parts) != 3:
        return None
    try:
        int(parts[1]), int(parts[2])
        when = datetime.fromisoformat(parts[0])
    except ValueError:
        return None
    return when if when.tzinfo else None


class SentEvents:
    def __init__(self, keys: Optional[Iterable[str]] = None):
        self._keys: Set[str] = {key for key in (keys or []) if _key_time(key) is not None}

    def seen(self, recorded_at: datetime, latitude: float, longitude: float) -> bool:
        """Whether an event was recorded for this minute in this cell or any of its eight neighbours."""
        minute = _minute(recorded_at)
        lat_cell, lon_cell = _cell(latitude, longitude)
        return any(
            _key(minute, lat_cell + d_lat, lon_cell + d_lon) in self._keys
            for d_lat in (-1, 0, 1)
            for d_lon in (-1, 0, 1)
        )

    def add(self, recorded_at: datetime, latitude: float, longitude: float) -> None:
        self._keys.add(_key(_minute(recorded_at), *_cell(latitude, longitude)))

    def prune(self, before: datetime) -> None:
        """Forget events acquired earlier than `before`."""
        self._keys = {key for key in self._keys if _key_time(key) >= before}

    def to_state(self) -> List[str]:
        return sorted(self._keys)
