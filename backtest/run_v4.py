"""Backtest v4: executes backtest/PREREGISTRATION_v4.md exactly. Do not tune here; bump a version instead."""
import json
import os
import numpy as np
import pandas as pd

exec(open("backtest/run_v3_power.py").read().split("\nif __name__ == ")[0])

KCS_PATH = "data/processed/korea_customs_exports.csv"


def korea_memory_series():
    """Korea Customs (tradedata.go.kr) HS 854232 if it validates within 5% of Comtrade, else Comtrade."""
    comtrade = KR["854232"].dropna()
    if not os.path.exists(KCS_PATH):
        return comtrade, "comtrade", None
    k = pd.read_csv(KCS_PATH, dtype={"hs": str}, parse_dates=["month"])
    kcs = k[k.hs == "854232"].set_index("month")["usd"].sort_index()
    both = pd.concat([kcs, comtrade], axis=1, keys=["kcs", "ct"]).dropna()
    both = both[(both.index >= "2013-01-01") & (both.index <= "2025-12-01")]
    med = float((both.kcs / both.ct - 1).abs().median())
    return (kcs, "korea_customs", med) if med <= 0.05 else (comtrade, "comtrade", med)


def composite(yoy):
    Z = pd.DataFrame({k: expanding_z(v.dropna()) for k, v in yoy.items()}).sort_index()
    C = Z.mean(axis=1, skipna=True)
    dz = Z - Z.shift(3)  # NaN unless the component exists at both t and t-3
    M = dz.mean(axis=1, skipna=True)
    return Z, C, M


def build_memory_v4():
    g = GROUPS["memory"]
    kr, src, med = korea_memory_series()
    k3 = kr.rolling(3).sum()
    yoy = {"tw_revenue": tw_yoy(g["tw"]), "korea_mem_exports": k3 / k3.shift(12) - 1}
    Z, C, M = composite(yoy)
    ind = pd.DataFrame({"C": C, "M": M, **{f"z_{k}": Z[k] for k in Z}}).dropna(subset=["C"])
    ind["decision"] = ind.index + pd.offsets.MonthBegin(1) + pd.Timedelta(days=15)
    ind["ruleM"] = ~((ind.C > 0) & (ind.M < 0))
    ind["ruleA"] = (ind.C > 0) & (ind.M > 0)
    ind["ruleB"] = ind.C > 0
    return ind, src, med


def tilt_eval(ind, b, bench, window):
    lo, hi = pd.Timestamp(window[0]), pd.Timestamp(window[1])
    days = b.index[(b.index >= max(lo, ind.decision.iloc[0])) & (b.index <= hi)]
    ex = (b - bench).reindex(days).fillna(0)
    sign = ind.set_index("decision")["ruleB"].map({True: 1.0, False: -1.0})
    pos = sign.reindex(days, method="ffill").shift(1).fillna(0)
    tilt, always = ex * pos, ex

    def sh(x):
        return round(float(x.mean() / x.std() * np.sqrt(252)), 2)

    res = {"from": str(days[0].date()), "to": str(days[-1].date()),
           "tilt_sharpe": sh(tilt), "always_overweight_sharpe": sh(always),
           "tilt_ann_excess_pct": round(float(tilt.mean() * 252 * 100), 1),
           "always_ann_excess_pct": round(float(always.mean() * 252 * 100), 1),
           "pct_overweight": round(float((pos > 0).mean() * 100), 1)}
    res["PASS"] = bool(res["tilt_sharpe"] > res["always_overweight_sharpe"] and tilt.mean() > 0)
    return res


def live(ind):
    """Latest row whose decision date has passed: later rows use data that is not fully published yet."""
    return ind[ind.decision <= pd.Timestamp.today().normalize()].iloc[-1]


if __name__ == "__main__":
    out = {}
    mem, src, med = build_memory_v4()
    g = GROUPS["memory"]
    b, bench = basket_returns(g["basket"]), PX["SOXX"].pct_change(fill_method=None)
    out["memory"] = {"korea_source": src, "korea_vs_comtrade_median_abs_diff": med,
                     "OOS": evaluate(mem, b, bench, "ruleM", OOS), "IS": evaluate(mem, b, bench, "ruleM", IS),
                     "now": (lambda r: {"month": str(r.name.date()), "decision": str(r.decision.date()),
                                        "C": round(float(r.C), 2), "M": round(float(r.M), 2),
                                        "state": "IN" if r.ruleM else "OUT"})(live(mem))}
    mem.to_csv("backtest/v4_memory_index.csv")

    specs = {"optics": (build(GROUPS["optics"]), GROUPS["optics"], "SOXX", OOS, "OOS"),
             "power_v2": (build(GROUPS["power"]), GROUPS["power"], "XLI", OOS, "OOS"),
             "power_v3": (build_v3(), GROUPS["power"], "XLI", None, "FULL_UNVALIDATED")}
    for name, (ind, grp, bm, win, label) in specs.items():
        b, bench = basket_returns(grp["basket"]), PX[bm].pct_change(fill_method=None)
        w = win or (str(ind.decision.iloc[0].date()), "2026-12-31")
        out[name] = {"window": label, "tilt": tilt_eval(ind, b, bench, w),
                     "IS_tilt": tilt_eval(ind, b, bench, IS) if win else None,
                     "now": (lambda r: {"month": str(r.name.date()), "decision": str(r.decision.date()),
                                        "C": round(float(r.C), 2),
                                        "tilt": "OVERWEIGHT" if r.ruleB else "UNDERWEIGHT"})(live(ind))}
    json.dump(out, open("backtest/v4_results.json", "w"), indent=1, default=str)
    print(json.dumps(out, indent=1, default=str))
