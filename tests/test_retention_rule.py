"""The period rule copied from docint: how a change of the period moves deadlines, and the sweep loop."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from ghostwriter.retention_rule import (
    RetentionPeriodState,
    deadline_under,
    is_due,
    next_period_state,
    run_retention_loop,
)

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)


def _settled(days: int) -> RetentionPeriodState:
    """A period in force for long, its notice long over."""
    return RetentionPeriodState(days, NOW - 400 * DAY, NOW - 370 * DAY)


def test_first_period_waits_the_notice() -> None:
    """A period seen for the first time deletes nothing for the notice."""
    state = next_period_state(None, 365, 30, NOW)
    assert (state.set_at, state.grace_until, state.in_force_days) == (NOW, NOW + 30 * DAY, None)
    assert deadline_under(NOW - 400 * DAY, state, 30) == NOW + 30 * DAY


def test_shortening_spares_only_what_falls_due_sooner() -> None:
    """What the period in force made due keeps its date; what the shorter one makes due waits the notice."""
    state = next_period_state(_settled(365), 30, 30, NOW)
    assert deadline_under(NOW - 400 * DAY, state, 30) == NOW - 35 * DAY
    assert deadline_under(NOW - 200 * DAY, state, 30) == NOW + 30 * DAY
    assert deadline_under(NOW - 10 * DAY, state, 30) == NOW + 30 * DAY


def test_lengthening_takes_effect_at_once() -> None:
    """A longer period never pauses deletion: what is older than it is due at once."""
    state = next_period_state(_settled(30), 365, 30, NOW)
    assert deadline_under(NOW - 400 * DAY, state, 30) == NOW
    assert deadline_under(NOW - 100 * DAY, state, 30) == NOW + 265 * DAY


def test_changing_back_and_forth_postpones_nothing() -> None:
    """365 → 364 → 365 within the notice: the 365 days still decide on time."""
    first = next_period_state(_settled(365), 364, 30, NOW)
    back = next_period_state(first, 365, 30, NOW + DAY)
    assert deadline_under(NOW - 400 * DAY, back, 30) == NOW + DAY


def test_raising_then_lowering_gives_the_notice() -> None:
    """30 → 365 → 30: the 365 days were in force, so a chat they keep gets the full notice."""
    raised = next_period_state(_settled(30), 365, 30, NOW)
    lowered = next_period_state(raised, 30, 30, NOW + 10 * DAY)
    assert deadline_under(NOW - 100 * DAY, lowered, 30) == NOW + 40 * DAY


def test_a_change_during_the_first_notice_keeps_its_end() -> None:
    """Changing the period before the first notice ended never moves that end later."""
    first = next_period_state(None, 365, 30, NOW)
    changed = next_period_state(first, 30, 30, NOW + 20 * DAY)
    assert deadline_under(NOW - 400 * DAY, changed, 30) == NOW + 30 * DAY


def test_a_lower_notice_never_ends_a_running_notice_sooner() -> None:
    """Changing the period and lowering the notice together keeps the notice already running."""
    first = next_period_state(None, 365, 30, NOW)
    changed = next_period_state(first, 30, 7, NOW + DAY)
    assert changed.grace_until == NOW + 30 * DAY


def test_off_then_on_gives_a_fresh_notice() -> None:
    """Switching off and on again starts a notice of its own."""
    off = next_period_state(_settled(365), None, 30, NOW)
    assert off.days is None
    assert deadline_under(NOW - 400 * DAY, off, 30) is None
    on = next_period_state(off, 365, 30, NOW + DAY)
    assert (on.grace_until, on.in_force_days) == (NOW + 31 * DAY, None)


def test_a_clock_stepped_back_keeps_what_is_due() -> None:
    """A state dated a few seconds ahead after a change is capped at now; the notice never restarts."""
    lengthened = next_period_state(_settled(30), 365, 30, NOW)
    stepped = next_period_state(lengthened, 365, 30, NOW - timedelta(seconds=5))
    assert stepped.grace_until == NOW
    assert deadline_under(NOW - 400 * DAY, stepped, 30) == NOW


def test_a_deadline_is_due_from_its_moment_on() -> None:
    """Due at the deadline, not before; no deadline is never due."""
    assert is_due(NOW, NOW) and not is_due(NOW + DAY, NOW) and not is_due(None, NOW)


async def test_the_loop_waits_first_then_survives_a_failed_sweep() -> None:
    """The first sweep runs after the first delay, then once per interval; one that raises ends nothing."""
    waits: list[float] = []
    runs: list[int] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) > 3:
            raise asyncio.CancelledError

    async def sweep() -> None:
        runs.append(len(waits))
        if len(runs) == 1:
            raise RuntimeError("synthetic sweep failure")

    with pytest.raises(asyncio.CancelledError):
        await run_retention_loop(sweep, first_delay=60, interval=86_400, sleep=sleep)
    assert waits == [60, 86_400, 86_400, 86_400]
    assert runs == [1, 2, 3]


async def test_cancelling_during_a_sweep_stops_the_loop() -> None:
    """Shutdown cancels mid-sweep; the failure handler must not swallow that."""
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) > 3:
            raise AssertionError("the loop swallowed the cancellation")

    async def sweep() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_retention_loop(sweep, first_delay=0, interval=0, sleep=sleep)
