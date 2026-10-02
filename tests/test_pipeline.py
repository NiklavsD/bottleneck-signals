"""Pipeline robustness tests (no network): monthly gates, price carry-over, Census cache fallback, link filter.

    .venv/bin/python -m pytest -q tests
"""
import json
import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "pipeline"), os.path.join(ROOT, "engine")]

import monthly  # noqa: E402  (chdirs to ROOT)
import fetch_census_all  # noqa: E402
import fetch_universe_prices  # noqa: E402
import context  # noqa: E402


# ---------------------------------------------------------------- monthly helpers

def v4_row(month, C=1.0, M=0.5, **cols):
    month = pd.Timestamp(month)
    decision = month + pd.offsets.MonthBegin(1) + pd.Timedelta(days=15)
    return pd.Series({"C": C, "M": M, "decision": decision, "ruleM": True, **cols}, name=month)


def rows_for(month):
    r = v4_row(month, z_tw_revenue=1.0, z_korea_mem_exports=1.0)
    return [("memory", "v4", "ruleM", r, "memory", None, "korea_customs")]


def snap_for(data_month, tw_latest, us_latest=None, C=1.0, M=0.5):
    comps = [{"key": "tw_revenue", "latest_month": tw_latest}]
    if us_latest:
        comps.append({"key": "us_imports", "latest_month": us_latest})
    return {"themes": [{"slug": "memory", "data_month": data_month, "C": C, "M": M, "components": comps},
                       {"slug": "basket-only", "components": [], "data_month": None}]}


@pytest.fixture
def run_main(tmp_path, monkeypatch):
    """Run monthly.main(--no-refresh) against a temp log with the model computations stubbed out."""
    log = tmp_path / "signal_log.jsonl"
    monkeypatch.setattr(monthly, "LOG", str(log))
    monkeypatch.setattr(monthly, "sanity_check", lambda: None)
    monkeypatch.setattr(monthly, "load_v4", lambda: None)

    def go(snap, rows, argv=(), calls=None, today="2026-10-17"):
        snap_path = tmp_path / "snapshot.json"
        snap_path.write_text(json.dumps(snap))
        monkeypatch.setattr(monthly, "SNAPSHOT", str(snap_path))
        monkeypatch.setattr(monthly, "v4_rows", lambda v4: rows)
        monkeypatch.setattr(monthly, "current_calls", lambda v4, r: calls or [
            {"bottleneck": "memory", "model": "v4", "decision_date": str(r[0][3].decision.date()),
             "data_month": str(r[0][3].name.date()), "state": "IN", "C": 1.0, "M": 0.5}])
        monkeypatch.setattr(monthly, "v6_calls", lambda s: [])
        real = monthly.stale_report
        monkeypatch.setattr(monthly, "stale_report", lambda s, r, today_=None: real(s, r, today))
        monthly.main(["--no-refresh", *argv])
        return monthly.read_log(str(log))
    return go


# ---------------------------------------------------------------- #1 freshness gate

def test_expected_month_calendar():
    assert monthly.expected_month("2026-10-17", 1) == pd.Timestamp("2026-09-01")  # TW / Korea: M-1
    assert monthly.expected_month("2026-10-17", 2) == pd.Timestamp("2026-08-01")  # US Census: M-2
    assert monthly.expected_month("2026-10-15", 1) == pd.Timestamp("2026-08-01")  # before the 16th decision date
    assert monthly.expected_month("2026-01-17", 1) == pd.Timestamp("2025-12-01")


def test_stale_report_fresh_and_stale():
    fresh = snap_for("2026-08-01", "2026-09-01", us_latest="2026-08-01")
    assert monthly.stale_report(fresh, rows_for("2026-09-01"), "2026-10-17") == []
    stale = snap_for("2026-07-01", "2026-08-01", us_latest="2026-07-01")
    rep = monthly.stale_report(stale, rows_for("2026-08-01"), "2026-10-17")
    assert any("memory: data_month 2026-07-01" in x for x in rep)
    assert any("tw_revenue latest 2026-08-01" in x for x in rep)
    assert any("us_imports latest 2026-07-01" in x for x in rep)
    assert any(x.startswith("v4 memory: data_month 2026-08-01") for x in rep)


