"""Backtest v1: executes backtest/PREREGISTRATION.md exactly. Do not tune here; bump a version instead."""
import json
import numpy as np
import pandas as pd

TW = pd.read_csv("data/processed/tw_revenue.csv", dtype={"code": str}, parse_dates=["month"])
PX = pd.read_csv("data/processed/prices.csv", index_col=0, parse_dates=True)
FRED = pd.read_csv("data/processed/fred.csv", index_col=0, parse_dates=True)

GROUPS = {
    "memory": dict(tw=["2408", "2344", "2337", "2451", "8299", "3260"], ppi=[],
                   basket=["MU", "WDC", "STX", "000660.KS"]),
    "optics": dict(tw=["3081", "4971", "2455", "3163", "3363", "2345"], ppi=[],
                   basket=["LITE", "COHR", "AAOI", "FN", "CIEN"]),
    "power": dict(tw=["1519", "1503", "1513", "1514"],
                  ppi=["PCU335311335311", "PCU335313335313", "PCU333611333611"],
                  basket=["ETN", "HUBB", "POWL", "VRT", "SIE.DE", "1519.TW"]),
}


def expanding_z(s, min_obs=24):
    mu = s.expanding(min_obs).mean()
    sd = s.expanding(min_obs).std()
    return (s - mu) / sd


def tw_yoy(codes):
    w = TW[TW.code.isin(codes)].pivot_table(index="month", columns="code", values="revenue_k_twd")
    # Sum only over members present in both periods, so a new listing does not look like growth.
    r3 = w.rolling(3).sum()
    prev = r3.shift(12)
    both = r3.notna() & prev.notna()
    return r3.where(both).sum(1, min_count=1) / prev.where(both).sum(1, min_count=1) - 1


def build(group):
    g = GROUPS[group]
    z = {}
    yoy = {}
    yoy["tw_revenue"] = tw_yoy(g["tw"])
    z["tw_revenue"] = expanding_z(yoy["tw_revenue"].dropna())
    for sid in g["ppi"]:
        s = FRED[sid].dropna()
        s = s[s.index >= "2012-01-01"]
        yoy[sid] = s / s.shift(12) - 1
        z[sid] = expanding_z(yoy[sid].dropna())
    Z = pd.DataFrame(z).sort_index()
    C = Z.mean(1, skipna=True)
    M = C - C.shift(3)
    ind = pd.DataFrame({"C": C, "M": M, **{f"yoy_{k}": v for k, v in yoy.items()}}).dropna(subset=["C"])
    # decision date for month M = 16th of M+1 (covers TW 10th deadline and PPI release)
    ind["decision"] = ind.index + pd.offsets.MonthBegin(1) + pd.Timedelta(days=15)
    ind["ruleA"] = (ind.C > 0) & (ind.M > 0)
    ind["ruleB"] = ind.C > 0
    return ind


def basket_returns(tickers):
    px = PX[[t for t in tickers if t in PX]].ffill(limit=5)
    rets = px.pct_change(fill_method=None)
    return rets.mean(1, skipna=True)  # equal-weight, daily rebalanced ~ close to monthly EW


def perf(daily):
    daily = daily.dropna()
    eq = (1 + daily).cumprod()
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    dd = (eq / eq.cummax() - 1).min()
    return dict(cagr=round(cagr * 100, 1), max_dd=round(dd * 100, 1), total=round((eq.iloc[-1] - 1) * 100, 1))


def run(group):
    ind = build(group)
    b = basket_returns(GROUPS[group]["basket"])
    spy = PX["SPY"].pct_change(fill_method=None)
    start = ind.decision.iloc[0]
    days = b.index[b.index >= start]
    out = {"group": group, "first_decision": str(start.date()), "last_signal_month": str(ind.index[-1].date())}
    for rule in ("ruleA", "ruleB"):
        pos = ind.set_index("decision")[rule].astype(float)
        pos = pos.reindex(days, method="ffill").shift(1)  # act the day after the decision date
        strat = (b.reindex(days) * pos).fillna(0)
        switches = pos.diff().abs().fillna(0)
        strat_cost = strat - switches * 0.001
        out[rule] = dict(**perf(strat), after_10bp=perf(strat_cost)["cagr"],
                         time_in_market=round(pos.mean() * 100, 1), switches=int(switches.sum()),
                         now="ON" if bool(ind[rule].iloc[-1]) else "OFF")
    out["buy_hold"] = perf(b.reindex(days))
    out["spy"] = perf(spy.reindex(days))
    # forward returns conditional on state at decision dates
    lvl = (1 + b.fillna(0)).cumprod()
    fw = []
    for _, r in ind.iterrows():
        d = r.decision
        if d > lvl.index[-1]:
            continue
        p0 = lvl.asof(d)
        f = {"month": r.name, "A": r.ruleA, "B": r.ruleB}
        for h in (6, 12):
            d1 = d + pd.DateOffset(months=h)
            f[f"fwd{h}"] = lvl.asof(d1) / p0 - 1 if d1 <= lvl.index[-1] else np.nan
        fw.append(f)
    fw = pd.DataFrame(fw).set_index("month")
    fw = fw[fw.index >= ind.index[0]]
    for rule, col in (("ruleA", "A"), ("ruleB", "B")):
        for h in (6, 12):
            on = fw.loc[fw[col], f"fwd{h}"].dropna()
            off = fw.loc[~fw[col], f"fwd{h}"].dropna()
            out[rule][f"fwd{h}_on_mean"] = round(on.mean() * 100, 1)
            out[rule][f"fwd{h}_off_mean"] = round(off.mean() * 100, 1)
            out[rule][f"n{h}_on_off"] = [len(on), len(off)]
    ind.to_csv(f"backtest/v1_{group}_index.csv")
    return out, ind


if __name__ == "__main__":
    res = {}
    for g in GROUPS:
        res[g], ind = run(g)
    json.dump(res, open("backtest/v1_results.json", "w"), indent=1, default=str)
    print(json.dumps(res, indent=1, default=str))
