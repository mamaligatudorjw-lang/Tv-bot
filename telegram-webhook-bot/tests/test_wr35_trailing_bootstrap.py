from wr35_trailing_bootstrap import STEPS, build_report


def test_bootstrap_uses_unique_signal_ids_not_step_rows():
    decisions = {
        1: {"strategy": "overheated_24h", "filter_pass": "yes"},
        2: {"strategy": "overheated_24h", "filter_pass": "yes"},
        3: {"strategy": "ema_cross_confirmed", "filter_pass": "yes"},
        4: {"strategy": "ema_cross_confirmed", "filter_pass": "yes"},
    }
    simulation_rows = {}
    for signal_id, difference in ((1, 0.2), (2, 0.4), (3, 0.1), (4, 0.3)):
        for step in STEPS:
            simulation_rows[(signal_id, step)] = {
                "baseline_r": "0",
                "alt_r": str(difference),
            }

    report = build_report(decisions, simulation_rows, iterations=200, seed=7)

    assert len(simulation_rows) == 4 * len(STEPS)
    assert len(report["results"]) == 2 * len(STEPS)
    assert {
        (row["strategy"], row["n_unique_signals"])
        for row in report["results"]
    } == {
        ("overheated_24h", 2),
        ("ema_cross_confirmed", 2),
    }
    assert all(row["bootstrap_iterations"] == 200 for row in report["results"])