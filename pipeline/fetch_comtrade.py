"""Korea monthly exports of ICs (HS 8542) and memory ICs (HS 854232) from the UN Comtrade public preview API.

The same numbers are announced domestically by MOTIE on the 1st of M+1, so a decision on the 16th of M+1 is point-in-time safe.
"""
import time, requests, pandas as pd
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

periods = [f"{y}{m:02d}" for y in range(2010, 2027) for m in range(1, 13) if f"{y}{m:02d}" <= "202608"]
with ThreadPoolExecutor(3) as ex:
    rows = [r for rs in ex.map(one, periods) for r in rs]
df = pd.DataFrame(rows, columns=["period", "hs", "usd"])
df["month"] = pd.to_datetime(df.period, format="%Y%m")
df.pivot_table(index="month", columns="hs", values="usd").to_csv("data/processed/korea_chip_exports.csv")
print(len(df), "rows", df.month.min(), df.month.max())
