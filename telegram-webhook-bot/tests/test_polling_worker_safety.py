from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
import threading
import time

import pytest
import requests

import app
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
    check_low_rejection_long,
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


def test_prefetch_future_finishing_after_budget_is_unreachable():
    """A future that outlives collection is never consumed by this cycle."""
    executor = ThreadPoolExecutor(max_workers=1)
    collected = []
    started = threading.Event()
    finished = threading.Event()

    def slow_prefetch():
        started.set()
        time.sleep(0.05)
        finished.set()
        return {"value": [["valid"]], "finished_ts": time.time(), "error": None}

    future = executor.submit(slow_prefetch)
    jobs = {future: "LATEUSDT"}
    try:
        assert started.wait(0.2)
        with pytest.raises(FuturesTimeoutError):
            for completed in as_completed(jobs, timeout=0.005):
                collected.append(completed.result())

        future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        assert finished.wait(0.2)
        assert future.done()
        assert collected == []
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def test_missing_prefetch_data_cannot_create_signal(monkeypatch):
    """An empty prefetch response follows the no-data path, not a signal path."""
    monkeypatch.setattr("app._gateio_klines", lambda *args, **kwargs: [])
    tickers = {
        "NODATAUSDT": {
            "quoteVolume": 1_000_000,
            "highPrice": 120,
            "lowPrice": 100,
            "lastPrice": 104,
        }
    }

    assert check_low_rejection_long(tickers) == 0


class _FakeGateResponse:
    def __init__(self, chunks=None):
        self._content = b""
        self._content_consumed = False
        self._chunks = list(chunks or [b"[]"])
        self.closed = False

    def iter_content(self, chunk_size=64 * 1024):
        yield from self._chunks

    def raise_for_status(self):
        return None

    def close(self):
        self.closed = True


def _assert_gateio_permit_available(sem):
    assert sem.acquire(timeout=0.1) is True
    sem.release()


def test_gateio_releases_permit_after_success(monkeypatch):
    sem = threading.Semaphore(1)
    response = _FakeGateResponse()
    monkeypatch.setattr(app, "_gateio_sem", sem)
    monkeypatch.setattr(app.requests, "get", lambda *args, **kwargs: response)

    assert app._gateio_get("/test", timeout=0.1) is response
    assert response.closed is True
    _assert_gateio_permit_available(sem)


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("transport failure"),
        requests.Timeout("socket inactivity"),
    ],
)
def test_gateio_releases_permit_after_request_exception(monkeypatch, failure):
    sem = threading.Semaphore(1)
    monkeypatch.setattr(app, "_gateio_sem", sem)

    def fail_request(*args, **kwargs):
        raise failure

    monkeypatch.setattr(app.requests, "get", fail_request)
    with pytest.raises(type(failure)):
        app._gateio_get("/test", timeout=0.1)

    _assert_gateio_permit_available(sem)


def test_gateio_semaphore_wait_is_bounded_and_requests_timeout_compatible(monkeypatch):
    sem = threading.Semaphore(0)
    calls = []
    monkeypatch.setattr(app, "_gateio_sem", sem)
    monkeypatch.setattr(app, "GATEIO_SEMAPHORE_WAIT_TIMEOUT", 0.02)
    monkeypatch.setattr(
        app.requests,
        "get",
        lambda *args, **kwargs: calls.append(args) or pytest.fail(
            "HTTP request must not start without a Gate.io permit"
        ),
    )

    started = time.monotonic()
    with pytest.raises(app._GateioSemaphoreTimeout) as exc_info:
        app._gateio_get("/starved", timeout=0.1)
    elapsed = time.monotonic() - started

    assert isinstance(exc_info.value, requests.Timeout)
    assert exc_info.value.gateio_reason == "semaphore_wait_timeout"
    assert elapsed < 0.2
    assert calls == []


def test_gateio_cancellable_semaphore_wait_is_bounded(monkeypatch):
    sem = threading.Semaphore(0)
    cancel_event = threading.Event()
    cancel_event.set()
    monkeypatch.setattr(app, "_gateio_sem", sem)
    monkeypatch.setattr(app, "GATEIO_SEMAPHORE_WAIT_TIMEOUT", 0.2)

    with pytest.raises(app._GateioRequestCancelled) as exc_info:
        app._gateio_get("/cancelled", cancel_event=cancel_event, timeout=0.1)

    assert isinstance(exc_info.value, requests.Timeout)
    assert exc_info.value.gateio_reason == "cancelled"


