"""US monthly import value for every HS6 code in chapters 28 (rare-earth chemicals), 84, 85 and 90, since 2013.

Feeds the HS-level discovery scan. Month files are cached in data/raw/census_hs6/; the latest 3 months are
re-fetched on every run because Census revises recent data.
"""
import datetime as dt, json, os, time, requests, pandas as pd
from concurrent.futures import ThreadPoolExecutor
KEY = dict(l.strip().split("=", 1) for l in open(".env") if "=" in l and not l.startswith("#"))["CENSUS_API_KEY"]
RAW = "data/raw/census_hs6"; os.makedirs(RAW, exist_ok=True)
CH = ("28", "84", "85", "90")
today = dt.date.today()
months = [f"{y}-{m:02d}" for y in range(2013, today.year + 1) for m in range(1, 13) if (y, m) < (today.year, today.month)]
recent = set(months[-3:])

def get(ym):
    p = f"{RAW}/{ym}.json"
    if os.path.exists(p) and ym not in recent:
        return json.load(open(p))
    url = ("https://api.census.gov/data/timeseries/intltrade/imports/hs?get=I_COMMODITY,I_COMMODITY_SDESC,GEN_VAL_MO"
           f"&COMM_LVL=HS6&time={ym}&key={KEY}")
    for a in range(4):
        try:
            r = requests.get(url, timeout=180)
            if r.status_code == 204:
                return []  # not yet published
            d = r.json()
            rows = [x for x in d[1:] if x[0][:2] in CH]
            json.dump(rows, open(p, "w"))
            return rows
        except Exception:
            time.sleep(5 * (a + 1))
    return []

with ThreadPoolExecutor(3) as ex:
    res = list(ex.map(lambda ym: (ym, get(ym)), months))
out = [(ym + "-01", r[0], r[1], float(r[2])) for ym, rows in res for r in rows]
df = pd.DataFrame(out, columns=["month", "hs", "desc", "usd"])
df.to_csv("data/processed/us_imports_all.csv", index=False)
print(df.shape, df.month.min(), df.month.max(), df.hs.nunique(), "codes")
