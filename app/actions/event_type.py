"""The EarthRanger event type FIRMS detections are reported against.

EarthRanger creates `firms_rep` for a site the first time its own FIRMS plugin runs there, so a site that never ran
the plugin does not have it and rejects every FIRMS event. Before events are sent, each EarthRanger destination is
checked and the type is created where it is missing, with the definition the plugin uses.
"""

import logging
from dataclasses import dataclass, field
from typing import List

from erclient.er_errors import ERClientNotFound

from .boundary_source import ERDestination, er_client, get_er_destinations, host_of

logger = logging.getLogger(__name__)

EVENT_TYPE_VALUE = "firms_rep"
EVENT_CATEGORY = {"value": "analyzer_event", "display": "Analyzer Events"}
REFERENCE_PRIORITY = 100
_SECTION = "section-1"

_REPORTED_NOTE = "as published by FIRMS; the event location is rounded for de-duplication."

# (name, title, JSON type, description), in form order. Mirrors FIRMS_EVENT_TYPE_SCHEMA in EarthRanger's
# das/tracking/firms_event_type.py.
_FIELDS = (
    ("confidence", "FIRMS Confidence", "string", ""),
    ("frp", "Fire Radiative Power (MW)", "number", ""),
    ("bright_ti4", "VIIRS I-4 channel brightness (Kelvin)", "number", ""),
    ("bright_ti5", "VIIRS I-5 channel brightness (Kelvin)", "number", ""),
    ("scan", "VIIRS Scan", "number", ""),
    ("track", "Satellite Track", "number", ""),
    ("reported_latitude", "Reported Latitude", "number", f"Latitude {_REPORTED_NOTE}"),
    ("reported_longitude", "Reported Longitude", "number", f"Longitude {_REPORTED_NOTE}"),
)


def _json_property(title: str, json_type: str, description: str) -> dict:
    prop = {"deprecated": False, "title": title}
    if json_type == "string":
        prop["default"] = ""
    prop.update({"description": description, "type": json_type})
    return prop


def _ui_field(json_type: str) -> dict:
    ui = {"conditionalDependents": [], "parent": _SECTION}
    if json_type == "string":
        ui.update({"type": "TEXT", "inputType": "SHORT_TEXT"})
    else:
        ui["type"] = "NUMERIC"
    ui["placeholder"] = ""
    return ui


EVENT_TYPE_SCHEMA = {
    "json": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "properties": {
            name: _json_property(title, json_type, description) for name, title, json_type, description in _FIELDS
        },
        "required": [],
        "type": "object",
        "unevaluatedProperties": False,
    },
    "ui": {
        "fields": {name: _ui_field(json_type) for name, _, json_type, _ in _FIELDS},
        "headers": {},
        "order": [_SECTION],
        "sections": {
            _SECTION: {
                "columns": 1,
                "conditions": [],
                "isActive": True,
                "label": "",
                "leftColumn": [{"name": name, "type": "field"} for name, _, _, _ in _FIELDS],
                "rightColumn": [],
            },
        },
    },
}

EVENT_TYPE = {
    "value": EVENT_TYPE_VALUE,
    "display": "FIRMS Report",
    "category": EVENT_CATEGORY["value"],
    "icon": EVENT_TYPE_VALUE,
    "default_priority": REFERENCE_PRIORITY,
    "readonly": True,
    "schema": EVENT_TYPE_SCHEMA,
}


class EventTypeUnavailable(Exception):
    """A destination has no FIRMS event type and it could not be created, so its events would be rejected."""

    def __init__(self, host: str, reason: str):
        # Only the host and the error's class: an EarthRanger error message can quote the request.
        super().__init__(
            f"The '{EVENT_TYPE_VALUE}' event type is missing on {host} and could not be created ({reason}). "
            "Nothing was sent."
        )
        self.host = host
        self.reason = reason


@dataclass
class EventTypeCheck:
    # Hosts where the event type was created in this check.
    created: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


async def ensure_event_type(destination: ERDestination) -> bool:
    """Create the FIRMS event type on one EarthRanger site if it has none. Returns whether it was created.

    Raises EventTypeUnavailable when the type is missing and cannot be created. Any other failure means the site
    could not be asked, and is raised as it came.
    """
    async with er_client(destination) as client:
        try:
            # This route answers for a type of either schema version, active or not; only an unknown value is a 404.
            await client.get_event_type(EVENT_TYPE_VALUE)
            return False
        except ERClientNotFound:
            pass
        try:
            categories = await client.get_event_categories(include_inactive=True)
            if not any(category.get("value") == EVENT_CATEGORY["value"] for category in categories or []):
                await client.post_event_category(dict(EVENT_CATEGORY))
            await client.post_event_type(EVENT_TYPE, version="v2.0")
        except Exception as exc:
            raise EventTypeUnavailable(host_of(destination.base_url), type(exc).__name__) from None
    return True


async def ensure_event_type_on_destinations(integration_id: str) -> EventTypeCheck:
    """Make sure every EarthRanger destination of the integration can take FIRMS events.

    A destination that cannot be asked becomes a warning, since its type is most likely there. One that is known to
    lack the type and refuses to have it created raises EventTypeUnavailable.
    """
    check = EventTypeCheck()
    try:
        destinations = await get_er_destinations(integration_id)
    except Exception as exc:
        logger.warning("Could not look up EarthRanger destinations: %s", type(exc).__name__)
        check.warnings.append(
            f"Could not look up the EarthRanger destinations to check the '{EVENT_TYPE_VALUE}' event type "
            f"({type(exc).__name__})."
        )
        return check

    for destination in destinations:
        host = host_of(destination.base_url)
        try:
            if destination.unreadable:
                raise RuntimeError("DestinationUnreadable")
            created = await ensure_event_type(destination)
        except EventTypeUnavailable:
            raise
        except Exception as exc:
            reason = "DestinationUnreadable" if destination.unreadable else type(exc).__name__
            logger.warning("Could not check the FIRMS event type on %s: %s", host, reason)
            check.warnings.append(f"Could not check the '{EVENT_TYPE_VALUE}' event type on {host} ({reason}).")
        else:
            if created:
                logger.info("Created the FIRMS event type on %s", host)
                check.created.append(host)
    return check
