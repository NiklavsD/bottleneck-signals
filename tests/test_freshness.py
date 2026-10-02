import datetime as dt
import json

import numpy as np
import pandas as pd
import pytest

from backtest import run_v2
from pipeline import _freshness as freshness

NOW = dt.datetime(2026, 10, 17, 12, tzinfo=dt.timezone.utc)


@pytest.fixture
def physical_inputs(tmp_path):
    folder = tmp_path / "data" / "processed"
    folder.mkdir(parents=True)
    dates = pd.date_range("2023-01-01", "2026-09-01", freq="MS")
    codes = sorted({code for group in run_v2.GROUPS.values() for code in group["tw"]})
    tw = pd.DataFrame([{"month": month, "code": code, "revenue_k_twd": 100 + i}
                       for code in codes for i, month in enumerate(dates)])
    tw.to_csv(folder / "tw_revenue.csv", index=False)
    kr = pd.DataFrame([{"month": month, "hs": hs, "usd": 100 + i}
                       for hs in ["854232", "8542"] for i, month in enumerate(dates)])
    kr.to_csv(folder / "korea_customs_exports.csv", index=False)
    pd.DataFrame({"854232": np.arange(len(dates)) + 100.}, index=dates).to_csv(folder / "korea_chip_exports.csv")
    pd.DataFrame({series: np.arange(len(dates)) + 100. for series in run_v2.PPI},
                 index=dates).to_csv(folder / "fred.csv")
    us = pd.DataFrame([{"month": month, "hs": "850423", "usd": 100 + i}
                       for i, month in enumerate(dates)])
    us.to_csv(folder / "us_imports_all.csv", index=False)
    config = tmp_path / "config"
    config.mkdir()
    (config / "themes.json").write_text(json.dumps([{
        "slug": "example", "tw_signals": [{"code": "1519"}, {"code": "1503"}],
        "us_import_hs6": [{"hs": "850423"}], "kr_export_hs": [{"hs": "8542"}],
    }]))
    return tmp_path


def v4(name):
    return {"bottleneck": name, "model": "v4" if name == "memory" else "v4-tilt",
            "state": "IN" if name == "memory" else "OVERWEIGHT", "korea_source": "korea_customs"}


def v6(components):
    return {"bottleneck": "example", "model": "v6", "state": "OVERWEIGHT",
            "source_components": components}


def test_all_required_v4_sources_are_fresh(physical_inputs):
    freshness.validate_active_sources(physical_inputs, [v4(name) for name in ("memory", "optics", "power")], NOW)


def test_stale_ppi_is_rejected_even_when_other_sources_are_current(physical_inputs, monkeypatch):
    path = physical_inputs / "data/processed/fred.csv"
    frame = pd.read_csv(path, index_col=0, parse_dates=True)
    stale = run_v2.PPI[0]
    frame.loc["2026-06-01":, stale] = np.nan
    frame.to_csv(path)
    tw = pd.read_csv(physical_inputs / "data/processed/tw_revenue.csv",
                     dtype={"code": str}, parse_dates=["month"])
    monkeypatch.setattr(run_v2, "TW", tw)
    monkeypatch.setattr(run_v2, "FRED", frame)
    current = run_v2.build(run_v2.GROUPS["power"]).iloc[-1]
    assert np.isfinite(current.C) and current.decision == pd.Timestamp("2026-10-16")
    assert pd.isna(current["yoy_" + stale])
    with pytest.raises(RuntimeError, match=stale + ".*2026-09"):
        freshness.validate_active_sources(physical_inputs, [v4("power")], NOW)


@pytest.mark.parametrize("lost_codes", [["1519"], run_v2.GROUPS["power"]["tw"]])
def test_partial_taiwan_member_loss_is_rejected_with_fresh_global_month(physical_inputs, monkeypatch, lost_codes):
    path = physical_inputs / "data/processed/tw_revenue.csv"
    frame = pd.read_csv(path, dtype={"code": str})
    frame = frame[~(frame.code.isin(lost_codes) & (frame.month == "2026-09-01"))]
    assert frame.month.max() == "2026-09-01"
    frame.to_csv(path, index=False)
    frame["month"] = pd.to_datetime(frame["month"])
    monkeypatch.setattr(run_v2, "TW", frame)
    monkeypatch.setattr(run_v2, "FRED", pd.read_csv(
        physical_inputs / "data/processed/fred.csv", index_col=0, parse_dates=True))
    current = run_v2.build(run_v2.GROUPS["power"]).iloc[-1]
    assert np.isfinite(current.C) and current.decision == pd.Timestamp("2026-10-16")
    with pytest.raises(RuntimeError, match="Taiwan revenue 1519.*2026-09"):
        freshness.validate_active_sources(physical_inputs, [v4("power")], NOW)


