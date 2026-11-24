"""Where the FIRMS boundary comes from.

The boundary is a spatial feature group on the connection's EarthRanger destination(s). Each destination is read
live; one that cannot be read contributes the geometry last read from it; and when no destination yields anything,
the polygon in the action's configuration is used. With none of those, there is no boundary and nothing is sent.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

from erclient import AsyncERClient
from gundi_client_v2.client import GundiClient
from gundi_client_v2.errors import GundiAPIError
from shapely.geometry.base import BaseGeometry

from . import boundaries

logger = logging.getLogger(__name__)

# "earthranger" was registered by accident in the past and is still on some integrations.
EARTHRANGER_TYPE_SLUGS = frozenset({"earth_ranger", "earthranger"})


class DestinationUnreadable(Exception):
    """Gundi could not return a destination's details, so its EarthRanger site was not contacted."""


@dataclass
class ERDestination:
    id: str
    base_url: str
    token: Optional[str] = field(default=None, repr=False)
    username: Optional[str] = None
    password: Optional[str] = field(default=None, repr=False)
    # Gundi could not return this destination's details this run, so its site cannot be read.
    unreadable: bool = False


@dataclass
class ResolvedBoundary:
    geometry: Optional[BaseGeometry]
    # "earthranger" (every part read live), "cached" (at least one part from the last good read),
    # "config" (the configured fallback area) or "none".
    source: str
    warnings: List[str] = field(default_factory=list)
    # The per-destination cache to store: {destination_id: {"group_id": ..., "geometry": <GeoJSON>}}
    cache: Dict[str, Any] = field(default_factory=dict)


def _site_root(base_url: str) -> Optional[str]:
    """`scheme://hostname` of an EarthRanger site URL, or None when it has no hostname."""
    parsed = urlparse(base_url or "")
    if not (parsed.scheme and parsed.hostname):
        return None
    return f"{parsed.scheme}://{parsed.hostname}"


def _host(base_url: str) -> str:
    # Only the hostname goes into messages: a URL can carry a token in its path or query.
    return urlparse(base_url or "").hostname or "an EarthRanger destination"


async def get_er_destinations(integration_id: str) -> List[ERDestination]:
    """The connection's EarthRanger destinations that carry credentials.

    Destinations of any other type are left out, so another system's credentials are never sent to EarthRanger
    endpoints on its host. So are EarthRanger destinations without an auth configuration, usable credentials or a
    site URL. One whose details Gundi fails to return is kept and marked `unreadable`, so that its last known
    boundary can still be used. Returns an empty list when the integration has no destinations, or is not a
    provider on any connection (a 404 from Gundi).
    """
    async with GundiClient() as gundi:
        try:
            connection = await gundi.get_connection_details(str(integration_id))
        except GundiAPIError as exc:
            if exc.status_code == 404:
                return []
            raise
        destinations = []
        for destination in connection.destinations or []:
            if getattr(getattr(destination, "type", None), "value", None) not in EARTHRANGER_TYPE_SLUGS:
                continue
            try:
                details = await gundi.get_integration_details(str(destination.id))
            except Exception as exc:
                logger.warning("Could not read destination %s from Gundi: %s", destination.id, type(exc).__name__)
                destinations.append(
                    ERDestination(str(destination.id), getattr(destination, "base_url", None) or "", unreadable=True)
                )
                continue
            auth_config = details.get_action_config("auth")
            data = getattr(auth_config, "data", None) or {}
            token = data.get("token") or None
            username = data.get("username") or None
            password = data.get("password") or None
            if not _site_root(details.base_url):
                continue
            if token:
                destinations.append(ERDestination(str(destination.id), details.base_url, token=token))
            elif username and password:
                destinations.append(
                    ERDestination(str(destination.id), details.base_url, username=username, password=password)
                )
        return destinations


async def fetch_feature_group(destination: ERDestination, group_id: str) -> dict:
    """The spatial feature group's detail payload (including its features) from one EarthRanger site."""
    site_root = _site_root(destination.base_url)
    client = AsyncERClient(
        service_root=f"{site_root}/api/v1.0",
        username=destination.username,
        password=destination.password,
        token=destination.token,
        token_url=f"{site_root}/oauth2/token",
        client_id="das_web_client",
    )
    async with client as er_client:
        return await er_client.get_feature_group(group_id)