def test_stale_report_missing_component_on_live_row():
    r = v4_row("2026-09-01", z_tw_revenue=1.0, z_korea_mem_exports=float("nan"))
    rep = monthly.stale_report({"themes": []}, [("memory", "v4", "ruleM", r, "memory", None, "x")], "2026-10-17")
    assert rep == ["v4 memory: korea_mem_exports missing for 2026-09-01"]


def test_monthly_refuses_stale(run_main, tmp_path):
    with pytest.raises(SystemExit) as e:
        run_main(snap_for("2026-07-01", "2026-08-01"), rows_for("2026-07-01"))
    assert "older than expected" in str(e.value) and "memory" in str(e.value)
    assert not (tmp_path / "signal_log.jsonl").exists()


def test_monthly_allow_stale_flags_entry(run_main):
    log = run_main(snap_for("2026-07-01", "2026-08-01", C=1.0, M=0.5), rows_for("2026-07-01"), ["--allow-stale"])
    assert len(log) == 1 and log[0]["stale_override"] is True and log[0]["stale"]


def test_monthly_logs_when_fresh(run_main):
    log = run_main(snap_for("2026-09-01", "2026-09-01"), rows_for("2026-09-01"))
    assert len(log) == 1 and "stale_override" not in log[0] and "forced_duplicate" not in log[0]


def test_refresh_stops_on_failed_step(monkeypatch):
    calls = []

    class R:
        def __init__(self, rc):
            self.returncode, self.stdout, self.stderr = rc, "", "boom"

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return R(1 if "fetch_market.py" in cmd[1] else 0)
    monkeypatch.setattr(monthly.subprocess, "run", fake_run)
    with pytest.raises(SystemExit) as e:
        monthly.refresh()
    assert "fetch_market.py" in str(e.value)
    assert len(calls) == 2  # nothing after the failed step ran


# ---------------------------------------------------------------- #10 duplicate guard

def test_duplicate_guard(run_main):
    snap, rows = snap_for("2026-09-01", "2026-09-01"), rows_for("2026-09-01")
    assert len(run_main(snap, rows)) == 1
    with pytest.raises(SystemExit) as e:
        run_main(snap, rows)
    assert "v4 decision 2026-10-16" in str(e.value)
    log = run_main(snap, rows, ["--force"])
    assert len(log) == 2 and log[1]["forced_duplicate"] is True and log[1]["prev"] == log[0]["hash"]


def test_duplicates_ignores_other_models_and_dates(tmp_path):
    log = tmp_path / "l.jsonl"
    log.write_text(json.dumps({"hash": "h", "calls": [{"model": "v4", "decision_date": "2026-09-16"}]}) + "\n")
    assert monthly.duplicates([{"model": "v6", "decision_date": "2026-09-16"},
                               {"model": "v4", "decision_date": "2026-10-16"},
                               {"model": "v6", "decision_date": None}], str(log)) == []
    assert monthly.duplicates([{"model": "v4", "decision_date": "2026-09-16"}], str(log)) == [("v4", "2026-09-16")]


# ---------------------------------------------------------------- #14 parity

def test_parity_check_unit():
    row = v4_row("2026-08-01", C=5.3257, M=-0.1457)
    assert monthly.parity_problems(row, snap_for("2026-08-01", "x", C=5.33, M=-0.15)) == []
    bad = monthly.parity_problems(row, snap_for("2026-08-01", "x", C=5.40, M=-0.15))
    assert len(bad) == 1 and bad[0].startswith("memory C")
    assert monthly.parity_problems(row, snap_for("2026-07-01", "x", C=5.33, M=-0.15))


