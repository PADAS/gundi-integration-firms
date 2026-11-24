import pytest

from app.actions.priority import PRIORITY_VALUES, event_priority

MAPPING = {"low": "gray", "nominal": "green", "high": "amber"}


def test_priority_values_are_earthrangers():
    assert PRIORITY_VALUES == {"gray": 0, "green": 100, "amber": 200, "red": 300}


@pytest.mark.parametrize("confidence,expected", [("low", 0), ("nominal", 100), ("high", 200)])
def test_priority_follows_confidence(confidence, expected):
    assert event_priority(confidence, 1.0, by_confidence=MAPPING, red_frp_threshold=50) == expected


@pytest.mark.parametrize(
    "frp,expected",
    [(49.99, 0), (50, 300), (50.0, 300), (812.4, 300), (None, 0), ("n/a", 0), (float("nan"), 0)],
)
def test_strong_fire_is_raised_to_red(frp, expected):
    assert event_priority("low", frp, by_confidence=MAPPING, red_frp_threshold=50) == expected


def test_no_threshold_means_no_override():
    assert event_priority("high", 9999.0, by_confidence=MAPPING, red_frp_threshold=None) == 200


def test_override_never_lowers_a_priority():
    mapping = {"low": "red", "nominal": "red", "high": "red"}
    assert event_priority("low", 0.1, by_confidence=mapping, red_frp_threshold=50) == 300


def test_unknown_confidence_has_no_priority():
    assert event_priority("certain", 1.0, by_confidence=MAPPING, red_frp_threshold=50) is None
    assert event_priority("certain", 80.0, by_confidence=MAPPING, red_frp_threshold=50) == 300
