"""Discovery scanner (backtest/PREREGISTRATION_v5_discovery.md).

Flags Taiwan-listed companies whose monthly revenue unusually accelerates, and HS codes whose trade value does.
Outputs:
  data/processed/tw_industry.csv      code -> industry (English), market
  data/processed/discovery_tw.csv     every company flag (point-in-time), with event date
  data/processed/discovery_hs.csv     every HS-code flag (US imports, Korea exports)
"""
import glob
import os
import re

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

INDUSTRY_EN = {
    "電子零組件業": "Electronic components", "半導體業": "Semiconductors", "生技醫療業": "Biotech & medical",
    "光電業": "Optoelectronics", "電腦及週邊設備業": "Computers & peripherals", "電機機械": "Electrical machinery",
    "其他電子業": "Other electronics", "通信網路業": "Communications & networking", "建材營造": "Construction",
    "其他": "Other", "紡織纖維": "Textiles", "觀光餐旅": "Tourism", "鋼鐵工業": "Steel", "資訊服務業": "IT services",
    "綠能環保": "Green energy", "化學工業": "Chemicals", "電子通路業": "Electronics distribution",
    "數位雲端": "Digital cloud", "汽車工業": "Automotive", "航運業": "Shipping", "食品工業": "Food",
    "居家生活": "Home & living", "文化創意業": "Cultural & creative", "塑膠工業": "Plastics", "運動休閒": "Sports & leisure",
    "電器電纜": "Electric wire & cable", "油電燃氣業": "Oil, power & gas", "貿易百貨": "Trading & retail",
    "橡膠工業": "Rubber", "金融業": "Finance", "水泥工業": "Cement", "造紙工業": "Paper", "玻璃陶瓷": "Glass & ceramics",
    "農業科技": "Agritech",
}
FINANCE_PREFIX = "金融保險業"

# Pre-registered thresholds (do not tune)
Z_MIN, G_MIN, R3_MIN_TWD_K, COOLDOWN_M = 2.0, 0.30, 300_000, 6
HS_R3_MIN_USD = 50e6


def industries():
    """Latest industry per company from the most recent cached MOPS pages."""
    rows = {}
    for market in ("sii", "otc"):
        for f in sorted(glob.glob(f"data/raw/tw/{market}_*.html"))[-24:]:  # newest wins
            h = open(f, encoding="utf-8").read()
            for chunk in h.split("產業別：")[1:]:
                zh = re.split(r"單位|<", chunk, 1)[0].strip()
                en = "Finance & insurance" if zh.startswith(FINANCE_PREFIX) else INDUSTRY_EN.get(zh, zh)
                for code in re.findall(r"<td[^>]*>\s*(\d{4,6})\s*</td>", chunk):
                    rows[code] = (en, market)
    df = pd.DataFrame([(c, i, m) for c, (i, m) in rows.items()], columns=["code", "industry", "market"])
    df.to_csv("data/processed/tw_industry.csv", index=False)
    return df


def flag_panel(values, min_level):
    """values: DataFrame month x id (monthly level). Returns long DataFrame of flags with g, z, a."""
    r3 = values.rolling(3, min_periods=3).sum()
    g = r3 / r3.shift(12) - 1
    g = g.where(r3.shift(12) > 0)
    mu = g.expanding(24).mean()
    sd = g.expanding(24).std()
    z = (g - mu) / sd
    a = g - g.shift(3)
    cond = (z >= Z_MIN) & (g >= G_MIN) & (a > 0) & (r3 >= min_level)
    out = []
    for col in values.columns:
        last = None
        for t in cond.index[cond[col].fillna(False)]:
            if last is not None and (t.year - last.year) * 12 + t.month - last.month < COOLDOWN_M:
                continue
            last = t
            out.append((t, col, float(g.at[t, col]), float(z.at[t, col]), float(a.at[t, col]), float(r3.at[t, col])))
    return pd.DataFrame(out, columns=["month", "id", "g", "z", "a", "r3"]), g, z


def scan_tw():
    tw = pd.read_csv("data/processed/tw_revenue.csv", dtype={"code": str}, parse_dates=["month"])
    names = tw.sort_values("month").groupby("code")[["name", "market"]].last()
    w = tw.pivot_table(index="month", columns="code", values="revenue_k_twd")
    flags, g, z = flag_panel(w, R3_MIN_TWD_K)
    flags = flags.rename(columns={"id": "code"})
    flags["event_date"] = flags.month + pd.offsets.MonthBegin(1) + pd.Timedelta(days=10)
    flags = flags.join(names, on="code")
    ind = industries().set_index("code")["industry"]
    flags["industry"] = flags.code.map(ind)
    flags["ticker"] = flags.code + np.where(flags.market == "otc", ".TWO", ".TW")
    flags.to_csv("data/processed/discovery_tw.csv", index=False)
    latest = pd.DataFrame({"g": g.iloc[-1], "z": z.iloc[-1]}).dropna()
    latest.to_csv("data/processed/tw_latest_growth.csv")
    return flags


def scan_hs():
    out = []
    if os.path.exists("data/processed/us_imports_all.csv"):
        us = pd.read_csv("data/processed/us_imports_all.csv", dtype={"hs": str}, parse_dates=["month"])
        w = us.pivot_table(index="month", columns="hs", values="usd")
        f, _, _ = flag_panel(w, HS_R3_MIN_USD)
        f["source"] = "us_imports"
        f["event_date"] = f.month + pd.offsets.MonthBegin(2) + pd.Timedelta(days=15)
        desc = us.drop_duplicates("hs").set_index("hs")["desc"]
        f["desc"] = f.id.map(desc)
        out.append(f)
    if os.path.exists("data/processed/korea_customs_exports.csv"):
        kr = pd.read_csv("data/processed/korea_customs_exports.csv", dtype={"hs": str}, parse_dates=["month"])
        w = kr.pivot_table(index="month", columns="hs", values="usd")
        f, _, _ = flag_panel(w, HS_R3_MIN_USD)
        f["source"] = "kr_exports"
        f["event_date"] = f.month + pd.offsets.MonthBegin(1) + pd.Timedelta(days=15)
        f["desc"] = None
        out.append(f)
    df = pd.concat(out, ignore_index=True) if out else pd.DataFrame()
    df = df.rename(columns={"id": "hs"})
    df.to_csv("data/processed/discovery_hs.csv", index=False)
    return df


if __name__ == "__main__":
    f = scan_tw()
    print("TW flags:", len(f), "companies:", f.code.nunique(), "latest month flags:",
          (f.month == f.month.max()).sum(), f.month.max().date())
    h = scan_hs()
    print("HS flags:", len(h))
