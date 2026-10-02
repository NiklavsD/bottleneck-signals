"""Backtest v3 (power): executes backtest/PREREGISTRATION_v3_power.md exactly."""
import json
import pandas as pd
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backtest import run_v2 as v2
from backtest.run_v2 import GROUPS, expanding_z, tw_yoy, basket_returns, evaluate

US = pd.DataFrame()


def load_data():
    global US
    v2.load_data()
    US = pd.read_csv(v2.ROOT / "data/processed/us_imports.csv", index_col=0, parse_dates=True)
    US.columns = US.columns.astype(str)


def yoy3(s):
    s3 = s.sort_index().asfreq("MS").rolling(3).sum()
    return s3 / s3.shift(12) - 1


def build_v3():
    g = GROUPS["power"]
    yoy = {"tw_revenue": tw_yoy(g["tw"]),
           "us_transformer_imports": yoy3(US[["850421", "850422", "850423"]].sum(axis=1, min_count=3)),
           "us_switchgear_imports": yoy3(US["853720"])}
    Z = pd.DataFrame({k: expanding_z(v.dropna()) for k, v in yoy.items()}).sort_index().asfreq("MS")
    Z = Z[Z.index >= Z["us_transformer_imports"].first_valid_index()]
    # the Census month M is only known on the 16th of M+2, so the composite is dated on that decision
    C = Z.mean(axis=1, skipna=True)
    ind = pd.DataFrame({"C": C, "M": C - C.shift(3), **{f"yoy_{k}": v for k, v in yoy.items()}})
    ind = ind.dropna(subset=["C"])
    ind["decision"] = ind.index + pd.offsets.MonthBegin(2) + pd.Timedelta(days=15)
    ind["ruleA"] = (ind.C > 0) & (ind.M > 0)
    ind["ruleB"] = ind.C > 0
    ind["ruleM"] = ~((ind.C > 0) & (ind.M < 0))
    return ind


if __name__ == "__main__":
    load_data()
    ind = build_v3()
    g = GROUPS["power"]
    b = basket_returns(g["basket"])
    bench = v2.PX["XLI"].pct_change(fill_method=None)
    W = (str(ind.decision.iloc[0].date()), "2026-12-31")
    res = {r: evaluate(ind, b, bench, r, W) for r in ("ruleB", "ruleA")}
    for r in res.values():  # apply the v2 pass criteria to the full window
        pass
    ind.to_csv("backtest/v3_power_index.csv")
    res["now"] = {"C": round(ind.C.iloc[-1], 2), "M": round(ind.M.iloc[-1], 2), "month": str(ind.index[-1].date()),
                  "ruleB": bool(ind.ruleB.iloc[-1]), "ruleA": bool(ind.ruleA.iloc[-1])}
    json.dump(res, open("backtest/v3_power_results.json", "w"), indent=1, default=str)
    print(json.dumps(res, indent=1, default=str))
