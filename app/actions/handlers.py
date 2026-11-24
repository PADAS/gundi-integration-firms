import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from gundi_core.events import LogLevel

from app import settings
from app.services.action_scheduler import crontab_schedule
from app.services.activity_logger import activity_logger, log_action_activity
from app.services.errors import IntegrationAuthError, IntegrationBadResponseError, IntegrationConnectionError
from app.services.gundi import send_events_to_gundi
from app.services.state import IntegrationStateManager
from app.services.utils import find_config_for_action, generate_batches

from .boundaries import make_filter
from .boundary_source import resolve_boundary
from .client import (
    FIRMS_PRODUCTS,
    FirmsAuthError,
    FirmsFetchError,
    FirmsHeaderError,
    FirmsStats,
    check_app_key,
    fetch_day,
    processing_level,
    window_date_indexes,
)
from .configurations import AuthenticateConfig, PullFireDetectionsConfig
from .core import action_title
from .dedupe import LOCATION_PRECISION, SentEvents

logger = logging.getLogger(__name__)

state_manager = IntegrationStateManager()

PULL_ACTION_ID = "pull_fire_detections"
RUN_LOCK_ID = f"{PULL_ACTION_ID}_lock"
# Longer than the runner's MAX_ACTION_EXECUTION_TIME (9 minutes), so a lease outlives the run that holds it.
RUN_LOCK_TTL_SECONDS = 600
EVENT_TYPE = "firms_rep"
EVENT_TITLE = "FIRMS Fire Detected"
EVENT_DETAIL_FIELDS = ("confidence", "frp", "bright_ti4", "bright_ti5", "scan", "track")
ACCEPTED_PROCESSING_LEVEL = "NRT"
EVENTS_BATCH_SIZE = 100
# Yesterday's and today's day files are read, so a sent event can be met again for up to two days.
FILE_WINDOW = timedelta(days=2)


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


NO_APP_KEY_MESSAGE = "No shared NASA Earthdata app key is available. Turn on 'Use your own key' and enter one."


def _app_key(auth_config: AuthenticateConfig) -> str:
    """The integration's own key when it uses one, otherwise the deployment's shared key."""
    if auth_config.use_own_key:
        return auth_config.app_key.get_secret_value()
    if settings.FIRMS_APP_KEY:
        return settings.FIRMS_APP_KEY
    raise IntegrationAuthError(NO_APP_KEY_MESSAGE)


@action_title("Authentication")
async def action_auth(integration, action_config: AuthenticateConfig):
    try:
        valid = await check_app_key(_app_key(action_config))
    except IntegrationAuthError:
        return {"valid_credentials": False, "error": NO_APP_KEY_MESSAGE}
    except FirmsFetchError:
        return {"valid_credentials": False, "error": "Could not reach NASA FIRMS to check the app key."}
    if not valid:
        return {"valid_credentials": False, "error": "NASA Earthdata rejected the app key."}
    return {"valid_credentials": True}


def _stored_app_key(integration) -> str:
    stored = find_config_for_action(configurations=integration.configurations, action_id="auth")
    try:
        # An integration with no stored authentication uses the shared key.
        auth_config = AuthenticateConfig.parse_obj(getattr(stored, "data", None) or {})
    except Exception:
        # The reason is left out: a validation message can quote the submitted values.
        raise IntegrationAuthError("The NASA Earthdata app key is not configured.") from None
    return _app_key(auth_config)


def _as_event(record: dict) -> dict:
    latitude, longitude = record["latitude"], record["longitude"]
    details = {name: record[name] for name in EVENT_DETAIL_FIELDS if name in record}
    details["reported_latitude"] = latitude
    details["reported_longitude"] = longitude
    return {
        "title": EVENT_TITLE,
        "event_type": EVENT_TYPE,
        "recorded_at": record["recorded_at"].isoformat(),
        # An intraday geolocation revision moves a detection slightly; snapping keeps it at one location.
        "location": {
            "lat": round(latitude, LOCATION_PRECISION),
            "lon": round(longitude, LOCATION_PRECISION),
        },
        "event_details": details,
    }


@action_title("Pull Fire Detections")
@crontab_schedule("*/15 * * * *")
@activity_logger()
async def action_pull_fire_detections(integration, action_config: PullFireDetectionsConfig):
    integration_id = str(integration.id)

    # Two runs working from the same stored cursors and sent-event record would both send the same detections.
    if not await state_manager.set_if_absent(integration_id, RUN_LOCK_ID, ttl_seconds=RUN_LOCK_TTL_SECONDS):
        return {"skipped": "another run is in progress"}
    try:
        return await _pull_fire_detections(integration, integration_id, action_config)
    finally:
        await state_manager.delete_state(integration_id, RUN_LOCK_ID)


