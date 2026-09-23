"""Build the site snapshot: every theme's pressure index, its v6 call and test, discovery flags and the track record.

Implements backtest/PREREGISTRATION_v6_universe.md (memory keeps v4 Rule M).
Output: web/data/snapshot.json, which the website reads. Run after the data refresh.
"""
import datetime as dt
import hashlib
import json
import re
import os

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
OUT = "web/data/snapshot.json"
TODAY = pd.Timestamp.today().normalize()
OOS = (pd.Timestamp("2008-03-01"), pd.Timestamp("2017-02-28"))

THEMES = json.load(open("config/themes.json"))
TW = pd.read_csv("data/processed/tw_revenue.csv", dtype={"code": str}, parse_dates=["month"])
PX = pd.read_csv("data/processed/universe_prices.csv.gz", index_col=0, parse_dates=True)
US = pd.read_csv("data/processed/us_imports_all.csv", dtype={"hs": str}, parse_dates=["month"])
KR = pd.read_csv("data/processed/korea_customs_exports.csv", dtype={"hs": str}, parse_dates=["month"])
NAMES_EN = pd.read_csv("data/processed/tw_names_en.csv", dtype={"code": str}).set_index("code")["name_en"].to_dict()
TW_IND = pd.read_csv("data/processed/tw_industry.csv", dtype={"code": str}).set_index("code")["industry"].to_dict()
US_W = US.pivot_table(index="month", columns="hs", values="usd")
US_DESC = US.sort_values("month").drop_duplicates("hs", keep="last").set_index("hs")["desc"].to_dict()
KR_W = KR.pivot_table(index="month", columns="hs", values="usd")
TW_W = TW.pivot_table(index="month", columns="code", values="revenue_k_twd")


def expanding_z(s, min_obs=24):
    s = s.dropna()
    return (s - s.expanding(min_obs).mean()) / s.expanding(min_obs).std()


def yoy3_sum(w, cols):
    """YoY of the trailing 3-month sum over members present in both periods (v1 rule)."""
    cols = [c for c in cols if c in w]
    if not cols:
        return None
    x = w[cols]
    r3 = x.rolling(3).sum()
    prev = r3.shift(12)
    both = r3.notna() & prev.notna()
    return (r3.where(both).sum(axis=1, min_count=1) / prev.where(both).sum(axis=1, min_count=1) - 1).dropna()


def components(t):
    """Returns {key: (yoy series, lag_months, meta)}; lag = months after data month until usable (16th)."""
    comps = {}
    tw_codes = [s["code"] for s in t.get("tw_signals", [])]
    if tw_codes:
        y = yoy3_sum(TW_W, tw_codes)
        if y is not None and len(y) > 30:
            comps["tw_revenue"] = (y, 1, {"label": "Taiwan supplier revenue",
                                          "source": "MOPS monthly revenue (TWSE/TPEx)",
                                          "url": "https://mopsov.twse.com.tw/mops/web/t21sc04_ifrs",
                                          "detail": ", ".join(f"{NAMES_EN.get(c, s['name'])} ({c})"
                                                              for c, s in zip(tw_codes, t['tw_signals']))})
    us_codes = [c["hs"] for c in t.get("us_import_hs6", [])]
    if us_codes:
        y = yoy3_sum(US_W, us_codes)
        if y is not None and len(y) > 30:
            comps["us_imports"] = (y, 2, {"label": "US imports",
                                          "source": "US Census Bureau, international trade API",
                                          "url": "https://www.census.gov/foreign-trade/data/index.html",
                                          "detail": ", ".join(f"HS {c} {US_DESC.get(c, '').title()}"
                                                              for c in us_codes)})
    kr_codes = [c["hs"] for c in t.get("kr_export_hs", [])]
    if kr_codes:
        y = yoy3_sum(KR_W, kr_codes)
        if y is not None and len(y) > 30:
            comps["kr_exports"] = (y, 1, {"label": "Korean exports",
                                          "source": "Korea Customs Service (tradedata.go.kr)",
                                          "url": "https://tradedata.go.kr/cts/index.do",
                                          "detail": ", ".join(f"HS {c['hs']} {c.get('desc', '')}"
                                                              for c in t['kr_export_hs'])})
    return comps


