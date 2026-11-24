from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from gundi_core.events import LogLevel

from app.actions import boundaries, handlers
from app.actions.boundary_source import ResolvedBoundary
from app.actions.event_type import EventTypeCheck, EventTypeUnavailable
from app.actions.client import DayResult, FirmsAuthError, FirmsFetchError, FirmsHeaderError
from app.actions.configurations import AuthenticateConfig, PullFireDetectionsConfig
from app.actions.core import get_actions
from app.services.errors import IntegrationAuthError, IntegrationBadResponseError, IntegrationConnectionError

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
TODAY, YESTERDAY = 2026327, 2026326
INTEGRATION_ID = "779ff3ab-5589-4f4c-9e0a-ae8d6c9edff0"
SQUARE = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}
UNIT_SQUARE = boundaries.geometry_from_polygon(SQUARE)
CUR = {"etag": '"a"', "offset": 10, "prefix_sha256": "abc", "columns": ["latitude"]}
LOCK = "pull_fire_detections_lock"


def rec(lat, lon, minute, confidence="nominal", version="2.0NRT"):
    return {
        "latitude": lat,
        "longitude": lon,
        "bright_ti4": 338.1,
        "scan": 0.43,
        "track": 0.46,
        "acq_date": "2026-11-23",
        "acq_time": f"00:{minute:02d}",
        "satellite": "N20",
        "confidence": confidence,
        "version": version,
        "bright_ti5": 281.5,
        "frp": 1.9,
        "daynight": "N",
        "recorded_at": datetime(2026, 11, 23, 0, minute, tzinfo=timezone.utc),
    }


class FakeStateManager:
    def __init__(self):
        self.docs = {}
        self.lock_free = True
        self.deleted = []

    async def get_state(self, integration_id, action_id, source_id="no-source"):
        return dict(self.docs.get(action_id, {}))

    async def set_state(self, integration_id, action_id, state, source_id="no-source"):
        self.docs[action_id] = state

    async def set_if_absent(self, integration_id, action_id, *, ttl_seconds, source_id="no-source"):
        self.lock_args = (action_id, ttl_seconds)
        return self.lock_free

    async def delete_state(self, integration_id, action_id, source_id="no-source"):
        self.deleted.append(action_id)

    @property
    def state(self):
        return self.docs.setdefault("pull_fire_detections", {})


@pytest.fixture
def integration():
    auth = SimpleNamespace(action=SimpleNamespace(value="auth"), data={"use_own_key": True, "app_key": "the-key"})
    return SimpleNamespace(id=INTEGRATION_ID, configurations=[auth])


@pytest.fixture
def env(mocker):
    mocker.patch("app.services.activity_logger.publish_event", new=AsyncMock())
    mocker.patch.object(handlers, "_utcnow", return_value=NOW)
    fake = FakeStateManager()
    mocker.patch.object(handlers, "state_manager", fake)
    return SimpleNamespace(
        fake=fake,
        resolve=mocker.patch.object(
            handlers,
            "resolve_boundary",
            new=AsyncMock(return_value=ResolvedBoundary(UNIT_SQUARE, "earthranger", [], {})),
        ),
        fetch=mocker.patch.object(handlers, "fetch_day", new=AsyncMock()),
        send=mocker.patch.object(handlers, "send_events_to_gundi", new=AsyncMock(return_value=[])),
        log=mocker.patch.object(handlers, "log_action_activity", new=AsyncMock()),
        check=mocker.patch.object(handlers, "check_app_key", new=AsyncMock(return_value=True)),
        shared_key=mocker.patch.object(handlers.settings, "FIRMS_APP_KEY", None),
        ensure=mocker.patch.object(
            handlers, "ensure_event_type_on_destinations", new=AsyncMock(return_value=EventTypeCheck())
        ),
    )


@pytest.fixture
def run(env, integration):
    async def _run(today_records, satellite="noaa-20", **config):
        if today_records is not None:

            async def fetch_day(**kwargs):
                if kwargs["date_index"] == TODAY:
                    return DayResult(list(today_records), CUR)
                return DayResult([], None)

            env.fetch.side_effect = fetch_day
        config.setdefault("spatial_feature_group", "g1")
        action_config = PullFireDetectionsConfig(region="Southern_Africa", satellite=satellite, **config)
        return await handlers.action_pull_fire_detections(integration=integration, action_config=action_config)

    return _run


