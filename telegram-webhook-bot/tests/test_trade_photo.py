"""
handle_trade_photo must:
  1. Download the largest photo via Telegram getFile.
  2. Call Gemini Vision and parse its JSON.
  3. Synthesize a /trade text and delegate to handle_trade_command.
  4. Send a friendly Russian error on any failure path.

These tests stub the vision call and downloader so no network is required.
"""
import app


def test_photo_parses_and_routes_to_trade(monkeypatch):
    captured = {}

    monkeypatch.setattr(app, "_telegram_download_photo",
                        lambda _fid: (b"\xff\xd8\xff fake jpeg", "image/jpeg"))
    monkeypatch.setattr(app, "_gemini_extract_trade_from_image",
                        lambda _b, mime_type="image/jpeg": {
                            "symbol": "MIRAUSDT",
                            "direction": "шорт",
                            "entry": 0.0799486,
                            "exit": 0.08002,
                        })
    # Spy the downstream call.
    def fake_trade(chat_id, raw_text):
        captured["chat_id"] = chat_id
        captured["raw_text"] = raw_text
    monkeypatch.setattr(app, "handle_trade_command", fake_trade)
    # Silence outbound Telegram calls.
    monkeypatch.setattr(app, "_telegram_send", lambda *a, **kw: True)

    msg = {"photo": [{"file_id": "small"}, {"file_id": "BIG"}]}
    app.handle_trade_photo(12345, msg)

    assert captured["chat_id"] == 12345
    # Should look like: "/trade MIRAUSDT шорт 0.0799486 0.08002"
    parts = captured["raw_text"].split()
    assert parts[0] == "/trade"
    assert parts[1] == "MIRAUSDT"
    assert parts[2] == "шорт"
    assert float(parts[3]) == 0.0799486
    assert float(parts[4]) == 0.08002


def test_photo_vision_error_message_is_surfaced(monkeypatch):
    sent = []
    monkeypatch.setattr(app, "_telegram_download_photo",
                        lambda _fid: (b"png", "image/png"))
    monkeypatch.setattr(app, "_gemini_extract_trade_from_image",
                        lambda _b, mime_type="image/jpeg":
                            {"error": "это не карточка сделки"})
    monkeypatch.setattr(app, "handle_trade_command",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("should not be called")))
    monkeypatch.setattr(app, "_telegram_send",
                        lambda cid, text, **kw: sent.append((cid, text)) or True)

    app.handle_trade_photo(42, {"photo": [{"file_id": "x"}]})

    # First message is the "🔎 Распознаю…" notice; second is the error.
    assert len(sent) >= 2
    assert "не карточка сделки" in sent[-1][1]


def test_photo_download_failure_is_handled(monkeypatch):
    sent = []
    monkeypatch.setattr(app, "_telegram_download_photo", lambda _fid: None)
    monkeypatch.setattr(app, "handle_trade_command",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("should not be called")))
    monkeypatch.setattr(app, "_telegram_send",
                        lambda cid, text, **kw: sent.append((cid, text)) or True)

    app.handle_trade_photo(42, {"photo": [{"file_id": "x"}]})

    assert any("Не удалось скачать" in s[1] for s in sent)


def test_photo_malformed_json_is_handled(monkeypatch):
    sent = []
    monkeypatch.setattr(app, "_telegram_download_photo",
                        lambda _fid: (b"jpg", "image/jpeg"))
    # Missing required keys → handler must report a graceful error.
    monkeypatch.setattr(app, "_gemini_extract_trade_from_image",
                        lambda _b, mime_type="image/jpeg":
                            {"symbol": "BTCUSDT"})
    monkeypatch.setattr(app, "handle_trade_command",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("should not be called")))
    monkeypatch.setattr(app, "_telegram_send",
                        lambda cid, text, **kw: sent.append((cid, text)) or True)

    app.handle_trade_photo(42, {"photo": [{"file_id": "x"}]})

    assert any("некорректные данные" in s[1] for s in sent)
