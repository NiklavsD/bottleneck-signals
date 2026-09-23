"""English short names for Taiwan companies: TWSE open API for listed (sii), Yahoo for OTC. Cached."""
import os, json, requests, pandas as pd, yfinance as yf
from concurrent.futures import ThreadPoolExecutor
OUT = "data/processed/tw_names_en.csv"
old = pd.read_csv(OUT, dtype={"code": str}).set_index("code")["name_en"].to_dict() if os.path.exists(OUT) else {}
names = dict(old)
for r in requests.get("https://openapi.twse.com.tw/v1/opendata/t187ap03_L", timeout=60).json():
    if r.get("英文簡稱"):
        names[r["公司代號"].strip()] = r["英文簡稱"].strip()
tw = pd.read_csv("data/processed/tw_revenue.csv", dtype={"code": str})
latest = tw[tw.month == tw.month.max()]
todo = [(c, m) for c, m in zip(latest.code, latest.market) if c not in names]
def yname(cm):
    c, m = cm
    try:
        i = yf.Ticker(c + (".TWO" if m == "otc" else ".TW")).info
        return c, (i.get("shortName") or i.get("longName") or "").strip()
    except Exception:
        return c, ""
with ThreadPoolExecutor(8) as ex:
    for c, n in ex.map(yname, todo):
        if n:
            names[c] = n
pd.DataFrame(sorted(names.items()), columns=["code", "name_en"]).to_csv(OUT, index=False)
print(len(names), "names;", len(todo), "looked up on Yahoo")