def build_index(comps):
    """Index per v4/v6: C = mean z, M like-for-like 3-month change. Dated by decision date."""
    Z = pd.DataFrame({k: expanding_z(v[0]) for k, v in comps.items()}).sort_index()
    C = Z.mean(axis=1, skipna=True)
    M = (Z - Z.shift(3)).mean(axis=1, skipna=True)
    ind = pd.DataFrame({"C": C, "M": M}).join(Z.add_prefix("z_")).dropna(subset=["C"])
    lag = max(v[1] for v in comps.values())
    ind["decision"] = ind.index + pd.offsets.MonthBegin(lag) + pd.Timedelta(days=15)
    return ind


def basket_returns(tickers):
    cols = [t for t in tickers if t in PX]
    px = PX[cols].ffill(limit=5)
    return px.pct_change(fill_method=None).mean(axis=1, skipna=True)


def sharpe(x):
    return float(x.mean() / x.std() * np.sqrt(252)) if x.std() > 0 else float("nan")


def tilt_test(ind, b, bench, lo, hi):
    days = b.index[(b.index >= max(lo, ind.decision.iloc[0])) & (b.index <= hi)]
    if len(days) < 500:
        return None
    ex = (b - bench).reindex(days).fillna(0)
    pos = (ind.set_index("decision")["C"] > 0).map({True: 1.0, False: -1.0})
    pos = pos.reindex(days, method="ffill").shift(1).fillna(0)
    tilt = ex * pos
    return {"from": str(days[0].date()), "to": str(days[-1].date()),
            "tilt_sharpe": round(sharpe(tilt), 2), "always_sharpe": round(sharpe(ex), 2),
            "tilt_ann_pct": round(float(tilt.mean() * 252 * 100), 1),
            "always_ann_pct": round(float(ex.mean() * 252 * 100), 1),
            "pass": bool(sharpe(tilt) > sharpe(ex) and tilt.mean() > 0)}


def pct(a, b):
    return None if a is None or b is None or not np.isfinite(a) or not np.isfinite(b) or b == 0 else round((a / b - 1) * 100, 1)


def price_stats(ticker):
    if ticker not in PX:
        return {}
    s = PX[ticker].dropna()
    if s.empty:
        return {}
    last = float(s.iloc[-1])

    def back(days):
        d = s.index[-1] - pd.Timedelta(days=days)
        return float(s.asof(d)) if d >= s.index[0] else None
    ytd0 = s[s.index < pd.Timestamp(s.index[-1].year, 1, 1)]
    return {"last": round(last, 2), "asof": str(s.index[-1].date()), "r1m": pct(last, back(30)), "r3m": pct(last, back(91)),
            "r1y": pct(last, back(365)), "ytd": pct(last, float(ytd0.iloc[-1]) if len(ytd0) else None)}


def monthly_series(s, start="2012-01-01"):
    s = s[s.index >= start].dropna()
    return [[d.strftime("%Y-%m-%d"), round(float(v), 3)] for d, v in s.items()]


def memory_rule(ind):
    return ~((ind.C > 0) & (ind.M < 0))


