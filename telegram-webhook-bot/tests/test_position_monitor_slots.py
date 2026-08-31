from types import SimpleNamespace

import app


def _monitors(long_count=0, short_count=0):
    return [
        *[SimpleNamespace(direction="LONG") for _ in range(long_count)],
        *[SimpleNamespace(direction="SHORT") for _ in range(short_count)],
    ]


def test_monitor_slot_summary_shows_counts_caps_and_remaining():
    summary = app._format_monitor_slot_summary(_monitors(long_count=4, short_count=7))

    assert "LONG: <b>4/25</b> (осталось 21)" in summary
    assert "SHORT: <b>7/25</b> (осталось 18)" in summary


def test_monitor_slot_summary_warns_near_and_at_capacity():
    summary = app._format_monitor_slot_summary(
        _monitors(long_count=22, short_count=25)
    )

    assert "LONG: <b>22/25</b> (⚠️ осталось 3)" in summary
    assert "SHORT: <b>25/25</b> (⛔ квота заполнена)" in summary


def test_positions_command_shows_empty_directional_capacity(monkeypatch):
    sent = []
    monkeypatch.setattr(app, "_active_monitors", {})
    monkeypatch.setattr(
        app,
        "_telegram_send",
        lambda _chat_id, text, **_kwargs: sent.append(text) or True,
    )

    app.handle_positions_command(123)

    assert len(sent) == 1
    assert "Нет открытых позиций" in sent[0]
    assert "LONG: <b>0/25</b> (осталось 25)" in sent[0]
    assert "SHORT: <b>0/25</b> (осталось 25)" in sent[0]


def test_status_command_includes_directional_capacity(monkeypatch):
    sent = []
    monkeypatch.setattr(
        app,
        "state",
        {
            "initialized": True,
            "known_pairs": {"BTCUSDT"},
            "last_run": "2026-08-31T15:00:00Z",
            "last_run_summary": {},
            "silenced": False,
            "silenced_at": None,
        },
    )
    monkeypatch.setattr(app, "_active_monitors", {
        1: SimpleNamespace(direction="LONG"),
        2: SimpleNamespace(direction="SHORT"),
        3: SimpleNamespace(direction="SHORT"),
    })
    monkeypatch.setattr(app, "_get_replay_status_snapshot", lambda _chat_id: (0, None))
    monkeypatch.setattr(app, "_ensure_prefs_loaded", lambda: None)
    monkeypatch.setattr(
        app,
        "_telegram_send",
        lambda _chat_id, text, **_kwargs: sent.append(text) or True,
    )

    app.handle_status_command(123)

    assert len(sent) == 1
    assert "LONG: <b>1/25</b> (осталось 24)" in sent[0]
    assert "SHORT: <b>2/25</b> (осталось 23)" in sent[0]