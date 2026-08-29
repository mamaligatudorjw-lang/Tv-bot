import pytest

from confirmation_level_bootstrap import build_report
from partial_tp50_followup import build_report as build_partial_report


def _level_row(signal_id, level, status, result_r):
    return {
        "id": str(signal_id),
        "strategy": "overheated_confirmed",
        "confirmation_level": level,
        "status": status,
        "result_r": str(result_r),
    }


def test_confirmation_bootstrap_reports_both_level_cis_and_insufficient_sample():
    report = build_report(
        {
            "2/3": [
                _level_row(1, "2/3", "tp", 1.5),
                _level_row(2, "2/3", "sl", -1.0),
                _level_row(3, "2/3", "sl", -1.0),
                _level_row(4, "2/3", "sl", -1.0),
            ],
            "3/3": [
                _level_row(5, "3/3", "tp", 1.0),
                _level_row(6, "3/3", "sl", -1.0),
            ],
        },
        iterations=200,
        seed=7,
    )

    assert len(report["results"]) == 2
    level_2 = report["results"][0]
    level_3 = report["results"][1]
    assert level_2["avg_r"] == pytest.approx(-0.375)
    assert level_2["delta_wr_minus_breakeven_pp"] == pytest.approx(-15.0)
    assert level_2["avg_r_ci_crosses_zero"] is True
    assert level_2["delta_ci_crosses_zero"] is True
    assert level_3["sample_status"] == "insufficient"
    assert level_3["n_unique_signals"] == 2


def test_partial_followup_counts_floor_and_trailing_and_estimates_second_leg():
    source_report = {
        "summary": [
            {
                "sample": "baseline_fixed",
                "n": 2,
                "n_tp_branch": 2,
                "total_r": 2.0,
                "avg_r": 1.0,
                "wr_pct": 100.0,
            },
            {
                "sample": "partial_tp50",
                "step_pct": 2.0,
                "n": 2,
                "n_tp_branch": 2,
                "total_r": 2.5,
                "avg_r": 1.25,
                "wr_pct": 100.0,
                "floor_exit_n": 1,
                "trail_exit_n": 1,
            },
        ]
    }
    audit = [
        {
            "id": "1",
            "step_pct": "2.0",
            "partial_branch": "tp_branch",
            "outcome": "partial_tp_floor",
            "trail_price": "120",
        },
        {
            "id": "2",
            "step_pct": "2.0",
            "partial_branch": "tp_branch",
            "outcome": "partial_trail_stop",
            "trail_price": "110",
        },
    ]
    positions = {
        1: {"entry_price": 100, "sl_price": 90},
        2: {"entry_price": 100, "sl_price": 90},
    }

    report = build_partial_report(
        source_report,
        audit,
        positions,
        cost_rates_bps=(5.0,),
    )
    row = report["rows"][1]

    assert row["floor_exit_n"] == 1
    assert row["trail_exit_n"] == 1
    assert row["branch_rows"] == row["tp_branch_n"] == 2
    assert row["proportional_split_fee_delta_r"] == 0.0
    assert row["extra_second_leg_cost_5bps_total_r"] == pytest.approx(0.00575)
    assert row["extra_second_leg_cost_5bps_avg_r"] == pytest.approx(0.002875)
    assert row["extra_second_leg_cost_5bps_adjusted_avg_r"] == pytest.approx(
        1.247125
    )
    assert report["branch_total"] == {
        "tp_branch_step_rows": 2,
        "floor_step_rows": 1,
        "trail_step_rows": 1,
    }