def test_parity_mismatch_aborts(run_main, tmp_path):
    with pytest.raises(SystemExit) as e:
        run_main(snap_for("2026-09-01", "2026-09-01", C=9.9), rows_for("2026-09-01"))
    assert "differs from the snapshot" in str(e.value)
    assert not (tmp_path / "signal_log.jsonl").exists()


@pytest.mark.skipif(not os.path.exists(os.path.join(ROOT, "web/data/snapshot.json")), reason="no snapshot built")
def test_v4_memory_matches_snapshot():
    """Real local data: monthly's v4 memory C/M for the live row equals engine/build.py's snapshot (tol 0.01)."""
    snap = json.load(open(os.path.join(ROOT, "web/data/snapshot.json")))
    v4 = monthly.load_v4()
    mem, _, _ = v4.build_memory_v4()
    assert monthly.parity_problems(v4.live(mem), snap) == []


# ---------------------------------------------------------------- #2 universe price carry-over

def fake_download(frame):
    def dl(tickers, **kw):
        cols = [t for t in tickers if t in frame]
        return pd.concat({"Close": frame[cols]}, axis=1)
    return dl


def test_universe_price_carry_over(tmp_path, monkeypatch):
    idx = pd.bdate_range("2024-01-01", periods=300)
    tickers = ["AAA", "BBB", "CCC", "DDD"]
    prev = pd.DataFrame({t: 100.0 + i for i, t in enumerate(tickers)}, index=idx)
    out = tmp_path / "px.csv.gz"
    prev.to_csv(out)
    new_idx = pd.bdate_range("2024-01-01", periods=301)
    new = pd.DataFrame({"AAA": 1.0, "CCC": 3.0}, index=new_idx)
    new.loc[new_idx[-20:], "CCC"] = 3.0
    new.loc[new_idx[:-20], "CCC"] = float("nan")  # truncated history: only the last 20 days
    monkeypatch.setattr(fetch_universe_prices, "universe_tickers", lambda: tickers)
    monkeypatch.setattr(fetch_universe_prices.yf, "download", fake_download(new))
    assert fetch_universe_prices.main(str(out)) == 0  # 3 carried: warn, not fail
    got = pd.read_csv(out, index_col=0, parse_dates=True)
    assert got["AAA"].iloc[-1] == 1.0  # fresh ticker updated
    for t in ("BBB", "CCC", "DDD"):  # empty / short tickers keep their previous column
        assert got[t].dropna().tolist() == prev[t].tolist()


def test_universe_price_too_many_carried_exits_nonzero(tmp_path, monkeypatch, capsys):
    idx = pd.bdate_range("2024-01-01", periods=300)
    tickers = ["A", "B", "C", "D", "E"]
    pd.DataFrame({t: 10.0 for t in tickers}, index=idx).to_csv(tmp_path / "px.csv.gz")
    monkeypatch.setattr(fetch_universe_prices, "universe_tickers", lambda: tickers)
    monkeypatch.setattr(fetch_universe_prices.yf, "download", fake_download(pd.DataFrame({"A": [1.0]}, index=idx[:1])))
    assert fetch_universe_prices.main(str(tmp_path / "px.csv.gz")) == 1
    got = pd.read_csv(tmp_path / "px.csv.gz", index_col=0, parse_dates=True)
    assert got[tickers].notna().sum().min() >= 300  # merged safe file still written with every ticker
    assert "B, C, D, E" in capsys.readouterr().out


# ---------------------------------------------------------------- #2 Census cache fallback

