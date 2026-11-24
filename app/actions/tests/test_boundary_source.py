import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from gundi_client_v2.errors import GundiAPIError

from app.actions import boundaries as b
from app.actions import boundary_source as bs
from app.actions.boundary_source import ERDestination

pytestmark = pytest.mark.asyncio

SQUARE = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}
SQUARE2 = {"type": "Polygon", "coordinates": [[[2, 2], [2, 3], [3, 3], [3, 2], [2, 2]]]}


def fc(geom):
    return {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": geom}]}


GROUP = {"features": [fc(SQUARE)]}
A = ERDestination("a", "https://a.pamdas.org", token="t")
B = ERDestination("b", "https://b.pamdas.org", token="t")


@pytest.fixture
def dests(mocker):
    return mocker.patch.object(bs, "get_er_destinations", new=AsyncMock(return_value=[A]))


@pytest.fixture
def fetch(mocker):
    return mocker.patch.object(bs, "fetch_feature_group", new=AsyncMock(return_value=GROUP))


async def resolve(group_id="g1", fallback=None, cache=None):
    return await bs.resolve_boundary(
        integration_id="i", group_id=group_id, fallback_area=fallback, cache=cache or {}
    )


async def test_live_read_wins_and_is_cached(dests, fetch):
    r = await resolve(fallback=SQUARE2)
    assert r.source == "earthranger" and r.warnings == []
    assert r.cache == {"a": {"group_id": "g1", "geometry": b.to_geojson(r.geometry)}}
    inside = b.make_filter(r.geometry)
    assert inside(0.5, 0.5) and not inside(2.5, 2.5)
    fetch.assert_awaited_once_with(A, "g1")


async def test_unreachable_destination_uses_its_cache(dests, fetch):
    fetch.side_effect = httpx.ConnectError("secret-token")
    cache = {"a": {"group_id": "g1", "geometry": SQUARE}}
    r = await resolve(cache=cache, fallback=SQUARE2)
    assert r.source == "cached" and b.make_filter(r.geometry)(0.5, 0.5)
    assert len(r.warnings) == 1 and "a.pamdas.org" in r.warnings[0] and "secret-token" not in r.warnings[0]
    assert r.cache == cache


async def test_cache_for_another_group_is_ignored(dests, fetch):
    fetch.side_effect = httpx.ConnectError("x")
    r = await resolve(cache={"a": {"group_id": "old", "geometry": SQUARE}}, fallback=SQUARE2)
    assert r.source == "config" and b.make_filter(r.geometry)(2.5, 2.5) and "a" not in r.cache


async def test_unreadable_cache_entry_is_ignored(dests, fetch):
    fetch.side_effect = httpx.ConnectError("x")
    r = await resolve(cache={"a": {"group_id": "g1", "geometry": {"type": "Nonsense"}}}, fallback=SQUARE2)
    assert r.source == "config"


async def test_two_destinations_union_live_and_cached(dests, fetch):
    dests.return_value = [A, B]
    fetch.side_effect = [GROUP, httpx.ConnectError("x")]
    r = await resolve(cache={"b": {"group_id": "g1", "geometry": SQUARE2}})
    inside = b.make_filter(r.geometry)
    assert r.source == "cached" and inside(0.5, 0.5) and inside(2.5, 2.5)
    assert set(r.cache) == {"a", "b"}


async def test_cache_of_a_disconnected_destination_is_dropped(dests, fetch):
    r = await resolve(cache={"gone": {"group_id": "g1", "geometry": SQUARE2}})
    assert set(r.cache) == {"a"} and r.source == "earthranger"


async def test_disconnected_destination_boundary_does_not_return_during_a_lookup_outage(dests, fetch):
    cache = {"a": {"group_id": "g1", "geometry": SQUARE}, "b": {"group_id": "g1", "geometry": SQUARE2}}
    first = await resolve(cache=cache)  # only A is still connected
    dests.side_effect = RuntimeError("gundi down")
    second = await resolve(cache=first.cache)
    inside = b.make_filter(second.geometry)
    assert second.source == "cached" and inside(0.5, 0.5) and not inside(2.5, 2.5)


async def test_group_with_no_polygons_falls_back(dests, fetch):
    fetch.return_value = {"features": []}
    r = await resolve(fallback=json.dumps(SQUARE2))
    assert r.source == "config" and r.warnings and b.make_filter(r.geometry)(2.5, 2.5)


async def test_no_group_selected_uses_config_without_warning(dests, fetch):
    r = await resolve(group_id=None, fallback=SQUARE2)
    assert r.source == "config" and r.warnings == [] and fetch.await_count == 0 and dests.await_count == 0


async def test_nothing_resolves(dests, fetch):
    dests.return_value = []
    r = await resolve(fallback=None)
    assert r.geometry is None and r.source == "none" and r.warnings


async def test_unusable_fallback_area_resolves_to_none(dests, fetch):
    dests.return_value = []
    r = await resolve(fallback="not json")
    assert r.geometry is None and r.source == "none"


async def test_destination_lookup_failure_is_not_fatal(dests, fetch):
    dests.side_effect = RuntimeError("secret")
    r = await resolve(fallback=SQUARE2)
    assert r.source == "config" and r.warnings and "secret" not in " ".join(r.warnings)


def _integration(base_url, auth_data):
    config = SimpleNamespace(data=auth_data) if auth_data is not None else None
    return SimpleNamespace(base_url=base_url, get_action_config=lambda action: config if action == "auth" else None)


def _dest(dest_id, type_value="earth_ranger", base_url=None):
    return SimpleNamespace(id=dest_id, type=SimpleNamespace(value=type_value), base_url=base_url)


def _gundi(mocker, connection=None, integrations=None, connection_error=None):
    client = MagicMock()
    client.get_connection_details = AsyncMock(return_value=connection, side_effect=connection_error)
    client.get_integration_details = AsyncMock(side_effect=lambda dest_id: integrations[dest_id])
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=client)
    manager.__aexit__ = AsyncMock(return_value=False)
    mocker.patch.object(bs, "GundiClient", return_value=manager)
    return client


