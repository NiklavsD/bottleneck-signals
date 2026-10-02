"""Korea monthly exports of ICs (HS 8542) and memory ICs (HS 854232) from the UN Comtrade public preview API.

The same numbers are announced domestically by MOTIE on the 1st of M+1, so a decision on the 16th of M+1 is point-in-time safe.
"""
import datetime as dt, os, time, requests, pandas as pd
from concurrent.futures import ThreadPoolExecutor

def one(period):
    url = ("https://comtradeapi.un.org/public/v1/preview/C/M/HS?reporterCode=410"
           f"&period={period}&cmdCode=8542,854232&flowCode=X&partnerCode=0")
    for a in range(5):
        try:
            d = requests.get(url, timeout=60).json()
            if "data" in d:
                return [(period, r["cmdCode"], r["primaryValue"]) for r in d["data"]]
        except Exception:
            pass
        time.sleep(4 * (a + 1))
    return []

# up to the previous calendar month (the latest one that can have been published)
END = (dt.date.today().replace(day=1) - dt.timedelta(days=1)).strftime("%Y%m")
periods = [f"{y}{m:02d}" for y in range(2010, int(END[:4]) + 1) for m in range(1, 13) if f"{y}{m:02d}" <= END]
with ThreadPoolExecutor(3) as ex:
    rows = [r for rs in ex.map(one, periods) for r in rs]
df = pd.DataFrame(rows, columns=["period", "hs", "usd"])
df["month"] = pd.to_datetime(df.period, format="%Y%m")
OUT = "data/processed/korea_chip_exports.csv"
wide = df.pivot_table(index="month", columns="hs", values="usd")
if os.path.exists(OUT):  # a period that failed to download keeps its previous values instead of vanishing
    prev = pd.read_csv(OUT, index_col=0, parse_dates=True)
    prev.columns = prev.columns.astype(str)
    wide.columns = wide.columns.astype(str)
    lost = prev.index.difference(wide.index)
    if len(lost):
        print("WARNING: kept previous values for", len(lost), "month(s) that failed:", ", ".join(str(d.date()) for d in lost))
    wide = wide.combine_first(prev)
wide.to_csv(OUT)
print(len(df), "rows", df.month.min(), df.month.max())
