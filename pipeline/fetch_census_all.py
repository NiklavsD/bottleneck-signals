"""US monthly import value for every HS6 code in chapters 28 (rare-earth chemicals), 84, 85 and 90, since 2013.

Feeds the HS-level discovery scan. Month files are cached in data/raw/census_hs6/; the latest 3 months are
re-fetched on every run because Census revises recent data. A failed re-fetch keeps the cached month, a month
that was in the previous output is never dropped, and the run exits non-zero if coverage still shrank.
"""
import datetime as dt
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = "data/raw/census_hs6"
OUT = "data/processed/us_imports_all.csv"
CH = ("28", "84", "85", "90")
COLS = ["month", "hs", "desc", "usd"]


def api_key():
    return dict(l.strip().split("=", 1) for l in open(".env") if "=" in l and not l.startswith("#"))["CENSUS_API_KEY"]


def month_list(today):
    return [f"{y}-{m:02d}" for y in range(2013, today.year + 1) for m in range(1, 13) if (y, m) < (today.year, today.month)]


def get(ym, key, recent, raw=RAW, retries=4, sleep=None):
    """Rows for one month: cache for settled months; re-fetch recent ones, falling back to the cache on failure."""
    p = f"{raw}/{ym}.json"
    cached = json.load(open(p)) if os.path.exists(p) else None
    if cached is not None and ym not in recent:
        return cached
    url = ("https://api.census.gov/data/timeseries/intltrade/imports/hs?get=I_COMMODITY,I_COMMODITY_SDESC,GEN_VAL_MO"
           f"&COMM_LVL=HS6&time={ym}&key={key}")
    for a in range(retries):
        try:
            r = requests.get(url, timeout=180)
            if r.status_code == 204:
                return cached or []  # not yet published
            r.raise_for_status()
            d = r.json()
            rows = [x for x in d[1:] if x[0][:2] in CH]
            if not rows and cached:
                return cached  # an empty answer must not wipe a month we already have
            json.dump(rows, open(p, "w"))
            return rows
        except Exception:
            (sleep or time.sleep)(5 * (a + 1))
    if cached is not None:
        print(f"WARNING: {ym} re-fetch failed, keeping cached data", flush=True)
        return cached
    return []


def merge_previous(df, prev):
    """Carry over any month present in the previous output but missing now. Returns (df, carried months)."""
    if prev is None or prev.empty:
        return df, []
    lost = sorted(set(prev.month) - set(df.month))
    if lost:
        df = pd.concat([df, prev[prev.month.isin(lost)]], ignore_index=True).sort_values(["month", "hs"], kind="stable")
    return df, lost


def main(today=None, raw=RAW, out=OUT):
    os.makedirs(raw, exist_ok=True)
    key = api_key()
    months = month_list(today or dt.date.today())
    recent = set(months[-3:])
    prev = pd.read_csv(out, dtype={"hs": str}) if os.path.exists(out) else None
    with ThreadPoolExecutor(3) as ex:
        res = list(ex.map(lambda ym: (ym, get(ym, key, recent, raw)), months))
    rows = [(ym + "-01", r[0], r[1], float(r[2])) for ym, rs in res for r in rs]
    df = pd.DataFrame(rows, columns=COLS)
    df, lost = merge_previous(df, prev)
    if lost:
        print(f"WARNING: kept {len(lost)} month(s) from the previous file that came back empty: {', '.join(lost)}")
    df.to_csv(out, index=False)
    print(df.shape, df.month.min(), df.month.max(), df.hs.nunique(), "codes")
    if prev is not None and df.month.nunique() < prev.month.nunique():
        print(f"ERROR: coverage fell from {prev.month.nunique()} to {df.month.nunique()} months", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(main())
