"""Daily closes for every Taiwan company that has ever been flagged by the discovery scanner, plus TAIEX."""
import pandas as pd, yfinance as yf
f = pd.read_csv("data/processed/discovery_tw.csv", dtype={"code": str})
tickers = sorted(set(f.ticker)) + ["^TWII"]
frames = []
for i in range(0, len(tickers), 200):
    d = yf.download(tickers[i:i + 200], start="2007-06-01", auto_adjust=True, progress=False, threads=True)["Close"]
    frames.append(d)
px = pd.concat(frames, axis=1)
px = px.loc[:, ~px.columns.duplicated()]
px.to_parquet("data/processed/tw_prices.parquet") if False else px.to_csv("data/processed/tw_prices.csv.gz")
print(px.shape, px.notna().any().sum(), "with data")