def test_census_failed_refetch_keeps_cache(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    cached = [["850423", "TRANSFORMERS", "123"]]
    (raw / "2026-08.json").write_text(json.dumps(cached))

    def boom(*a, **k):
        raise ConnectionError("down")
    monkeypatch.setattr(fetch_census_all.requests, "get", boom)
    assert fetch_census_all.get("2026-08", "k", {"2026-08"}, str(raw), sleep=lambda s: None) == cached
    assert fetch_census_all.get("2026-09", "k", {"2026-09"}, str(raw), sleep=lambda s: None) == []


def test_census_204_and_empty_keep_cache(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    cached = [["850423", "TRANSFORMERS", "123"]]
    (raw / "2026-08.json").write_text(json.dumps(cached))

    class Resp:
        def __init__(self, code, body):
            self.status_code, self.body = code, body

        def raise_for_status(self):
            pass

        def json(self):
            return self.body
    monkeypatch.setattr(fetch_census_all.requests, "get", lambda *a, **k: Resp(204, None))
    assert fetch_census_all.get("2026-08", "k", {"2026-08"}, str(raw)) == cached
    monkeypatch.setattr(fetch_census_all.requests, "get", lambda *a, **k: Resp(200, [["hdr"], ["010101", "x", "1"]]))
    assert fetch_census_all.get("2026-08", "k", {"2026-08"}, str(raw)) == cached  # no ch.28/84/85/90 rows


def test_census_main_never_drops_months(tmp_path, monkeypatch):
    raw = tmp_path / "raw"
    raw.mkdir()
    out = tmp_path / "us.csv"
    for ym in ("2013-01", "2013-02"):
        (raw / f"{ym}.json").write_text(json.dumps([["850423", "TRANSFORMERS", "5"]]))
    # 2013-03 was in the previous output but has no cache file and the API is down
    pd.DataFrame([("2013-01-01", "850423", "T", 5.0), ("2013-02-01", "850423", "T", 5.0),
                  ("2013-03-01", "850423", "T", 7.0)], columns=fetch_census_all.COLS).to_csv(out, index=False)
    monkeypatch.setattr(fetch_census_all, "api_key", lambda: "k")
    monkeypatch.setattr(fetch_census_all, "month_list", lambda today: ["2013-01", "2013-02", "2013-03"])

    def boom(*a, **k):
        raise ConnectionError("down")
    monkeypatch.setattr(fetch_census_all.requests, "get", boom)
    monkeypatch.setattr(fetch_census_all.time, "sleep", lambda s: None)
    assert fetch_census_all.main(raw=str(raw), out=str(out)) == 0
    got = pd.read_csv(out, dtype={"hs": str})
    assert sorted(got.month.unique()) == ["2013-01-01", "2013-02-01", "2013-03-01"]
    assert got[got.month == "2013-03-01"].usd.tolist() == [7.0]


# ---------------------------------------------------------------- #15 context link scheme

@pytest.mark.parametrize("url,ok", [
    ("https://example.com/a", True), ("http://example.com", True), ("HTTPS://Example.com/x", True),
    ("javascript:alert(1)", False), (" javascript:alert(1)", False), ("java\tscript:alert(1)", False),
    ("data:text/html,<script>", False), ("vbscript:x", False), ("//example.com/a", False),
    ("ftp://example.com", False), ("https:", False), ("", False), (None, False)])
def test_safe_link(url, ok):
    assert context.safe_link(url) is ok


def test_gather_drops_non_http_links(monkeypatch):
    import time as _t
    stamp = _t.gmtime()
    entries = [{"link": "https://ok.example/a", "title": "transformer shortage", "published_parsed": stamp},
               {"link": "javascript:alert(document.cookie)", "title": "transformer shortage", "published_parsed": stamp},
               {"link": "data:text/html,x", "title": "transformer shortage", "published_parsed": stamp}]
    monkeypatch.setattr(context, "CFG", {"feeds": [{"id": "f", "name": "F", "kind": "press", "url": "https://f", "all": True}],
                                         "themes": {}})
    monkeypatch.setattr(context, "read_feed", lambda url: entries)
    assert [i["url"] for i in context.gather(3, 10)] == ["https://ok.example/a"]