def theme_block(t):
    comps = components(t)
    tickers = [b["ticker"] for b in t["basket"]]
    b = basket_returns(tickers)
    bench = PX[t["benchmark"]].pct_change(fill_method=None)
    lvl = (1 + b.fillna(0)).cumprod()
    blvl = (1 + bench.fillna(0)).cumprod()
    out = {k: t.get(k) for k in ("slug", "name", "universe", "layer", "thesis", "watch", "invalidate", "benchmark")}
    out["components"] = []
    out["basket"] = []
    for m in t["basket"]:
        row = dict(m, **price_stats(m["ticker"]))
        code = m["ticker"].split(".")[0]
        if m["ticker"].endswith((".TW", ".TWO")) and code in TW_W:
            y = yoy3_sum(TW_W, [code])
            row["rev_yoy3"] = round(float(y.iloc[-1]) * 100, 1) if y is not None and len(y) else None
        out["basket"].append(row)
    # basket vs benchmark, weekly for the chart
    wk = pd.DataFrame({"basket": lvl, "bench": blvl}).resample("W-FRI").last()
    wk = wk[wk.index >= "2016-01-01"]
    wk = wk / wk.iloc[0] * 100
    out["perf_series"] = [[d.strftime("%Y-%m-%d"), round(float(r.basket), 2), round(float(r.bench), 2)]
                          for d, r in wk.dropna().iterrows()]
    out["basket_stats"] = {"r1m": pct(float(lvl.iloc[-1]), float(lvl.asof(lvl.index[-1] - pd.Timedelta(days=30)))),
                           "r3m": pct(float(lvl.iloc[-1]), float(lvl.asof(lvl.index[-1] - pd.Timedelta(days=91)))),
                           "r1y": pct(float(lvl.iloc[-1]), float(lvl.asof(lvl.index[-1] - pd.Timedelta(days=365)))),
                           "bench_r3m": pct(float(blvl.iloc[-1]), float(blvl.asof(blvl.index[-1] - pd.Timedelta(days=91))))}
    if not comps:
        out.update(status="monitoring", reason="No free physical data series yet; basket performance only.",
                   call=None, C=None, M=None, index_series=[], test=None)
        return out
    ind = build_index(comps)
    for k, (y, lag, meta) in comps.items():
        z = ind["z_" + k].dropna()
        out["components"].append(dict(meta, key=k, latest_month=str(y.index[-1].date()),
                                      latest_yoy=round(float(y.iloc[-1]) * 100, 1),
                                      latest_z=round(float(z.iloc[-1]), 2) if len(z) else None,
                                      series=monthly_series(y * 100)))
    live = ind[ind.decision <= TODAY]
    if live.empty:
        out.update(status="monitoring", reason="Not enough history yet.", call=None, C=None, M=None,
                   index_series=[], test=None)
        return out
    r = live.iloc[-1]
    out.update(C=round(float(r.C), 2), M=round(float(r.M), 2), data_month=str(r.name.date()),
               decision_date=str(r.decision.date()),
               index_series=[[d.strftime("%Y-%m-%d"), round(float(c), 3), round(float(m), 3) if pd.notna(m) else None]
                             for d, c, m in zip(ind.decision, ind.C, ind.M) if d >= pd.Timestamp("2010-01-01")])
    has_tw = "tw_revenue" in comps
    if t["slug"] == "memory":
        rule = memory_rule(live)
        state = "IN" if bool(rule.iloc[-1]) else "OUT"
        out.update(status="validated", call=state, rule="Rule M: stay in unless tight AND slowing (C>0, M<0)",
                   test={"kind": "timing", "note": "Out-of-sample 2008–2017 PASS (v2/v4): CAGR 15.9% vs 13.0%, "
                                                   "Sharpe 0.71 vs 0.60."})
        hist = pd.Series(rule.values, index=live.decision)
    else:
        oos = tilt_test(ind, b, bench, *OOS) if has_tw else None
        recent = tilt_test(ind, b, bench, pd.Timestamp("2016-01-01"), TODAY)
        test = {"kind": "tilt", "oos": oos, "since_2016": recent,
                "window": "OOS 2008–2017" if oos else "No out-of-sample window (trade data starts 2013)"}
        validated = bool(oos and oos["pass"])
        state = ("OVERWEIGHT" if r.C > 0 else "UNDERWEIGHT") if validated else None
        out.update(status="validated" if validated else "monitoring", call=state, test=test,
                   rule="Tilt: overweight vs benchmark when C>0, underweight otherwise",
                   reason=None if validated else ("Tilt failed its out-of-sample test." if oos else
                                                  "No out-of-sample window to validate on yet."))
        hist = pd.Series((live.C > 0).values, index=live.decision)
    # call changes over time (for the theme page timeline), last 10
    ch = hist[hist != hist.shift(1)]
    labels = ({True: "IN", False: "OUT"} if t["slug"] == "memory" else {True: "OVERWEIGHT", False: "UNDERWEIGHT"})
    out["call_history"] = [{"date": str(d.date()), "state": labels[bool(v)]} for d, v in ch.items()][-10:]
    return out


KR_DESC = {"8542": "Integrated circuits (all)", "854232": "Memory chips", "850423": "Large power transformers (>10 MVA)",
           "850760": "Lithium-ion batteries"}