def test_gateio_cancellation_before_semaphore_never_starts_http(monkeypatch):
    sem = threading.Semaphore(0)
    cancel_event = threading.Event()
    cancel_event.set()
    calls = []
    monkeypatch.setattr(app, "_gateio_sem", sem)
    monkeypatch.setattr(app, "GATEIO_SEMAPHORE_WAIT_TIMEOUT", 0.02)

    def unexpected_http(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("HTTP must not start after cancellation")

    monkeypatch.setattr(app.requests, "get", unexpected_http)
    with pytest.raises(app._GateioRequestCancelled):
        app._gateio_get(
            "/cancelled-before-semaphore",
            cancel_event=cancel_event,
            timeout=0.1,
        )

    assert calls == []


def test_gateio_slow_drip_response_hits_absolute_deadline_and_releases_permit(
    monkeypatch,
):
    sem = threading.Semaphore(1)
    response = _FakeGateResponse()
    monkeypatch.setattr(app, "_gateio_sem", sem)

    def slow_request(*args, **kwargs):
        response._chunks = []

        def slow_chunks(chunk_size=64 * 1024):
            time.sleep(0.03)
            yield b"[]"

        response.iter_content = slow_chunks
        return response

    monkeypatch.setattr(app.requests, "get", slow_request)

    with pytest.raises(app._GateioRequestDeadline) as exc_info:
        app._gateio_get("/slow-drip", timeout=0.01)

    assert isinstance(exc_info.value, requests.Timeout)
    assert exc_info.value.gateio_reason == "request_deadline"
    assert response.closed is True
    _assert_gateio_permit_available(sem)


def test_bollinger_gateio_timeout_has_no_signal_or_side_effects(monkeypatch):
    """The shared semaphore timeout must be a no-signal path for Bollinger."""
    sem = threading.Semaphore(0)
    symbol = "BBGATEIOTIMEOUTUSDT"
    side_effects = []
    telegram = []
    before_cooldown = app.state["last_bb_squeeze_alerted"].get(symbol)
    monkeypatch.setattr(app, "_gateio_sem", sem)
    monkeypatch.setattr(app, "GATEIO_SEMAPHORE_WAIT_TIMEOUT", 0.01)
    monkeypatch.setattr(
        app,
        "_demo_open_position",
        lambda *args, **kwargs: side_effects.append(("demo", args, kwargs)),
    )
    monkeypatch.setattr(
        app,
        "send_telegram",
        lambda *args, **kwargs: telegram.append(("send", args, kwargs)),
    )
    monkeypatch.setattr(
        app,
        "_telegram_send",
        lambda *args, **kwargs: telegram.append(("raw", args, kwargs)),
    )

    sent = app.check_bollinger_squeeze(
        {
            symbol: {
                "quoteVolume": app.MIN_VOLUME_USDT + 1,
                "priceChangePercent": 0,
                "lastPrice": 100,
            }
        },
        {symbol: 50},
    )

    assert sent == 0
    assert side_effects == []
    assert telegram == []
    assert app.state["last_bb_squeeze_alerted"].get(symbol) == before_cooldown


def test_whitelisted_confirmation_timeout_has_no_live_side_effects(monkeypatch):
    """The #186 confirmation path must stay inert on the shared timeout path."""
    sem = threading.Semaphore(0)
    symbol = "EMACONFIRMATIONTIMEOUTUSDT"
    key = (symbol, "ema_cross")
    pending = {
        "ts": time.time(),
        "signal_price": 100.0,
        "direction": "LONG",
        "atr": 1.0,
        "confirm_num": 0,
        "last_confirm_price": None,
    }
    side_effects = []
    telegram = []
    monkeypatch.setattr(app, "_gateio_sem", sem)
    monkeypatch.setattr(app, "GATEIO_SEMAPHORE_WAIT_TIMEOUT", 0.01)
    monkeypatch.setattr(
        app,
        "_demo_open_position",
        lambda *args, **kwargs: side_effects.append(("demo", args, kwargs)),
    )
    monkeypatch.setattr(
        app,
        "send_telegram",
        lambda *args, **kwargs: telegram.append(("send", args, kwargs)),
    )
    monkeypatch.setattr(
        app,
        "_telegram_send",
        lambda *args, **kwargs: telegram.append(("raw", args, kwargs)),
    )

    with app._cont_lock:
        previous = app._cont_pending.get(key)
        app._cont_pending[key] = pending
    try:
        assert app.check_cont_confirmed() == 0
        with app._cont_lock:
            assert app._cont_pending[key] == pending
        assert side_effects == []
        assert telegram == []
    finally:
        with app._cont_lock:
            if previous is None:
                app._cont_pending.pop(key, None)
            else:
                app._cont_pending[key] = previous


def _set_timed_cycle(cycle_id, deadline):
    _begin_cycle(cycle_id)
    _set_cycle_context(
        cycle_id=cycle_id,
        deadline=deadline,
        cancel_event=threading.Event(),
    )


def test_range_worker_logs_cancellation_confirmed_and_blocks_late_side_effect(
    monkeypatch, caplog
):
    cycle_id = "test-range-cancellation-confirmed"
    _set_timed_cycle(cycle_id, time.time() + 0.01)
    side_effects = []

    def slow_range_worker():
        time.sleep(0.04)
        if app._cycle_side_effect_allowed("range_late_write", symbol="RANGEUSDT"):
            side_effects.append("late-write")
        return 1

    monkeypatch.setattr(app, "CONFIRM_GRACE", 0.1)
    try:
        with pytest.raises(_CycleDeadlineExceeded):
            _run_timed_strategy("range_breakout_long", slow_range_worker)
        assert any(
            "polling_worker_cancellation_confirmed" in record.getMessage()
            for record in caplog.records
        )
        assert side_effects == []
    finally:
        _cleanup_cycle(cycle_id)


def test_range_worker_logs_cancellation_failed_when_grace_expires(
    monkeypatch, caplog
):
    cycle_id = "test-range-cancellation-failed"
    _set_timed_cycle(cycle_id, time.time() + 0.01)
    worker_finished = threading.Event()

    def uncooperative_range_worker():
        try:
            time.sleep(0.08)
        finally:
            worker_finished.set()

    monkeypatch.setattr(app, "CONFIRM_GRACE", 0.01)
    try:
        with pytest.raises(_CycleDeadlineExceeded):
            _run_timed_strategy("range_breakout_long", uncooperative_range_worker)
        assert any(
            "polling_worker_cancellation_failed" in record.getMessage()
            for record in caplog.records
        )
        assert worker_finished.wait(0.2)
    finally:
        _cleanup_cycle(cycle_id)


def test_range_breakout_prefetch_respects_max_inflight(monkeypatch):
    active = 0
    peak = 0
    lock = threading.Lock()

    def fake_klines(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.02)
            return []
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(app, "_gateio_klines", fake_klines)
    tickers = {
        f"RANGE{i}USDT": {
            "lastPrice": 100,
            "quoteVolume": app.MIN_VOLUME_USDT + 1,
        }
        for i in range(app.RANGE_BREAKOUT_MAX_INFLIGHT * 2)
    }

    assert app.check_range_breakout_long(tickers) == 0
    assert peak == app.RANGE_BREAKOUT_MAX_INFLIGHT


def test_range_slow_drip_logs_cancellation_confirmed(monkeypatch, caplog):
    cycle_id = "test-range-slow-drip-confirmed"
    _set_timed_cycle(cycle_id, time.time() + 1.0)
    response = _FakeGateResponse()
    request_started = threading.Event()
    stream_started = threading.Event()
    request_finished = threading.Event()

    def slow_request(*args, **kwargs):
        request_started.set()

        def slow_chunks(chunk_size=64 * 1024):
            stream_started.set()
            time.sleep(2.0)
            request_finished.set()
            yield b"[]"

        response.iter_content = slow_chunks
        return response

    monkeypatch.setattr(app.requests, "get", slow_request)
    tickers = {
        "RANGESLOWDRIPUSDT": {
            "lastPrice": 100,
            "quoteVolume": app.MIN_VOLUME_USDT + 1,
        }
    }
    try:
        try:
            _run_timed_strategy(
                "range_breakout_long",
                app.check_range_breakout_long,
                tickers,
            )
        except _CycleDeadlineExceeded:
            pass
        assert any(
            "polling_worker_cancellation_confirmed" in record.getMessage()
            for record in caplog.records
        )
        assert request_started.wait(0.2)
        assert stream_started.wait(0.2)
        assert request_finished.wait(2.5)
    finally:
        _cleanup_cycle(cycle_id)


def test_whitelisted_strategy_order_stays_before_downstream_range():
    order = app._POLLING_OPTIONAL_ORDER
    assert order.index("overheated_oversold") < order.index("ema_crossover")
    assert order.index("ema_crossover") < order.index("low_rejection_long")
    assert order.index("low_rejection_long") < order.index("range_breakout_long")


def test_gateio_starvation_with_all_global_permits_held_is_bounded(monkeypatch):
    sem = threading.Semaphore(app.GATEIO_MAX_CONCURRENT)
    for _ in range(app.GATEIO_MAX_CONCURRENT):
        assert sem.acquire(blocking=False)
    calls = []
    monkeypatch.setattr(app, "_gateio_sem", sem)
    monkeypatch.setattr(app, "GATEIO_SEMAPHORE_WAIT_TIMEOUT", 0.02)
    monkeypatch.setattr(
        app.requests,
        "get",
        lambda *args, **kwargs: calls.append(args) or pytest.fail(
            "HTTP request must not start while all permits are held"
        ),
    )

    try:
        started = time.monotonic()
        with pytest.raises(app._GateioSemaphoreTimeout):
            app._gateio_get("/all-permits-held", timeout=0.1)
        assert time.monotonic() - started < 0.2
        assert calls == []
    finally:
        for _ in range(app.GATEIO_MAX_CONCURRENT):
            sem.release()