async def test_get_er_destinations_reads_both_auth_types_and_skips_the_rest(mocker):
    connection = SimpleNamespace(
        destinations=[_dest(i) for i in ("d1", "d2", "d3", "d4", "d5")]
    )
    _gundi(
        mocker,
        connection,
        {
            "d1": _integration("https://one.pamdas.org", {"authentication_type": "token", "token": "tok"}),
            "d2": _integration(
                "https://two.pamdas.org/",
                {"authentication_type": "username_password", "username": "u", "password": "p", "token": ""},
            ),
            "d3": _integration("https://three.example.org", None),  # no auth configuration
            "d4": _integration("https://four.example.org", {"username": "u"}),  # no usable credentials
            "d5": _integration("", {"token": "tok"}),  # no site URL
        },
    )
    assert await bs.get_er_destinations("i") == [
        ERDestination("d1", "https://one.pamdas.org", token="tok"),
        ERDestination("d2", "https://two.pamdas.org/", username="u", password="p"),
    ]


async def test_get_er_destinations_without_destinations(mocker):
    _gundi(mocker, SimpleNamespace(destinations=None), {})
    assert await bs.get_er_destinations("i") == []


async def test_get_er_destinations_when_integration_is_not_a_provider(mocker):
    _gundi(mocker, connection_error=GundiAPIError(404, "not found"))
    assert await bs.get_er_destinations("i") == []


async def test_get_er_destinations_propagates_other_api_errors(mocker):
    _gundi(mocker, connection_error=GundiAPIError(500, "boom"))
    with pytest.raises(GundiAPIError):
        await bs.get_er_destinations("i")