async def _pull_fire_detections(integration, integration_id: str, action_config: PullFireDetectionsConfig):
    app_key = _stored_app_key(integration)
    now = _utcnow()
    product = FIRMS_PRODUCTS[action_config.satellite.value]
    state = await state_manager.get_state(integration_id, PULL_ACTION_ID)

    boundary = await resolve_boundary(
        integration_id=integration_id,
        group_id=action_config.spatial_feature_group,
        fallback_area=action_config.fallback_area,
        cache=state.get("boundary_cache") or {},
    )
    state["boundary_cache"] = boundary.cache
    for warning in boundary.warnings:
        await log_action_activity(
            integration_id=integration_id, action_id=PULL_ACTION_ID, title=warning, level=LogLevel.WARNING
        )
    if boundary.geometry is None:
        await state_manager.set_state(integration_id, PULL_ACTION_ID, state)
        await log_action_activity(
            integration_id=integration_id,
            action_id=PULL_ACTION_ID,
            title="No boundary available for FIRMS; nothing was sent.",
            level=LogLevel.WARNING,
        )
        return {"skipped": "no boundary", "events_sent": 0}

    inside = make_filter(boundary.geometry)
    confidence_levels = {level.value for level in action_config.confidence_alert_levels}
    alert_window = timedelta(hours=action_config.alert_window_hours)
    alert_window_start = now - alert_window

    window = [str(day) for day in window_date_indexes(now)]
    all_cursors = state.get("cursors") if isinstance(state.get("cursors"), dict) else {}
    # Day files that left the window are never read again; their cursors go for every satellite.
    all_cursors = {
        key: {day: cursor for day, cursor in days.items() if day in window}
        for key, days in all_cursors.items()
        if isinstance(days, dict)
    }
    cursors = all_cursors.setdefault(product.key, {})
    state["cursors"] = all_cursors

    sent = SentEvents(state.get("sent_events"))
    sent.prune(before=alert_window_start - FILE_WINDOW)
    # Detections queued in this run are not in `sent` until their batch has been delivered.
    queued = SentEvents()

    stats = FirmsStats()
    counts = {"events_sent": 0, "events_deduplicated": 0, "outside_area": 0, "skipped_processing_level": 0}
    failures = []

    try:
        for day in window:
            try:
                result = await fetch_day(
                    app_key=app_key,
                    region=action_config.region.value,
                    product=product,
                    date_index=int(day),
                    cursor=cursors.get(day),
                    stats=stats,
                )
            except FirmsAuthError:
                raise IntegrationAuthError("NASA Earthdata rejected the app key.") from None
            except (FirmsFetchError, FirmsHeaderError) as exc:
                # The day's cursor is left as it was, so the next run reads the same lines again.
                logger.warning("FIRMS day file %s could not be read: %s", day, exc)
                failures.append(exc)
                continue

            pending = []
            for record in result.records:
                if processing_level(record.get("version")) != ACCEPTED_PROCESSING_LEVEL:
                    counts["skipped_processing_level"] += 1
                    continue
                latitude, longitude, recorded_at = record["latitude"], record["longitude"], record["recorded_at"]
                if not inside(latitude, longitude):
                    counts["outside_area"] += 1
                    continue
                if record.get("confidence") not in confidence_levels or recorded_at < alert_window_start:
                    continue
                if sent.seen(recorded_at, latitude, longitude) or queued.seen(recorded_at, latitude, longitude):
                    counts["events_deduplicated"] += 1
                    continue
                queued.add(recorded_at, latitude, longitude)
                pending.append(record)

            for batch in generate_batches(pending, EVENTS_BATCH_SIZE):
                await send_events_to_gundi([_as_event(record) for record in batch], integration_id=integration_id)
                for record in batch:
                    sent.add(record["recorded_at"], record["latitude"], record["longitude"])
                counts["events_sent"] += len(batch)

            # Only once everything read from the day has been delivered may the next run skip past it.
            if result.cursor is not None:
                cursors[day] = result.cursor
    finally:
        state["sent_events"] = sent.to_state()
        await state_manager.set_state(integration_id, PULL_ACTION_ID, state)

    if failures:
        message = f"{len(failures)} of {len(window)} FIRMS day files could not be read."
        if all(isinstance(exc, FirmsFetchError) and exc.status_code is None for exc in failures):
            raise IntegrationConnectionError(message)
        raise IntegrationBadResponseError(message)

    return {**counts, "boundary_source": boundary.source, **asdict(stats)}