async def test_sends_event_for_confident_recent_detection_inside(env, run):
    result = await run([rec(0.51234, 0.49876, 42)])
    (events,), kwargs = env.send.await_args
    assert events == [
        {
            "title": "FIRMS Fire Detected",
            "event_type": "firms_rep",
            "recorded_at": "2026-11-23T00:42:00+00:00",
            "location": {"lat": 0.512, "lon": 0.499},
            "priority": 100,
            "event_details": {
                "confidence": "nominal",
                "frp": 1.9,
                "bright_ti4": 338.1,
                "bright_ti5": 281.5,
                "scan": 0.43,
                "track": 0.46,
                "reported_latitude": 0.51234,
                "reported_longitude": 0.49876,
            },
        }
    ]
    assert kwargs["integration_id"] == INTEGRATION_ID
    assert result["events_sent"] == 1 and result["boundary_source"] == "earthranger"
    assert result["records_read"] == 0  # counted by the client, which is mocked here
    assert env.fake.state["cursors"]["noaa-20"][str(TODAY)] == CUR
    assert env.fake.state["sent_events"] == ["2026-11-23T00:42+00:00|512|499"]


async def test_fetches_with_the_stored_app_key_region_and_boundary_inputs(env, run):
    await run([], fallback_area=SQUARE)
    first = env.fetch.await_args_list[0].kwargs
    assert first["app_key"] == "the-key" and first["region"] == "Southern_Africa"
    assert first["product"].key == "noaa-20"
    resolve_kwargs = env.resolve.await_args.kwargs
    assert resolve_kwargs["integration_id"] == INTEGRATION_ID and resolve_kwargs["group_id"] == "g1"
    assert boundaries.geometry_from_polygon(resolve_kwargs["fallback_area"]).equals(UNIT_SQUARE)


@pytest.mark.parametrize(
    "record,counter",
    [
        (rec(5.0, 5.0, 1), "outside_area"),
        (rec(0.5, 0.5, 1, confidence="low"), None),
        (rec(0.5, 0.5, 1, version="2.0URT"), "skipped_processing_level"),
        (rec(0.5, 0.5, 1, version="2.0"), "skipped_processing_level"),
    ],
)
async def test_filtered_detections_send_nothing(env, run, record, counter):
    result = await run([record])
    assert result["events_sent"] == 0 and env.send.await_count == 0
    if counter:
        assert result[counter] == 1


async def test_minimum_confidence_is_honoured(env, run):
    records = [rec(0.5, 0.5, 1, confidence="low"), rec(0.6, 0.6, 2), rec(0.7, 0.7, 3, confidence="high")]
    result = await run(records, minimum_confidence="high")
    assert result["events_sent"] == 1
    assert env.send.await_args.args[0][0]["event_details"]["confidence"] == "high"
    env.send.reset_mock()
    env.fake.docs.clear()
    assert (await run(records, minimum_confidence="low"))["events_sent"] == 3


async def test_detection_older_than_alert_window_is_not_sent(env, run):
    old = rec(0.5, 0.5, 1)
    old["recorded_at"] = NOW - timedelta(hours=25)
    assert (await run([old]))["events_sent"] == 0
    assert (await run([old], alert_window_hours=26))["events_sent"] == 1


async def test_second_run_and_revised_position_are_deduplicated(env, run):
    await run([rec(0.51234, 0.49876, 42)])
    env.send.reset_mock()
    result = await run([rec(0.51234, 0.49876, 42), rec(0.51251, 0.49949, 42)])
    assert result["events_sent"] == 0 and result["events_deduplicated"] == 2 and env.send.await_count == 0


async def test_duplicates_within_one_file_send_once(env, run):
    result = await run([rec(0.5, 0.5, 7), rec(0.5, 0.5, 7)])
    assert result["events_sent"] == 1 and result["events_deduplicated"] == 1
    assert len(env.send.await_args.args[0]) == 1


async def test_no_boundary_sends_nothing_and_warns(env, run):
    env.resolve.return_value = ResolvedBoundary(None, "none", ["x"], {})
    result = await run([rec(0.5, 0.5, 1)])
    assert result == {"skipped": "no boundary", "events_sent": 0}
    assert env.fetch.await_count == 0 and env.send.await_count == 0
    titles = [c.kwargs["title"] for c in env.log.await_args_list]
    assert titles == ["x", "No boundary available for FIRMS; nothing was sent."]
    assert all(c.kwargs["level"] == LogLevel.WARNING for c in env.log.await_args_list)
    assert LOCK in env.fake.deleted


async def test_fallback_boundary_logs_each_warning_and_saves_cache(env, run):
    cache = {"a": {"group_id": "g1", "geometry": SQUARE}}
    env.resolve.return_value = ResolvedBoundary(UNIT_SQUARE, "cached", ["ER unreachable"], cache)
    result = await run([])
    assert env.fake.state["boundary_cache"] == cache and result["boundary_source"] == "cached"
    assert [c.kwargs["title"] for c in env.log.await_args_list] == ["ER unreachable"]
    assert env.log.await_args.kwargs["level"] == LogLevel.WARNING


