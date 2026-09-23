"""Backtest v2: executes backtest/PREREGISTRATION_v2.md exactly. Do not tune here; bump a version instead."""
import json
import numpy as np
import pandas as pd

TW = pd.read_csv("data/processed/tw_revenue.csv", dtype={"code": str}, parse_dates=["month"])
PX = pd.read_csv("data/processed/prices.csv", index_col=0, parse_dates=True)
FRED = pd.read_csv("data/processed/fred.csv", index_col=0, parse_dates=True)
KR = pd.read_csv("data/processed/korea_chip_exports.csv", index_col=0, parse_dates=True)

PPI = ["PCU335311335311", "PCU335313335313", "PCU333611333611"]
GROUPS = {
    "memory": dict(tw=["2408", "2344", "2337", "2451", "8299", "3260"], ppi=[], korea=True, rule="ruleM",
                   bench="SOXX",
                   basket=["MU", "WDC", "STX", "000660.KS", "005930.KS", "2408.TW", "2344.TW", "2337.TW",
                           "2451.TW", "8299.TWO", "3260.TWO"]),
    "optics": dict(tw=["3081", "4971", "2455", "3163", "3363", "2345"], ppi=[], korea=False, rule="ruleB",
                   bench="SOXX",
                   basket=["COHR", "CIEN", "FN", "AAOI", "LITE", "VIAV", "2455.TW", "4971.TWO", "3163.TWO",
                           "3363.TWO", "3081.TWO", "2345.TW"]),
    "power": dict(tw=["1519", "1503", "1513", "1514"], ppi=PPI, korea=False, rule="ruleB", bench="XLI",
                  basket=["ETN", "HUBB", "POWL", "SIE.DE", "ABBN.SW", "SU.PA", "6501.T", "1519.TW", "1503.TW",
                          "1513.TW", "1514.TW", "VRT"]),
}
GROUPS["power-TWonly"] = dict(GROUPS["power"], ppi=[])
OOS = ("2008-01-01", "2017-02-28")
IS = ("2017-03-01", "2026-12-31")


def expanding_z(s, min_obs=24):
    return (s - s.expanding(min_obs).mean()) / s.expanding(min_obs).std()


def tw_yoy(codes):
    w = TW[TW.code.isin(codes)].pivot_table(index="month", columns="code", values="revenue_k_twd")
    r3 = w.rolling(3).sum()
    prev = r3.shift(12)
    both = r3.notna() & prev.notna()
    return r3.where(both).sum(axis=1, min_count=1) / prev.where(both).sum(axis=1, min_count=1) - 1


def build(g):
    yoy = {"tw_revenue": tw_yoy(g["tw"])}
    for sid in g["ppi"]:
        s = FRED[sid].dropna()
        s = s[s.index >= "2003-01-01"]
        yoy[sid] = s / s.shift(12) - 1
    if g["korea"]:
        k = KR["854232"].dropna()
        k3 = k.rolling(3).sum()
        yoy["korea_mem_exports"] = k3 / k3.shift(12) - 1
    Z = pd.DataFrame({k: expanding_z(v.dropna()) for k, v in yoy.items()}).sort_index()
    C = Z.mean(axis=1, skipna=True)
    ind = pd.DataFrame({"C": C, "M": C - C.shift(3), **{f"yoy_{k}": v for k, v in yoy.items()}})
    ind = ind.dropna(subset=["C"])
    ind["decision"] = ind.index + pd.offsets.MonthBegin(1) + pd.Timedelta(days=15)
    ind["ruleA"] = (ind.C > 0) & (ind.M > 0)
    ind["ruleB"] = ind.C > 0
    ind["ruleM"] = ~((ind.C > 0) & (ind.M < 0))
    return ind


def basket_returns(tickers):
    px = PX[[t for t in tickers if t in PX]].ffill(limit=5)
    return px.pct_change(fill_method=None).mean(axis=1, skipna=True)


def perf(daily):
    daily = daily.dropna()
    eq = (1 + daily).cumprod()
    yrs = (eq.index[-1] - eq.index[0]).days / 365.25
    return dict(cagr=round((eq.iloc[-1] ** (1 / yrs) - 1) * 100, 1),
                max_dd=round((eq / eq.cummax() - 1).min() * 100, 1),
                sharpe=round(daily.mean() / daily.std() * np.sqrt(252), 2))


def evaluate(ind, b, bench, rule, window):
    lo, hi = pd.Timestamp(window[0]), pd.Timestamp(window[1])
    days = b.index[(b.index >= max(lo, ind.decision.iloc[0])) & (b.index <= hi)]
    pos = ind.set_index("decision")[rule].astype(float).reindex(days, method="ffill").shift(1).fillna(0)
    strat = b.reindex(days).fillna(0) * pos
    res = {"rule": rule, "from": str(days[0].date()), "to": str(days[-1].date()),
           "strategy": perf(strat), "buy_hold": perf(b.reindex(days)), "benchmark": perf(bench.reindex(days)),
           "time_in_market": round(pos.mean() * 100, 1)}
    lvl_b = (1 + b.fillna(0)).cumprod()
    lvl_x = (1 + bench.fillna(0)).cumprod()
    fw = []
    for m, r in ind.iterrows():
        d = r.decision
        d1 = d + pd.DateOffset(months=12)
        if d < lo or d > hi or d1 > lvl_b.index[-1]:
            continue
        ex = (lvl_b.asof(d1) / lvl_b.asof(d)) - (lvl_x.asof(d1) / lvl_x.asof(d))
        fw.append((bool(r[rule]), ex))
    fw = pd.DataFrame(fw, columns=["on", "ex"])
    on, off = fw[fw.on].ex, fw[~fw.on].ex
    res["fwd12_excess_on"] = round(on.mean() * 100, 1) if len(on) else None
    res["fwd12_excess_off"] = round(off.mean() * 100, 1) if len(off) else None
    res["n_on_off"] = [len(on), len(off)]
    if window == OOS:
        c1 = len(on) > 0 and len(off) > 0 and on.mean() > off.mean()
        c2 = res["strategy"]["sharpe"] >= res["buy_hold"]["sharpe"]
        res["pass_c1_excess_spread"], res["pass_c2_sharpe"], res["PASS"] = bool(c1), bool(c2), bool(c1 and c2)
    return res


def run(name):
    g = GROUPS[name]
    ind = build(g)
    b = basket_returns(g["basket"])
    bench = PX[g["bench"]].pct_change(fill_method=None)
    out = {"first_signal_month": str(ind.index[0].date()), "last_signal_month": str(ind.index[-1].date()),
           "now": {r: bool(ind[r].iloc[-1]) for r in ("ruleA", "ruleB", "ruleM")},
           "now_C": round(ind.C.iloc[-1], 2), "now_M": round(ind.M.iloc[-1], 2)}
    for label, w in (("OOS", OOS), ("IS", IS)):
        out[label] = {r: evaluate(ind, b, bench, r, w) for r in dict.fromkeys([g["rule"], "ruleA"])}
    ind.to_csv(f"backtest/v2_{name}_index.csv")
    return out


if __name__ == "__main__":
    res = {n: run(n) for n in GROUPS}
    json.dump(res, open("backtest/v2_results.json", "w"), indent=1, default=str)
    print(json.dumps(res, indent=1, default=str))