def _cached_geometry(entry: Any, group_id: str) -> Optional[BaseGeometry]:
    if not isinstance(entry, dict) or entry.get("group_id") != group_id:
        return None
    try:
        geometry = boundaries.from_geojson(entry["geometry"])
    except Exception:
        return None
    return None if geometry.is_empty else geometry


async def resolve_boundary(
    *,
    integration_id: str,
    group_id: Optional[str],
    fallback_area: Union[dict, str, None],
    cache: Optional[Dict[str, Any]],
) -> ResolvedBoundary:
    """Resolve the area to filter by. Never raises: every failure becomes a warning and the next fallback."""
    group_id = (group_id or "").strip() or None
    warnings: List[str] = []
    # Geometries cached for a different group are no longer the configured boundary.
    new_cache = {
        dest_id: entry
        for dest_id, entry in (cache or {}).items()
        if group_id and isinstance(entry, dict) and entry.get("group_id") == group_id
    }
    parts: List[BaseGeometry] = []
    used_cache = False

    if group_id:
        try:
            destinations = await get_er_destinations(integration_id)
        except Exception as exc:
            # Exception text is left out of every warning: it can quote a URL or credentials.
            logger.warning("Could not look up EarthRanger destinations: %s", type(exc).__name__)
            destinations = []
            # Without the list of destinations, every boundary last read for this group still applies.
            for entry in new_cache.values():
                cached = _cached_geometry(entry, group_id)
                if cached is not None:
                    parts.append(cached)
                    used_cache = True
            warnings.append(
                f"Could not look up the EarthRanger destinations ({type(exc).__name__})"
                + ("; using the boundary last read from EarthRanger." if parts else ".")
            )
        else:
            # The lookup succeeded, so a destination it does not list is no longer connected: forget its boundary,
            # or a later lookup outage would bring it back. One that is listed but unreadable keeps its entry.
            connected = {destination.id for destination in destinations}
            new_cache = {dest_id: entry for dest_id, entry in new_cache.items() if dest_id in connected}
            if not destinations:
                warnings.append("No EarthRanger destination with credentials is connected to this integration.")

        for destination in destinations:
            host = _host(destination.base_url)
            try:
                if destination.unreadable:
                    raise DestinationUnreadable()
                geometry = boundaries.geometry_from_feature_group(await fetch_feature_group(destination, group_id))
            except Exception as exc:
                logger.warning("Could not read the spatial feature group from %s: %s", host, type(exc).__name__)
                cached = _cached_geometry(new_cache.get(destination.id), group_id)
                if cached is not None:
                    parts.append(cached)
                    used_cache = True
                    warnings.append(
                        f"Could not read the spatial feature group from {host} ({type(exc).__name__}); "
                        "using the boundary last read from it."
                    )
                else:
                    new_cache.pop(destination.id, None)
                    warnings.append(
                        f"Could not read the spatial feature group from {host} ({type(exc).__name__}), "
                        "and no earlier copy of its boundary is stored."
                    )
            else:
                parts.append(geometry)
                new_cache[destination.id] = {"group_id": group_id, "geometry": boundaries.to_geojson(geometry)}

    if parts:
        return ResolvedBoundary(
            boundaries.union_geometries(parts), "cached" if used_cache else "earthranger", warnings, new_cache
        )

    if fallback_area:
        try:
            geometry = boundaries.geometry_from_polygon(fallback_area)
        except boundaries.BoundaryError:
            warnings.append("The fallback area in the configuration is not a usable polygon.")
        else:
            if group_id:
                warnings.append("Using the fallback area from the configuration to filter FIRMS detections.")
            return ResolvedBoundary(geometry, "config", warnings, new_cache)
    elif not group_id:
        warnings.append("No spatial feature group or fallback area is configured.")

    return ResolvedBoundary(None, "none", warnings, new_cache)