@pytest.mark.parametrize("month", ["2026-08-01", "2025-08-01"])
def test_three_month_growth_requires_current_and_prior_year_windows(physical_inputs, month):
    path = physical_inputs / "data/processed/tw_revenue.csv"
    frame = pd.read_csv(path, dtype={"code": str})
    frame = frame[~((frame.code == "2408") & (frame.month == month))]
    frame.to_csv(path, index=False)
    with pytest.raises(RuntimeError, match="2408"):
        freshness.validate_active_sources(physical_inputs, [v4("memory")], NOW)


def test_memory_checks_the_selected_source_not_fresh_unused_korea_customs(physical_inputs):
    path = physical_inputs / "data/processed/korea_chip_exports.csv"
    frame = pd.read_csv(path, index_col=0, parse_dates=True)
    frame.loc["2026-09-01", "854232"] = np.nan
    frame.to_csv(path)
    call = {**v4("memory"), "korea_source": "comtrade"}
    with pytest.raises(RuntimeError, match="comtrade.*2026-09"):
        freshness.validate_active_sources(physical_inputs, [call], NOW)


def test_v6_checks_only_used_components_with_correct_lags(physical_inputs):
    folder = physical_inputs / "data/processed"
    # September US data is not due at the October decision. August is.
    frame = pd.read_csv(folder / "us_imports_all.csv", dtype={"hs": str})
    frame = frame[frame.month <= "2026-08-01"]
    frame.to_csv(folder / "us_imports_all.csv", index=False)
    freshness.validate_active_sources(physical_inputs, [v6(["tw_revenue", "us_imports", "kr_exports"])], NOW)
    frame = frame[frame.month < "2026-08-01"]
    frame.to_csv(folder / "us_imports_all.csv", index=False)
    with pytest.raises(RuntimeError, match="US imports 850423.*2026-08"):
        freshness.validate_active_sources(physical_inputs, [v6(["us_imports"])], NOW)
    # An unused trade component must not make a valid TW-only call abort.
    freshness.validate_active_sources(physical_inputs, [v6(["tw_revenue"])], NOW)


def test_v6_new_member_is_not_forced_into_present_in_both_aggregation(physical_inputs):
    path = physical_inputs / "data/processed/tw_revenue.csv"
    frame = pd.read_csv(path, dtype={"code": str})
    new = frame[(frame.code == "1503") & (frame.month >= "2026-01-01")].copy()
    frame = pd.concat([frame[frame.code != "1503"], new])
    frame.to_csv(path, index=False)
    freshness.validate_active_sources(physical_inputs, [v6(["tw_revenue"])], NOW)
    # The v4 fixed incumbent basket does not have this eligibility exemption.
    with pytest.raises(RuntimeError, match="1503"):
        freshness.validate_active_sources(physical_inputs, [v4("power")], NOW)


def test_missing_member_or_invalid_ppi_value_aborts(physical_inputs):
    path = physical_inputs / "data/processed/fred.csv"
    frame = pd.read_csv(path, index_col=0, parse_dates=True)
    frame.loc["2026-09-01", run_v2.PPI[0]] = float("inf")
    frame.to_csv(path)
    with pytest.raises(RuntimeError, match="FRED PPI"):
        freshness.validate_active_sources(physical_inputs, [v4("power")], NOW)


def test_monitoring_calls_do_not_require_physical_sources(tmp_path):
    freshness.validate_active_sources(tmp_path, [{"state": "NO CALL"}], NOW)


def test_decision_boundary_and_timezone_are_explicit():
    before = dt.datetime(2026, 10, 15, 23, tzinfo=dt.timezone.utc)
    after = dt.datetime(2026, 10, 16, tzinfo=dt.timezone.utc)
    assert freshness.due_month(before, 1) == pd.Timestamp("2026-08-01")
    assert freshness.due_month(after, 1) == pd.Timestamp("2026-09-01")
    offset = dt.timezone(dt.timedelta(hours=2))
    assert freshness.due_month(before.astimezone(offset), 1) == pd.Timestamp("2026-08-01")
    with pytest.raises(ValueError, match="aware"):
        freshness.due_month(dt.datetime(2026, 10, 16), 1)


def test_missing_v6_source_metadata_cannot_silently_skip_checks(physical_inputs):
    with pytest.raises(RuntimeError, match="metadata"):
        freshness.validate_active_sources(physical_inputs, [v6([])], NOW)