async def test_stored_boundary_cache_is_passed_to_the_resolver(env, run):
    cache = {"a": {"group_id": "g1", "geometry": SQUARE}}
    env.fake.state["boundary_cache"] = cache
    await run([])
    assert env.resolve.await_args.kwargs["cache"] == cache


async def test_send_failure_keeps_cursor_and_does_not_mark_sent(env, run):
    env.send.side_effect = RuntimeError("gundi down")
    with pytest.raises(RuntimeError):
        await run([rec(0.5, 0.5, 1)])
    assert str(TODAY) not in env.fake.state.get("cursors", {}).get("noaa-20", {})
    assert env.fake.state.get("sent_events", []) == []
    assert LOCK in env.fake.deleted


async def test_batches_sent_before_a_failure_stay_marked_as_sent(env, run):
    env.send.side_effect = [[], RuntimeError("gundi down")]
    records = [rec(0.005 * i, 0.5, 1) for i in range(1, 151)]
    with pytest.raises(RuntimeError):
        await run(records)
    assert len(env.fake.state["sent_events"]) == 100
    assert str(TODAY) not in env.fake.state.get("cursors", {}).get("noaa-20", {})


async def test_auth_failure_raises_integration_auth_error(env, run):
    env.fetch.side_effect = FirmsAuthError("x")
    with pytest.raises(IntegrationAuthError):
        await run(None)
    assert env.fetch.await_count == 1 and LOCK in env.fake.deleted


@pytest.mark.parametrize("failure", [FirmsFetchError("x", status_code=500), FirmsHeaderError("x")])
async def test_one_bad_day_file_does_not_block_the_other(env, run, failure):
    env.fetch.side_effect = [failure, DayResult([rec(0.5, 0.5, 1)], CUR)]
    with pytest.raises(IntegrationBadResponseError):
        await run(None)
    assert env.send.await_count == 1
    cursors = env.fake.state["cursors"]["noaa-20"]
    assert str(YESTERDAY) not in cursors and cursors[str(TODAY)] == CUR


async def test_unreachable_nasa_is_a_connection_error(env, run):
    env.fetch.side_effect = FirmsFetchError("x")
    with pytest.raises(IntegrationConnectionError):
        await run(None)
    assert env.fetch.await_count == 2


async def test_rollover_drops_stale_cursors_and_keys(env, run):
    env.fake.state.update(
        {
            "cursors": {"noaa-20": {"2026320": CUR, str(YESTERDAY): CUR}},
            "sent_events": ["2026-11-16T00:42+00:00|500|500", "2026-11-22T23:59+00:00|500|500"],
        }
    )
    await run([])
    cursors = env.fake.state["cursors"]["noaa-20"]
    assert "2026320" not in cursors and str(YESTERDAY) in cursors
    assert env.fake.state["sent_events"] == ["2026-11-22T23:59+00:00|500|500"]
    assert [c.kwargs["date_index"] for c in env.fetch.await_args_list] == [YESTERDAY, TODAY]


async def test_cursor_is_read_for_the_configured_satellite(env, run):
    env.fake.state["cursors"] = {"noaa-21": {str(TODAY): CUR}, "noaa-20": {str(TODAY): {"etag": "other"}}}
    await run([], satellite="noaa-21")
    today_call = env.fetch.await_args_list[1].kwargs
    assert today_call["cursor"] == CUR and today_call["product"].key == "noaa-21"
    assert env.fake.state["cursors"]["noaa-20"] == {str(TODAY): {"etag": "other"}}


async def test_overlapping_run_is_skipped(env, run):
    env.fake.lock_free = False
    assert await run([rec(0.5, 0.5, 1)]) == {"skipped": "another run is in progress"}
    assert env.fetch.await_count == 0 and env.resolve.await_count == 0
    assert env.fake.lock_args == (LOCK, 600)
    assert LOCK not in env.fake.deleted  # the lease belongs to the other run


async def test_more_than_100_events_are_sent_in_batches(env, run):
    records = [rec(0.01 * i, 0.5, 1) for i in range(1, 99)] + [rec(0.5, 0.01 * i, 2) for i in range(1, 99)]
    result = await run(records)
    assert [len(c.args[0]) for c in env.send.await_args_list] == [100, 96]
    assert result["events_sent"] == 196


@pytest.mark.parametrize("auth_data", [None, {}, {"use_own_key": False, "app_key": "ignored"}])
async def test_shared_key_is_used_unless_the_integration_uses_its_own(env, run, integration, mocker, auth_data):
    mocker.patch.object(handlers.settings, "FIRMS_APP_KEY", "shared-key")
    if auth_data is None:
        integration.configurations = []
    else:
        integration.configurations[0].data = auth_data
    await run([])
    assert env.fetch.await_args_list[0].kwargs["app_key"] == "shared-key"


