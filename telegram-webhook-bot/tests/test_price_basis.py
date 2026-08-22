import pytest

from app import _compute_pump_fade_sl_tp, _validate_demo_price_basis


def test_price_basis_accepts_levels_around_entry():
    _validate_demo_price_basis("SHORT", 100.0, 112.0, 76.0, "pump_24h_fade")
    _validate_demo_price_basis("LONG", 100.0, 88.0, 124.0, "ema_cross_confirmed")


def test_price_basis_rejects_levels_not_ordered_around_entry():
    with pytest.raises(ValueError, match="barrier ordering"):
        _validate_demo_price_basis("SHORT", 100.0, 112.0, 101.0, "pump_24h_fade")


def test_pump_fade_targets_are_derived_from_explicit_entry():
    sl, tp = _compute_pump_fade_sl_tp(100.0, None)
    assert sl == pytest.approx(112.0)
    assert tp == pytest.approx(76.0)