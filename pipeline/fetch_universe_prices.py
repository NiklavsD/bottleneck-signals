"""Daily closes for every basket ticker and benchmark in config/themes.json (Yahoo Finance, not redistributed).

A ticker whose new history comes back empty or short keeps its previous column (pipeline/price_merge.py);
the run exits non-zero, after writing the merged file, if more than 3 tickers had to be carried over.
"""
import json
import os
import sys

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from price_merge import merge_prices, read_previous, report  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = "data/processed/universe_prices.csv.gz"


def universe_tickers():
    themes = json.load(open("config/themes.json"))
    return sorted({b["ticker"] for t in themes for b in t["basket"]} | {t["benchmark"] for t in themes}
                  | {"SPY", "QQQ", "SOXX", "XLI", "XLU", "^TWII"})


def download(tickers):
    frames = []
    for i in range(0, len(tickers), 100):
        frames.append(yf.download(tickers[i:i + 100], start="2004-01-01", auto_adjust=True, progress=False,
                                  threads=True)["Close"])
    px = pd.concat(frames, axis=1, sort=True)
    px = px.loc[:, ~px.columns.duplicated()]
    # Same bad-tick guard as the v5 event study: a close far from its 252-day centred median is treated as missing.
    med = px.rolling(252, center=True, min_periods=20).median()
    return px.mask((px < 0.2 * med) | (px > 5 * med))


def main(out=OUT):
    tickers = universe_tickers()
    px, carried, missing = merge_prices(download(tickers), read_previous(out), tickers)
    px.to_csv(out)
    print(px.shape, "tickers with data:", int(px.notna().any().sum()), "last", px.index.max().date())
    return 1 if report(carried, missing) else 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(main())
