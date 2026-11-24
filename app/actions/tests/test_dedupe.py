from datetime import datetime, timedelta, timezone

from app.actions.dedupe import LOCATION_PRECISION, SentEvents

T = datetime(2026, 11, 23, 0, 42, tzinfo=timezone.utc)


def test_precision_matches_the_plugin():
    assert LOCATION_PRECISION == 3


def test_same_and_revised_position_are_seen():
    s = SentEvents()
    s.add(T, -1.25031, 35.12345)
    assert s.seen(T, -1.25031, 35.12345)
    assert s.seen(T, -1.25049, 35.12351)  # same cell
    assert s.seen(T, -1.25051, 35.12449)  # neighbouring cell on both axes


def test_adjacent_viirs_pixel_and_other_minute_are_new():
    s = SentEvents()
    s.add(T, -1.25031, 35.12345)
    assert not s.seen(T, -1.25031 + 0.0034, 35.12345)
    assert not s.seen(T + timedelta(minutes=1), -1.25031, 35.12345)


def test_nothing_is_seen_before_it_is_added():
    assert not SentEvents().seen(T, 1.0, 2.0)


def test_time_is_compared_in_utc():
    s = SentEvents()
    s.add(T, 1.0, 2.0)
    assert s.seen(T.astimezone(timezone(timedelta(hours=3))), 1.0, 2.0)


def test_state_round_trip_and_prune():
    s = SentEvents()
    s.add(T, 1.0, 2.0)
    s.add(T - timedelta(days=4), 1.0, 2.0)
    s.prune(before=T - timedelta(days=3))
    assert s.to_state() == ["2026-11-23T00:42+00:00|1000|2000"]
    assert SentEvents(s.to_state()).seen(T, 1.0, 2.0)


def test_unreadable_stored_keys_are_dropped():
    assert SentEvents(["garbage", None, 5, "not-a-date|1|2", "2026-11-23T00:42+00:00|x|2"]).to_state() == []