async def test_fetch_feature_group_builds_the_er_client_from_the_site_root(mocker):
    er = MagicMock()
    er.get_feature_group = AsyncMock(return_value=GROUP)
    er.__aenter__ = AsyncMock(return_value=er)
    er.__aexit__ = AsyncMock(return_value=False)
    factory = mocker.patch.object(bs, "AsyncERClient", return_value=er)
    dest = ERDestination("d", "https://site.pamdas.org:443/some/path", username="u", password="p")
    assert await bs.fetch_feature_group(dest, "g1") == GROUP
    kwargs = factory.call_args.kwargs
    assert kwargs["service_root"] == "https://site.pamdas.org/api/v1.0"
    assert kwargs["token_url"] == "https://site.pamdas.org/oauth2/token"
    assert (kwargs["username"], kwargs["password"], kwargs["token"]) == ("u", "p", None)
    assert kwargs["client_id"] == "das_web_client"
    er.get_feature_group.assert_awaited_once_with("g1")


async def test_get_er_destinations_skips_destinations_that_are_not_earthranger(mocker):
    """Another system's credentials must never be sent to EarthRanger endpoints on its host."""
    connection = SimpleNamespace(
        destinations=[_dest("er"), _dest("smart", "smart_connect"), _dest("untyped", None), _dest("old", "earthranger")]
    )
    connection.destinations[2].type = None
    client = _gundi(
        mocker,
        connection,
        {
            "er": _integration("https://er.pamdas.org", {"token": "tok"}),
            "smart": _integration("https://smart.example.org", {"username": "u", "password": "p"}),
            "untyped": _integration("https://x.example.org", {"token": "tok"}),
            "old": _integration("https://old.pamdas.org", {"token": "tok2"}),
        },
    )
    assert await bs.get_er_destinations("i") == [
        ERDestination("er", "https://er.pamdas.org", token="tok"),
        ERDestination("old", "https://old.pamdas.org", token="tok2"),
    ]
    assert [c.args[0] for c in client.get_integration_details.await_args_list] == ["er", "old"]


async def test_get_er_destinations_isolates_a_destination_whose_details_fail(mocker):
    connection = SimpleNamespace(destinations=[_dest("d1", base_url="https://one.pamdas.org"), _dest("d2")])
    client = _gundi(mocker, connection, {"d2": _integration("https://two.pamdas.org", {"token": "tok"})})
    client.get_integration_details.side_effect = [GundiAPIError(500, "boom"), client.get_integration_details.side_effect("d2")]
    assert await bs.get_er_destinations("i") == [
        ERDestination("d1", "https://one.pamdas.org", unreadable=True),
        ERDestination("d2", "https://two.pamdas.org", token="tok"),
    ]


async def test_unreadable_destination_contributes_its_cached_boundary(dests, fetch):
    dests.return_value = [ERDestination("a", "https://a.pamdas.org", unreadable=True), B]
    r = await resolve(cache={"a": {"group_id": "g1", "geometry": SQUARE2}}, fallback=None)
    inside = b.make_filter(r.geometry)
    assert r.source == "cached" and inside(0.5, 0.5) and inside(2.5, 2.5)
    fetch.assert_awaited_once_with(B, "g1")


async def test_destination_lookup_failure_uses_the_cached_boundary_before_the_fallback(dests, fetch):
    dests.side_effect = RuntimeError("gundi down")
    cache = {"a": {"group_id": "g1", "geometry": SQUARE}}
    r = await resolve(cache=cache, fallback=SQUARE2)
    inside = b.make_filter(r.geometry)
    assert r.source == "cached" and inside(0.5, 0.5) and not inside(2.5, 2.5)
    assert r.cache == cache and r.warnings


async def test_destination_repr_hides_credentials():
    text = repr(ERDestination("a", "https://a.pamdas.org", token="tok-secret", username="u", password="pw-secret"))
    assert "tok-secret" not in text and "pw-secret" not in text