async def test_own_key_wins_over_the_shared_key(env, run, mocker):
    mocker.patch.object(handlers.settings, "FIRMS_APP_KEY", "shared-key")
    await run([])
    assert env.fetch.await_args_list[0].kwargs["app_key"] == "the-key"


@pytest.mark.parametrize("auth_data", [None, {}, {"use_own_key": True}])
async def test_no_usable_app_key_is_an_auth_error(env, run, integration, auth_data):
    if auth_data is None:
        integration.configurations = []
    else:
        integration.configurations[0].data = auth_data
    with pytest.raises(IntegrationAuthError):
        await run([])
    assert env.fetch.await_count == 0


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (True, {"valid_credentials": True}),
        (False, {"valid_credentials": False, "error": "NASA Earthdata rejected the app key."}),
        (
            FirmsFetchError("x"),
            {"valid_credentials": False, "error": "Could not reach NASA FIRMS to check the app key."},
        ),
    ],
)
async def test_action_auth(env, integration, outcome, expected):
    if isinstance(outcome, Exception):
        env.check.side_effect = outcome
    else:
        env.check.return_value = outcome
    config = AuthenticateConfig(use_own_key=True, app_key="k")
    assert await handlers.action_auth(integration=integration, action_config=config) == expected
    env.check.assert_awaited_once_with("k")


async def test_action_auth_checks_the_shared_key(env, integration, mocker):
    mocker.patch.object(handlers.settings, "FIRMS_APP_KEY", "shared-key")
    result = await handlers.action_auth(integration=integration, action_config=AuthenticateConfig())
    assert result == {"valid_credentials": True}
    env.check.assert_awaited_once_with("shared-key")


async def test_action_auth_without_any_key(env, integration):
    result = await handlers.action_auth(integration=integration, action_config=AuthenticateConfig())
    assert result["valid_credentials"] is False and "Use your own key" in result["error"]
    assert env.check.await_count == 0


async def test_event_type_is_checked_once_before_the_first_send(env, run):
    order = []
    env.ensure.side_effect = lambda integration_id: order.append("ensure") or EventTypeCheck()
    env.send.side_effect = lambda events, **kwargs: order.append("send") or []
    await run([rec(0.005 * i, 0.5, 1) for i in range(1, 151)])
    assert order == ["ensure", "send", "send"]
    env.ensure.assert_awaited_once_with(INTEGRATION_ID)


async def test_event_type_is_not_checked_when_nothing_is_sent(env, run):
    await run([rec(5.0, 5.0, 1)])
    assert env.ensure.await_count == 0


async def test_created_event_type_and_warnings_are_logged(env, run):
    env.ensure.return_value = EventTypeCheck(created=["a.pamdas.org"], warnings=["could not check b"])
    result = await run([rec(0.5, 0.5, 1)])
    logged = [(c.kwargs["title"], c.kwargs["level"]) for c in env.log.await_args_list]
    assert logged == [
        ("Created the 'firms_rep' event type on a.pamdas.org.", LogLevel.INFO),
        ("could not check b", LogLevel.WARNING),
    ]
    assert result["events_sent"] == 1


async def test_missing_event_type_that_cannot_be_created_sends_nothing(env, run):
    env.ensure.side_effect = EventTypeUnavailable("a.pamdas.org", "ERClientPermissionDenied")
    with pytest.raises(IntegrationBadResponseError) as raised:
        await run([rec(0.5, 0.5, 1)])
    assert "a.pamdas.org" in str(raised.value)
    assert env.send.await_count == 0
    assert env.fake.state.get("sent_events", []) == []
    assert str(TODAY) not in env.fake.state.get("cursors", {}).get("noaa-20", {})


async def test_priority_follows_the_configured_rules(env, run):
    strong = rec(0.3, 0.3, 3, confidence="low")
    strong["frp"] = 75.0
    await run(
        [rec(0.1, 0.1, 1, confidence="low"), rec(0.2, 0.2, 2, confidence="high"), strong, rec(0.4, 0.4, 4)],
        minimum_confidence="low",
        priority_nominal="amber",
        red_frp_threshold_mw=75,
    )
    assert [event["priority"] for event in env.send.await_args.args[0]] == [0, 200, 300, 200]


async def test_fire_below_the_chosen_threshold_keeps_its_confidence_priority(env, run):
    strong = rec(0.3, 0.3, 3, confidence="high")
    strong["frp"] = 199.9
    await run([strong], red_frp_threshold_mw=200)
    assert env.send.await_args.args[0][0]["priority"] == 200


async def test_schedule_and_discovery():
    assert set(get_actions()) == {"auth", "pull_fire_detections"}
    assert handlers.action_pull_fire_detections.crontab_schedule.minute == "*/15"
    assert handlers.action_pull_fire_detections.crontab_schedule.hour == "*"
