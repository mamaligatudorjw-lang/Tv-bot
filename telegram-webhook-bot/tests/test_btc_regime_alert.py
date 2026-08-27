import app


def _snapshot(**overrides):
    result = {
        "available": True,
        "regime": "bull",
        "candle_ts": 1_760_000_000,
        "close": 100.0,
        "ema50": 95.0,
        "fetched_ts": 1_760_000_600,
        "stale": False,
        "error": "",
    }
    result.update(overrides)
    return result


def _report(**overrides):
    result = {
        "status": "ready",
        "analysis_ts": 1_760_000_000,
        "summary_index": {
            ("bb_squeeze", "SHORT", "bull"): {
                "n": 268,
                "resolved_wr_pct": 15.3,
                "avg_r": -1.0091,
                "sample_status": "ready",
            }
        },
    }
    result.update(overrides)
    return result


def test_current_btc_label_includes_completed_candle_and_snapshot_time():
    label = app._format_btc_4h_regime_label(_snapshot())

    assert "BTC 4h: <b>BULL</b>" in label
    assert "close 100 vs EMA50 95" in label
    assert "свеча закрыта" in label
    assert "срез" in label
    assert "ДАННЫЕ УСТАРЕЛИ" not in label


def test_stale_current_btc_label_is_explicit():
    label = app._format_btc_4h_regime_label(_snapshot(stale=True))

    assert "ДАННЫЕ УСТАРЕЛИ" in label


def test_unavailable_current_btc_label_is_explicit():
    label = app._format_btc_4h_regime_label(
        _snapshot(available=False, regime="unknown", close=None, ema50=None)
    )

    assert "BTC 4h: <b>Н/Д</b>" in label
    assert "завершённая свеча недоступны" in label


def test_stats_label_shows_n_wr_avg_r_and_report_snapshot():
    label = app._format_regime_stats_label(
        "bb_squeeze", "SHORT", _snapshot(), _report()
    )

    assert "n=268" in label
    assert "WR=15.30%" in label
    assert "avg R=-1.0091" in label
    assert "срез" in label
    assert "INSUFFICIENT" not in label


def test_small_stats_label_is_marked_insufficient():
    report = _report(
        summary_index={
            ("confluence", "SHORT", "bull"): {
                "n": 10,
                "resolved_wr_pct": 10.0,
                "avg_r": -0.8,
                "sample_status": "insufficient_sample",
            }
        }
    )
    label = app._format_regime_stats_label(
        "confluence", "SHORT", _snapshot(), report
    )

    assert "n=10" in label
    assert "WR=10.00%" in label
    assert "avg R=-0.8000" in label
    assert "INSUFFICIENT (<20; n=10)" in label


def test_missing_stats_and_stale_report_are_not_fabricated():
    missing = app._format_regime_stats_label(
        "new_strategy", "LONG", _snapshot(), _report()
    )
    stale = app._format_regime_stats_label(
        "bb_squeeze", "SHORT", _snapshot(), _report(status="stale")
    )

    assert "WR: <b>Н/Д</b>" in missing
    assert "нет resolved-статистики" in missing
    assert "WR: <b>Н/Д</b>" in stale
    assert "отчёт устарел" in stale


def test_enrichment_helper_combines_live_and_historical_context(monkeypatch):
    monkeypatch.setattr(app, "_get_btc_4h_regime_snapshot", lambda: _snapshot())
    monkeypatch.setattr(app, "_load_regime_stats_report", lambda: _report())

    label = app._get_btc_regime_enrichment_label("bb_squeeze", "SHORT")

    assert label.count("\n") == 1
    assert "BTC 4h: <b>BULL</b>" in label
    assert "n=268" in label