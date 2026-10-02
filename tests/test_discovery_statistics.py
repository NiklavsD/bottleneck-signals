import numpy as np
import pandas as pd
import pytest

from backtest.run_v5_discovery import clustered_mean, stats, summarize


def test_clustered_statistic_tests_the_reported_event_weighted_mean():
    values = [.1] * 20 + [.4] * 3 + [-.05] + [.05] * 10
    months = ["2008-01-11"] * 20 + ["2008-02-11"] * 3 + ["2008-03-11"] + ["2008-04-11"] * 10
    events = pd.DataFrame({"event_date": pd.to_datetime(months), "ex6": values})
    mean = np.mean(values)
    sums = [20 * (.1 - mean), 3 * (.4 - mean), -.05 - mean, 10 * (.05 - mean)]
    se = np.sqrt(4 / 3 * np.sum(np.array(sums) ** 2) / len(values) ** 2)
    actual_mean, actual_se, t, groups = clustered_mean(events, "ex6")
    assert actual_mean == pytest.approx(mean)
    assert actual_se == pytest.approx(se)
    assert t == pytest.approx(mean / se) and groups == 4
    result = stats(events, "ex6")
    assert result["mean_pct"] == round(mean * 100, 2)
    assert result["t_clustered"] == round(mean / se, 2)


def test_single_month_does_not_claim_statistical_validation():
    events = pd.DataFrame({"event_date": pd.to_datetime(["2008-01-11"] * 3),
                           "ex6": [.1, .2, .3], "ex12": [.1, .2, .3]})
    assert stats(events, "ex6")["t_clustered"] is None
    assert summarize(events)["PASS"] is False
