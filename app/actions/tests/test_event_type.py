from unittest.mock import AsyncMock, MagicMock

import pytest
from erclient.er_errors import ERClientBadRequest, ERClientNotFound, ERClientPermissionDenied

from app.actions import event_type as et
from app.actions.boundary_source import ERDestination

pytestmark = pytest.mark.asyncio

A = ERDestination("a", "https://a.pamdas.org", token="tok-secret")
B = ERDestination("b", "https://b.pamdas.org", token="t")


@pytest.fixture
def er(mocker):
    client = MagicMock()
    client.get_event_type = AsyncMock(return_value=None)  # a v2 type reads as null on this route
    client.get_event_categories = AsyncMock(return_value=[{"value": "analyzer_event"}])
    client.post_event_category = AsyncMock()
    client.post_event_type = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.factory = mocker.patch.object(et, "er_client", return_value=client)
    return client


@pytest.fixture
def dests(mocker):
    return mocker.patch.object(et, "get_er_destinations", new=AsyncMock(return_value=[A]))


async def test_existing_type_is_left_alone(er):
    assert await et.ensure_event_type(A) is False
    er.get_event_type.assert_awaited_once_with("firms_rep")
    assert er.post_event_type.await_count == 0 and er.get_event_categories.await_count == 0


async def test_missing_type_is_created_as_the_plugin_creates_it(er):
    er.get_event_type.side_effect = ERClientNotFound()
    assert await et.ensure_event_type(A) is True
    (body,), kwargs = er.post_event_type.await_args
    assert kwargs == {"version": "v2.0"}
    assert {key: value for key, value in body.items() if key != "schema"} == {
        "value": "firms_rep",
        "display": "FIRMS Report",
        "category": "analyzer_event",
        "icon": "firms_rep",
        "default_priority": 100,
        "readonly": True,
    }
    fields = [
        "confidence",
        "frp",
        "bright_ti4",
        "bright_ti5",
        "scan",
        "track",
        "reported_latitude",
        "reported_longitude",
    ]
    assert list(body["schema"]["json"]["properties"]) == fields
    assert list(body["schema"]["ui"]["fields"]) == fields
    assert [item["name"] for item in body["schema"]["ui"]["sections"]["section-1"]["leftColumn"]] == fields
    assert body["schema"]["json"]["unevaluatedProperties"] is False
    assert er.post_event_category.await_count == 0


async def test_missing_category_is_created_first(er):
    er.get_event_type.side_effect = ERClientNotFound()
    er.get_event_categories.return_value = [{"value": "security"}]
    assert await et.ensure_event_type(A) is True
    er.get_event_categories.assert_awaited_once_with(include_inactive=True)
    er.post_event_category.assert_awaited_once_with({"value": "analyzer_event", "display": "Analyzer Events"})
    assert er.post_event_type.await_count == 1


async def test_failed_creation_is_reported_without_the_response_text(er):
    er.get_event_type.side_effect = ERClientNotFound()
    er.post_event_type.side_effect = ERClientBadRequest("tok-secret in body")
    with pytest.raises(et.EventTypeUnavailable) as raised:
        await et.ensure_event_type(A)
    message = str(raised.value)
    assert "a.pamdas.org" in message and "ERClientBadRequest" in message and "tok-secret" not in message


async def test_a_check_that_fails_is_not_a_missing_type(er):
    er.get_event_type.side_effect = ERClientPermissionDenied("nope")
    with pytest.raises(ERClientPermissionDenied):
        await et.ensure_event_type(A)
    assert er.post_event_type.await_count == 0


async def test_every_destination_is_checked_and_creations_are_reported(er, dests):
    dests.return_value = [A, B]
    er.get_event_type.side_effect = [None, ERClientNotFound()]
    result = await et.ensure_event_type_on_destinations("i")
    assert result.created == ["b.pamdas.org"] and result.warnings == []
    assert [call.args[0] for call in er.factory.call_args_list] == [A, B]


async def test_destination_that_cannot_be_checked_is_a_warning(er, dests):
    er.get_event_type.side_effect = ERClientPermissionDenied("tok-secret")
    result = await et.ensure_event_type_on_destinations("i")
    assert result.created == [] and len(result.warnings) == 1
    assert "a.pamdas.org" in result.warnings[0] and "tok-secret" not in result.warnings[0]


async def test_unreadable_destination_is_skipped(er, dests):
    dests.return_value = [ERDestination("a", "https://a.pamdas.org", unreadable=True)]
    result = await et.ensure_event_type_on_destinations("i")
    assert result.created == [] and len(result.warnings) == 1 and er.factory.call_count == 0


async def test_destination_lookup_failure_is_a_warning(er, dests):
    dests.side_effect = RuntimeError("secret")
    result = await et.ensure_event_type_on_destinations("i")
    assert len(result.warnings) == 1 and "secret" not in result.warnings[0] and er.factory.call_count == 0


async def test_type_that_cannot_be_created_stops_the_run(er, dests):
    er.get_event_type.side_effect = ERClientNotFound()
    er.post_event_type.side_effect = ERClientPermissionDenied("x")
    with pytest.raises(et.EventTypeUnavailable):
        await et.ensure_event_type_on_destinations("i")
