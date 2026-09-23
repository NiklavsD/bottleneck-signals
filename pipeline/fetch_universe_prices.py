"""Daily closes for every basket ticker and benchmark in config/themes.json (Yahoo Finance, not redistributed)."""
import json
import os

import pandas as pd
import yfinance as yf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
themes = json.load(open("config/themes.json"))
tickers = sorted({b["ticker"] for t in themes for b in t["basket"]} | {t["benchmark"] for t in themes}
                 | {"SPY", "QQQ", "SOXX", "XLI", "XLU", "^TWII"})
frames = []
for i in range(0, len(tickers), 100):
    frames.append(yf.download(tickers[i:i + 100], start="2004-01-01", auto_adjust=True, progress=False,
                              threads=True)["Close"])
px = pd.concat(frames, axis=1, sort=True)
px = px.loc[:, ~px.columns.duplicated()]
# Same bad-tick guard as the v5 event study: a close far from its 252-day centred median is treated as missing.
med = px.rolling(252, center=True, min_periods=20).median()
px = px.mask((px < 0.2 * med) | (px > 5 * med))
px.to_csv("data/processed/universe_prices.csv.gz")
print(px.shape, "tickers with data:", int(px.notna().any().sum()), "last", px.index.max().date())
