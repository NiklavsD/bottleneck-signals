"""US monthly import value by HS6 code (Census intltrade API, 2013+).

Released about 35 days after month end, so month M is used from the 16th of M+2.
"""
import os, time, requests, pandas as pd

KEY = dict(l.strip().split("=", 1) for l in open(".env") if l.strip() and not l.startswith("#"))["CENSUS_API_KEY"]
CODES = {
    "850423": "Liquid-dielectric transformers >10 MVA (large power transformers)",
    "850422": "Liquid-dielectric transformers 650 kVA-10 MVA",
    "850421": "Liquid-dielectric transformers <=650 kVA (distribution)",
    "853720": "Switchgear / control boards >1000 V",
    "850440": "Static converters (UPS, rectifiers, inverters)",
}

rows = []
for code in CODES:
    url = ("https://api.census.gov/data/timeseries/intltrade/imports/hs"
           f"?get=GEN_VAL_MO&I_COMMODITY={code}&time=from+2013-01&key={KEY}")
    for a in range(4):
        try:
            j = requests.get(url, timeout=120).json()
            break
        except Exception:
            time.sleep(5 * (a + 1))
    for val, _, t in j[1:]:
        rows.append((t, code, float(val)))
df = pd.DataFrame(rows, columns=["month", "hs", "usd"])
df["month"] = pd.to_datetime(df.month)
out = df.pivot_table(index="month", columns="hs", values="usd")
out.to_csv("data/processed/us_imports.csv")
print(out.shape, out.index.min().date(), out.index.max().date())
print(out.resample("YE").sum().div(1e9).round(2).to_string())
