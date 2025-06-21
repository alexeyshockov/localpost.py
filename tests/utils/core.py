from datetime import timedelta

import pytest

# noinspection PyProtectedMember
from localpost._utils import FixedDelay, RandomDelay, Result, ensure_delay_factory, ensure_td, td_str

pytestmark = pytest.mark.anyio


def test_result_ok():
    result = Result.ok(42)
    assert result.value == 42
    assert result.error is None


def test_result_failure():
    error = ValueError("test error")
    result = Result.failure(error)
    assert result.is_failure
    assert result.error == error


def test_fixed_delay():
    delay = FixedDelay.create(5)
    assert delay() == timedelta(seconds=5)

    delay = FixedDelay.create(timedelta(seconds=10))
    assert delay() == timedelta(seconds=10)

    delay = FixedDelay.create(None)
    assert delay() == timedelta(0)

    with pytest.raises(ValueError):
        FixedDelay.create("invalid")  # noqa


def test_random_delay():
    delay = RandomDelay((1, 5))
    result = delay()
    assert isinstance(result, timedelta)
    assert timedelta(seconds=1) <= result <= timedelta(seconds=5)

    delay = RandomDelay((1.0, 5.0))
    result = delay()
    assert isinstance(result, timedelta)
    assert timedelta(seconds=1) <= result <= timedelta(seconds=5)


def test_ensure_delay_factory():
    # Test with FixedDelay
    factory = ensure_delay_factory(FixedDelay.create(5))
    assert factory() == timedelta(seconds=5)

    # Test with RandomDelay
    factory = ensure_delay_factory(RandomDelay((1, 5)))
    result = factory()
    assert isinstance(result, timedelta)
    assert timedelta(seconds=1) <= result <= timedelta(seconds=5)

    # Test with callable
    def custom_delay():
        return timedelta(seconds=3)

    factory = ensure_delay_factory(custom_delay)
    assert factory() == timedelta(seconds=3)


def test_ensure_td():
    # Test with timedelta
    td = timedelta(seconds=5)
    assert ensure_td(td) == td

    # Test with string
    assert ensure_td("5s") == timedelta(seconds=5)
    assert ensure_td("1m") == timedelta(minutes=1)
    assert ensure_td("1h") == timedelta(hours=1)

    # Test with invalid input
    with pytest.raises(ValueError):
        ensure_td("invalid")


def test_td_str():
    # Test with humanize available
    td = timedelta(seconds=5)
    assert td_str(td) == "5 seconds"

    # Test with longer duration
    td = timedelta(hours=1, minutes=30)
    assert td_str(td) == "1 hour and 30 minutes"
