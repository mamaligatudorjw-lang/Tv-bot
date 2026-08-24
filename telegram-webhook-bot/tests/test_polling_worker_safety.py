import threading
import time

import pytest

from app import (
    _CycleDeadlineExceeded,
    _begin_cycle,
    _clear_cycle_context,
    _cycle_side_effect_allowed,
    _end_cycle,
    _run_timed_strategy,
    _set_cycle_context,
    _strategy_worker_acquire,
    _strategy_worker_release,
)


def _cleanup_cycle(cycle_id: str) -> None:
    _end_cycle(cycle_id)
    _clear_cycle_context()


def test_timeout_blocks_late_side_effect_and_worker_exits_cooperatively():
    cycle_id = "test-timeout-cycle"
    _begin_cycle(cycle_id)
    _set_cycle_context(
        cycle_id=cycle_id,
        deadline=time.time() + 0.02,
        cancel_event=threading.Event(),
    )
    side_effects = []

    def slow_strategy():
        time.sleep(0.06)
        if _cycle_side_effect_allowed("test_write", symbol="TESTUSDT"):
            side_effects.append("late-write")

    try:
        with pytest.raises(_CycleDeadlineExceeded):
            _run_timed_strategy("test_timeout", slow_strategy)
        time.sleep(0.08)
        assert side_effects == []
    finally:
        _cleanup_cycle(cycle_id)


def test_overlapping_strategy_worker_aborts_new_cycle_before_start():
    cycle_id = "test-overlap-cycle"
    _begin_cycle(cycle_id)
    _set_cycle_context(
        cycle_id=cycle_id,
        deadline=time.time() + 1,
        cancel_event=threading.Event(),
    )
    assert _strategy_worker_acquire("test_overlap", "old-worker") is True

    try:
        with pytest.raises(_CycleDeadlineExceeded):
            _run_timed_strategy("test_overlap", lambda: None)
    finally:
        _strategy_worker_release("test_overlap")
        _cleanup_cycle(cycle_id)