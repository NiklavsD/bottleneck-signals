import json

import numpy as np
import pandas as pd
import pytest

from engine import build
from pipeline.discovery import flag_panel
from backtest import run_v2, run_v3_power, run_v4


def seasonal_panel():
    return pd.DataFrame({"x": list(range(1, 13)) * 5},
                        index=pd.date_range("2021-01-01", periods=60, freq="MS"))


def test_growth_uses_calendar_months_when_a_month_is_missing():
    panel = seasonal_panel().drop(pd.Timestamp("2024-01-01"))
    growth = build.yoy3_sum(panel, ["x"])
    assert "2024-02-01" not in growth.index
    assert "2024-03-01" not in growth.index
    assert growth.loc["2024-04-01"] == pytest.approx(0)
    flags, discovery_growth, _ = flag_panel(panel, 0)
    assert pd.isna(discovery_growth.loc["2024-02-01", "x"])
    assert discovery_growth.loc["2024-04-01", "x"] == pytest.approx(0)
    assert flags.empty
    assert run_v3_power.yoy3(panel.x).loc["2024-04-01"] == pytest.approx(0)


def test_v2_taiwan_growth_uses_calendar_months(monkeypatch):
    panel = seasonal_panel().drop(pd.Timestamp("2024-01-01"))
    tw = panel.rename_axis("month").reset_index().rename(columns={"x": "revenue_k_twd"})
    tw["code"] = "2408"
    monkeypatch.setattr(run_v2, "TW", tw)
    growth = run_v2.tw_yoy(["2408"])
    assert pd.isna(growth.loc["2024-02-01"])
    assert growth.loc["2024-04-01"] == pytest.approx(0)


def test_decision_lag_depends_on_components_present_in_that_month():
    dates = pd.date_range("2005-01-01", "2018-01-01", freq="MS")
    tw = pd.Series(np.arange(len(dates), dtype=float), index=dates)
    us = pd.Series(np.arange(61, dtype=float), index=pd.date_range("2013-01-01", periods=61, freq="MS"))
    index = build.build_index({"tw": (tw, 1, {}), "us": (us, 2, {})})
    assert pd.isna(index.loc["2008-01-01", "z_us"])
    assert index.loc["2008-01-01", "decision"] == pd.Timestamp("2008-02-16")
    assert index.loc["2015-01-01", "decision"] == pd.Timestamp("2015-03-16")


def test_momentum_uses_three_calendar_months_not_three_available_rows(monkeypatch):
    monkeypatch.setattr(build, "expanding_z", lambda series: series)
    series = pd.Series([0., 1., 3., 4., 5.], index=pd.to_datetime([
        "2024-01-01", "2024-02-01", "2024-04-01", "2024-05-01", "2024-06-01"]))
    index = build.build_index({"tw": (series, 1, {})})
    assert index.loc["2024-04-01", "M"] == 3
    assert pd.isna(index.loc["2024-06-01", "M"])
    _, _, momentum = run_v4.composite({"tw": series})
    assert pd.isna(momentum.loc["2024-06-01"])


def test_tilt_handles_two_data_months_sharing_one_decision_date():
    index = pd.DataFrame({"C": [1., -1.], "decision": pd.to_datetime(["2020-03-16", "2020-03-16"])},
                         index=pd.to_datetime(["2020-01-01", "2020-02-01"]))
    days = pd.bdate_range("2020-03-16", "2023-03-16")
    basket = pd.Series(np.sin(np.arange(len(days))) * .01 + .001, index=days)
    result = build.tilt_test(index, basket, basket * 0, days[0], days[-1])
    assert result is not None
    assert result["tilt_ann_pct"] < 0  # Latest month says underweight.


@pytest.mark.parametrize("ticker,currency", [("TSM", "USD"), ("2330.TW", "TWD"),
    ("8299.TWO", "TWD"), ("000660.KS", "KRW"), ("8035.T", "JPY"), ("RR.L", "GBp"),
    ("SIE.DE", "EUR"), ("unknown.XX", None)])
def test_currency_describes_listing_not_company_country(ticker, currency):
    assert build.quote_currency(ticker)[0] == currency


def test_price_stats_has_currency_and_constituent_asof(monkeypatch):
    px = pd.DataFrame({"TSM": [100., 101., np.nan]}, index=pd.bdate_range("2026-09-28", periods=3))
    monkeypatch.setattr(build, "PX", px)
    result = build.price_stats("TSM")
    assert result["currency"] == "USD"
    assert result["price_asof"] == "2026-09-29"


@pytest.mark.parametrize("present", [True, False])
def test_missing_price_stats_keep_complete_template_shape(monkeypatch, present):
    frame = pd.DataFrame({"TSM": [np.nan]}) if present else pd.DataFrame()
    monkeypatch.setattr(build, "PX", frame)
    result = build.price_stats("TSM")
    assert result["currency"] == "USD"
    assert all(result[key] is None for key in ("last", "asof", "price_asof", "r1m", "r3m", "r1y", "ytd"))


def test_json_has_no_nonstandard_nan_or_infinity():
    value = build.json_safe({"M": float("nan"), "series": [np.float64("inf"), 1.]})
    assert json.loads(json.dumps(value, allow_nan=False)) == {"M": None, "series": [None, 1.]}


def test_memory_source_selection_falls_back_identically_for_site_and_model(monkeypatch):
    dates = pd.date_range("2013-01-01", periods=100, freq="MS")
    baseline = pd.Series(np.arange(100, dtype=float) + 100, index=dates)
    kcs = baseline * 2
    selected, source, difference = run_v4.select_korea_memory(kcs, baseline)
    assert source == "comtrade" and difference == pytest.approx(1.)
    pd.testing.assert_series_equal(selected, baseline)
    monkeypatch.setattr(build, "KR_W", kcs.to_frame("854232"))
    monkeypatch.setattr(build, "COMTRADE", baseline.to_frame("854232"))
    result = build.components({"slug": "memory", "kr_export_hs": [{"hs": "854232"}]})
    expected = build.yoy3_sum(baseline.to_frame("854232"), ["854232"])
    pd.testing.assert_series_equal(result["kr_exports"][0], expected)
    assert "fallback" in result["kr_exports"][2]["source"]


def test_chart_loads_model_inputs_explicitly(monkeypatch):
    from backtest import chart_v2
    calls = []
    monkeypatch.setattr(chart_v2.model, "load_data", lambda: calls.append("load"))
    decisions = pd.to_datetime(["2026-08-16", "2026-09-16"])
    index = pd.DataFrame({"decision": decisions, "ruleM": [True, False]})
    monkeypatch.setattr(chart_v2, "build", lambda group: index)
    monkeypatch.setattr(chart_v2, "basket_returns", lambda tickers: pd.Series(.001, index=pd.bdate_range("2026-08-17", "2026-09-30")))
    _, level, _ = chart_v2.prepare_series()
    assert calls == ["load"] and not level.empty


def test_v4_live_loading_does_not_require_v3_import_data(monkeypatch):
    calls = []
    monkeypatch.setattr(run_v2, "load_data", lambda: calls.append("v2"))
    monkeypatch.setattr(run_v3_power, "load_data", lambda: pytest.fail("Incidental v3 data loaded"))
    run_v4.load_data()
    assert calls == ["v2"]
