import threading
import time

import pytest

from app import (
    _CycleDeadlineExceeded,
    _begin_cycle,
    _cycle_context,
    _clear_cycle_context,
    _cycle_side_effect_allowed,
    _cycle_cancel_requested,
    _end_cycle,
    _prefetch_result_allowed,
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
    worker_finished = threading.Event()
    cancellation_seen = threading.Event()

    def slow_strategy():
        try:
            time.sleep(0.06)
            if _cycle_cancel_requested():
                cancellation_seen.set()
            if _cycle_side_effect_allowed("test_write", symbol="TESTUSDT"):
                side_effects.append("late-write")
        finally:
            worker_finished.set()

    try:
        with pytest.raises(_CycleDeadlineExceeded):
            _run_timed_strategy("test_timeout", slow_strategy)
        time.sleep(0.08)
        assert cancellation_seen.is_set()
        assert worker_finished.is_set()
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


def test_late_valid_prefetch_data_is_discarded_and_cannot_create_signal():
    """A valid response after the deadline is still stale for this cycle."""
    cycle_id = "test-late-valid-prefetch-cycle"
    _begin_cycle(cycle_id)
    deadline = time.time() + 0.02
    _set_cycle_context(
        cycle_id=cycle_id,
        deadline=deadline,
        cancel_event=threading.Event(),
    )
    accepted_results = {}
    valid_candles = [["open-time", "1", "2", "0.9", "1.8", "100"]]

    try:
        # The data is deliberately valid; only its completion timestamp is late.
        accepted = _prefetch_result_allowed(
            "LATEUSDT", deadline + 0.001, "test_prefetch"
        )
        if accepted:
            accepted_results["LATEUSDT"] = valid_candles

        assert accepted is False
        assert accepted_results == {}
        assert _cycle_context()["prefetch_telemetry"]["late"] == 1
    finally:
        _cleanup_cycle(cycle_id)