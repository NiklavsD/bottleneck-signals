"""Event study for the discovery scanner: executes backtest/PREREGISTRATION_v5_discovery.md exactly."""
import json
import argparse
import datetime as dt
import hashlib
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


def fwd(series, d, months):
    s = series.dropna()
    s = s[s.index >= d]
    if s.empty:
        return None, None
    d0 = s.index[0]
    d1 = d0 + pd.DateOffset(months=months)
    if d1 > series.dropna().index[-1]:
        return d0, None
    return d0, float(series.dropna().asof(d1) / s.iloc[0] - 1)


def event_returns(flags, px):
    mkt = px["^TWII"].dropna()
    # Disclosed historical bad-tick guard; the correction below does not change it.
    med = px.rolling(252, center=True, min_periods=20).median()
    bad = (px < 0.2 * med) | (px > 5 * med)
    px = px.mask(bad)
    rows, dropped = [], 0
    for r in flags.itertuples():
        if r.ticker not in px or px[r.ticker].dropna().empty or px[r.ticker].dropna().index[0] > r.event_date:
            dropped += 1
            continue
        rec = {"code": r.code, "month": r.month, "event_date": r.event_date}
        for h in (6, 12):
            d0, ret = fwd(px[r.ticker], r.event_date, h)
            _, market = fwd(mkt, d0, h) if d0 is not None else (None, None)
            rec[f"ex{h}"] = ret - market if ret is not None and market is not None else np.nan
        rows.append(rec)
    events = pd.DataFrame(rows, columns=["code", "month", "event_date", "ex6", "ex12"])
    return events, {"bad_ticks_removed": int(bad.sum().sum()), "dropped_no_price": dropped,
                    "total_flags": int(len(flags))}


def clustered_mean(df, col):
    """Event-weighted intercept estimate and event-month cluster-robust standard error (CR1)."""
    d = df.dropna(subset=[col])
    if d.empty:
        return np.nan, np.nan, np.nan, 0
    mean = float(d[col].mean())
    cluster = pd.to_datetime(d.event_date).dt.to_period("M")
    residual_sums = (d[col] - mean).groupby(cluster).sum()
    groups = len(residual_sums)
    se = np.sqrt(groups / (groups - 1) * (residual_sums ** 2).sum() / len(d) ** 2) if groups > 1 else np.nan
    t = mean / se if se > 0 else np.nan
    return mean, float(se), float(t), groups


def stats(df, col):
    d = df.dropna(subset=[col])
    if d.empty:
        return {}
    mean, se, t, groups = clustered_mean(d, col)
    return {"n_events": int(len(d)), "n_months": groups, "mean_pct": round(mean * 100, 2),
            "median_pct": round(d[col].median() * 100, 2), "hit_rate_pct": round((d[col] > 0).mean() * 100, 1),
            "se_clustered_pct": round(se * 100, 4) if np.isfinite(se) else None,
            "t_clustered": round(t, 2) if np.isfinite(t) else None}


def summarize(events, metadata=None):
    oos = events[(events.event_date >= "2008-01-01") & (events.event_date <= "2016-12-31")]
    ins = events[events.event_date >= "2017-01-01"]
    res = {**(metadata or {}), "OOS": {"6m": stats(oos, "ex6"), "12m": stats(oos, "ex12")},
           "IS": {"6m": stats(ins, "ex6"), "12m": stats(ins, "ex12")},
           "estimator": "event-weighted mean; CR1 standard error clustered by event month"}
    mean, _, t, _ = clustered_mean(oos, "ex6")
    res["PASS"] = bool(mean > 0 and t > 2 and oos.ex6.dropna().median() > 0)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from-events", type=Path, help="Recalculate statistics from stored events without price fetching.")
    ap.add_argument("--output", type=Path, default=ROOT / "backtest/v5_discovery_results_corrected.json")
    ap.add_argument("--events-output", type=Path, default=ROOT / "backtest/v5_discovery_events_corrected.csv")
    args = ap.parse_args(argv)
    if args.output.resolve() == ROOT / "backtest/v5_discovery_results.json" or args.events_output.resolve() == ROOT / "backtest/v5_discovery_events.csv":
        ap.error("Historical v5 result and event files are immutable; choose separate corrected outputs.")
    if args.from_events:
        events = pd.read_csv(args.from_events, dtype={"code": str}, parse_dates=["month", "event_date"])
        old = json.load(open(ROOT / "backtest/v5_discovery_results.json"))
        metadata = {k: old[k] for k in ("bad_ticks_removed", "dropped_no_price", "total_flags")}
        try:
            source = args.from_events.resolve().relative_to(ROOT).as_posix()
        except ValueError:
            source = args.from_events.resolve().as_posix()
        canonical = args.from_events.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
        metadata.update(events_source=source,
                        events_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                        events_hash_format="UTF-8 with LF line endings; original trailing newline retained",
                        correction_scope="Statistics only; stored event returns unchanged.")
    else:
        flags = pd.read_csv(ROOT / "data/processed/discovery_tw.csv", dtype={"code": str}, parse_dates=["month", "event_date"])
        px = pd.read_csv(ROOT / "data/processed/tw_prices.csv.gz", index_col=0, parse_dates=True)
        events, metadata = event_returns(flags, px)
        events.to_csv(args.events_output, index=False)
        metadata["events_source"] = args.events_output.resolve().relative_to(ROOT).as_posix()
    res = summarize(events, metadata)
    res["computed_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as handle:
        json.dump(res, handle, indent=1, allow_nan=False)
    print(json.dumps(res, indent=1, allow_nan=False))


if __name__ == "__main__":
    main()