def discovery():
    f = pd.read_csv("data/processed/discovery_tw.csv", dtype={"code": str}, parse_dates=["month", "event_date"])
    theme_of = {}
    for t in THEMES:
        for s in t.get("tw_signals", []):
            theme_of.setdefault(s["code"], []).append(t["slug"])
        for m in t["basket"]:
            if m["ticker"].endswith((".TW", ".TWO")):
                theme_of.setdefault(m["ticker"].split(".")[0], []).append(t["slug"])
    recent = f[f.month >= f.month.max() - pd.DateOffset(months=5)].copy()
    items = []
    for r in recent.sort_values(["month", "z"], ascending=[False, False]).itertuples():
        items.append({"kind": "company", "month": str(r.month.date()), "published": str(r.event_date.date()),
                      "code": r.code, "ticker": r.ticker, "name": NAMES_EN.get(r.code) or r.name, "name_local": r.name,
                      "industry": r.industry, "yoy3": round(r.g * 100, 1), "z": round(r.z, 2),
                      "accel_pp": round(r.a * 100, 1), "rev3_twd_m": round(r.r3 / 1000, 1),
                      "themes": sorted(set(theme_of.get(r.code, []))), **price_stats(r.ticker)})
    hs_theme = {}
    for t in THEMES:
        for c in t.get("us_import_hs6", []):
            hs_theme.setdefault(("us_imports", c["hs"]), []).append(t["slug"])
        for c in t.get("kr_export_hs", []):
            hs_theme.setdefault(("kr_exports", c["hs"]), []).append(t["slug"])
    if os.path.exists("data/processed/discovery_hs.csv") and os.path.getsize("data/processed/discovery_hs.csv") > 10:
        h = pd.read_csv("data/processed/discovery_hs.csv", dtype={"hs": str}, parse_dates=["month", "event_date"])
        h = h[h.month >= h.month.max() - pd.DateOffset(months=5)]
        for r in h.sort_values(["month", "z"], ascending=[False, False]).itertuples():
            items.append({"kind": "trade", "source": r.source, "month": str(r.month.date()),
                          "published": str(r.event_date.date()), "hs": r.hs,
                          "desc": (KR_DESC.get(r.hs) if r.source == "kr_exports" else None) or
                                  ((US_DESC.get(r.hs) or r.desc or "").title() if isinstance(r.desc, str) or r.hs in US_DESC else ""),
                          "yoy3": round(r.g * 100, 1), "z": round(r.z, 2), "accel_pp": round(r.a * 100, 1),
                          "value3_usd_m": round(r.r3 / 1e6, 1), "themes": hs_theme.get((r.source, r.hs), [])})
    ev = json.load(open("backtest/v5_discovery_results.json"))
    return {"items": items, "event_study": ev, "latest_month": str(f.month.max().date())}


def track_record():
    entries = [json.loads(l) for l in open("backtest/signal_log.jsonl") if l.strip()]
    ok, prev = True, "0" * 64
    for e in entries:
        body = {k: v for k, v in e.items() if k != "hash"}
        h = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        ok = ok and h == e["hash"] and e["prev"] == prev
        prev = e["hash"]
    rows, no_call = [], 0
    for e in entries:
        for c in e["calls"]:
            if c["state"] == "NO CALL":
                no_call += 1
                continue
            bp = c.get("basket_prices", {})
            bench = c.get("benchmark")
            rets = [pct(float(PX[k].dropna().iloc[-1]), v) for k, v in bp.items()
                    if k != bench and k in PX and not PX[k].dropna().empty]
            rets = [x for x in rets if x is not None]
            basket_ret = round(float(np.mean(rets)), 1) if rets else None
            bench_ret = pct(float(PX[bench].dropna().iloc[-1]), bp[bench]) if bench and bench in bp and bench in PX else None
            rows.append({"logged_at": e["logged_at"], "hash": e["hash"], "bottleneck": c["bottleneck"],
                         "model": c["model"], "state": c["state"], "C": c["C"], "M": c["M"],
                         "basket_since_pct": basket_ret, "bench": bench, "bench_since_pct": bench_ret})
    return {"chain_ok": ok, "entries": len(entries), "head": entries[-1]["hash"] if entries else None,
            "rows": rows[::-1], "no_call": no_call, "repo": "https://github.com/NiklavsD/bottleneck-signals"}


