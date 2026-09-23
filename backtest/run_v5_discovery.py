"""Event study for the discovery scanner: executes backtest/PREREGISTRATION_v5_discovery.md exactly."""
import json
import numpy as np
import pandas as pd

flags = pd.read_csv("data/processed/discovery_tw.csv", dtype={"code": str}, parse_dates=["month", "event_date"])
px = pd.read_csv("data/processed/tw_prices.csv.gz", index_col=0, parse_dates=True)
mkt = px["^TWII"].dropna()

# DATA QUALITY (disclosed, not a rule change): Yahoo occasionally prints a one-day bad tick (e.g. 8917 at 0.02 on
# 2008-08-11 between ~12 closes). Any close below 20% or above 5x its centred 252-day median is treated as missing.
med = px.rolling(252, center=True, min_periods=20).median()
bad = (px < 0.2 * med) | (px > 5 * med)
BAD_TICKS = int(bad.sum().sum())
px = px.mask(bad)


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


rows, dropped = [], 0
for r in flags.itertuples():
    if r.ticker not in px or px[r.ticker].dropna().empty or px[r.ticker].dropna().index[0] > r.event_date:
        dropped += 1
        continue
    rec = {"code": r.code, "month": r.month, "event_date": r.event_date}
    for h in (6, 12):
        d0, ret = fwd(px[r.ticker], r.event_date, h)
        _, m = fwd(mkt, d0, h) if d0 is not None else (None, None)
        rec[f"ex{h}"] = ret - m if ret is not None and m is not None else np.nan
    rows.append(rec)
ev = pd.DataFrame(rows)


def stats(df, col):
    d = df.dropna(subset=[col])
    if d.empty:
        return {}
    by_m = d.groupby("month")[col].mean()  # cluster by event month
    t = by_m.mean() / (by_m.std(ddof=1) / np.sqrt(len(by_m))) if len(by_m) > 1 else np.nan
    return {"n_events": int(len(d)), "n_months": int(len(by_m)), "mean_pct": round(d[col].mean() * 100, 2),
            "median_pct": round(d[col].median() * 100, 2), "hit_rate_pct": round((d[col] > 0).mean() * 100, 1),
            "t_clustered": round(float(t), 2)}


oos = ev[(ev.event_date >= "2008-01-01") & (ev.event_date <= "2016-12-31")]
ins = ev[ev.event_date >= "2017-01-01"]
res = {"bad_ticks_removed": BAD_TICKS, "dropped_no_price": dropped, "total_flags": int(len(flags)),
       "OOS": {"6m": stats(oos, "ex6"), "12m": stats(oos, "ex12")},
       "IS": {"6m": stats(ins, "ex6"), "12m": stats(ins, "ex12")}}
o = res["OOS"]["6m"]
res["PASS"] = bool(o and o["mean_pct"] > 0 and o["t_clustered"] > 2 and o["median_pct"] > 0)
ev.to_csv("backtest/v5_discovery_events.csv", index=False)
json.dump(res, open("backtest/v5_discovery_results.json", "w"), indent=1)
print(json.dumps(res, indent=1))