def context():
    """Press/analyst context (engine/context.py) and memory spot prices. Context only: no call reads this."""
    out = {"themes": {}, "latest": [], "all": [], "emerging": [], "since": None, "spot": None, "sources": []}
    path = "data/context/items.jsonl"
    if os.path.exists(path):
        items = [json.loads(l) for l in open(path) if l.strip()]
        seen, uniq = set(), []
        for i in sorted(items, key=lambda i: (i["published"], i["tagged_at"]), reverse=True):
            key = re.sub(r"\W+", " ", i["title"].lower()).strip()[:80]
            if key not in seen:  # the same story syndicated by several outlets counts once
                seen.add(key)
                uniq.append(i)
        items = uniq
        out["since"] = min(i["tagged_at"] for i in items)[:10] if items else None
        out["latest"] = [i for i in items if i["signal"] != "neutral"][:24]
        cut60 = str((TODAY - pd.Timedelta(days=60)).date())
        out["all"] = [i for i in items if i["published"] >= cut60]
        today = pd.Timestamp(TODAY.date())
        weeks = pd.date_range(end=today, periods=26, freq="W-MON")
        for slug in [t["slug"] for t in THEMES]:
            mine = [i for i in items if slug in i["themes"]]
            d = pd.to_datetime(pd.Series([i["published"] for i in mine], dtype="object"))
            wk = d.dt.to_period("W-SUN").dt.start_time.value_counts() if len(d) else pd.Series(dtype=int)
            recent = [i for i in mine if i["published"] >= str((today - pd.Timedelta(days=28)).date())]
            tight = sum(i["signal"] == "tightening" for i in recent)
            ease = sum(i["signal"] == "easing" for i in recent)
            out["themes"][slug] = {"items": mine[:10], "n_28d": len(recent), "tight_28d": tight, "ease_28d": ease,
                                   "tone": round((tight - ease) / len(recent), 2) if recent else None,
                                   "weekly": [[str(w.date()), int(wk.get(w, 0))] for w in weeks]}
        cut = str((today - pd.Timedelta(days=60)).date())
        terms = {}
        known = {t["name"].lower() for t in THEMES}
        for i in items:
            if i["published"] < cut:
                continue
            for term in i.get("new_terms", []):
                k = term.strip().lower()
                if len(k) < 3 or k in known:
                    continue
                e = terms.setdefault(k, {"term": term.strip(), "n": 0, "example": i["url"], "example_title": i["title"]})
                e["n"] += 1
        out["emerging"] = sorted([e for e in terms.values() if e["n"] >= 2], key=lambda e: -e["n"])[:15]
    src = json.load(open("config/context_sources.json"))
    out["sources"] = [{"name": f["name"], "kind": f["kind"]} for f in src["feeds"]]
    if os.path.exists("data/context/spot_prices.csv"):
        sp = pd.read_csv("data/context/spot_prices.csv")
        last = sp[sp.date == sp.date.max()]
        first = sp.sort_values("date").drop_duplicates("item")[["item", "date", "avg_usd"]].set_index("item")
        rows = []
        for r in last.itertuples():
            f0 = first.loc[r.item]
            rows.append({"category": r.category, "item": r.item, "avg_usd": r.avg_usd, "change_pct": r.change_pct,
                         "since": f0.date, "since_pct": round((r.avg_usd / f0.avg_usd - 1) * 100, 1) if f0.date != r.date else None})
        out["spot"] = {"asof": str(sp.date.max()), "rows": rows, "history": None}
        if os.path.exists("data/context/spot_history.csv"):
            h = pd.concat([pd.read_csv("data/context/spot_history.csv"), sp[["date", "category", "item", "avg_usd"]]])
            h["date"] = pd.to_datetime(h.date)
            h["item"] = h["item"].str.replace(r"\s+", " ", regex=True).str.strip()
            h = h[h.category == "dram_chip"].sort_values("date")
            # like-for-like chained index (as in PREREGISTRATION_v7 B): mean log change of items present at both dates
            piv = h.pivot_table(index="date", columns="item", values="avg_usd", aggfunc="last")
            lr = np.log(piv).diff().mean(axis=1, skipna=True).fillna(0)
            idx = np.exp(lr.cumsum()) * 100
            m = idx.resample("ME").last().dropna()
            out["spot"]["history"] = [[str(d.date()), round(float(v), 2)] for d, v in m.items()]
    return out


def main():
    os.makedirs("web/data", exist_ok=True)
    themes = [theme_block(t) for t in THEMES]
    snap = {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "prices_asof": str(PX.index.max().date()), "themes": themes, "discovery": discovery(), "context": context(),
            "track": track_record(),
            "universes": [{"slug": "ai", "name": "AI compute", "tagline": "Wafers, memory, packaging, optics and the data-centre stack."},
                          {"slug": "power", "name": "Power", "tagline": "Turbines, transformers, grid and delivered megawatts."},
                          {"slug": "robotics", "name": "Robotics", "tagline": "Actuators, sensing and automation."}]}
    tmp = OUT + ".tmp"
    json.dump(snap, open(tmp, "w"), default=lambda o: None if isinstance(o, float) and not np.isfinite(o) else str(o))
    os.replace(tmp, OUT)
    v = sum(t["status"] == "validated" for t in themes)
    print(f"snapshot: {len(themes)} themes ({v} validated), {len(snap['discovery']['items'])} discovery items, "
          f"track entries {snap['track']['entries']} chain_ok={snap['track']['chain_ok']}")
    for t in themes:
        print(f"  {t['slug']:30s} {t['status']:10s} call={t['call']} C={t['C']} M={t['M']}")


if __name__ == "__main__":
    main()
