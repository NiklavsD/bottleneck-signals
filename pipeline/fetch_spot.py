"""Record today's memory spot prices from the public DRAMeXchange (TrendForce) homepage.

Only the free spot tables are read (DRAM chips, modules, NAND flash, GDDR, NAND wafers).
The history exists only from the day this job started running (2026-09-23): there is no
free back-history, so these prices are shown as context and are not used by any tested rule.
Appends to data/context/spot_prices.csv, one row per (date, item); re-running the same day overwrites that day.
"""
import datetime as dt
import os
import re

import pandas as pd
import requests
from bs4 import BeautifulSoup

URL = "https://www.dramexchange.com/"
OUT = "data/context/spot_prices.csv"
UA = "Mozilla/5.0 (compatible; LeadtimeBot/0.1; +https://github.com/NiklavsD/bottleneck-signals)"


def category(item):
    i = item.upper()
    if "DIMM" in i:
        return "dram_module"
    if "GDDR" in i:
        return "gddr"
    if "DDR" in i:
        return "dram_chip"
    if "TLC" in i or "QLC" in i:
        return "nand_wafer"
    if "SLC" in i or "MLC" in i:
        return "nand_chip"
    return None  # memory cards etc.: not tracked


def num(s):
    s = s.replace(",", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse(html, fallback_date=None):
    """Return (asof_date, rows) from a DRAMeXchange homepage HTML."""
    soup = BeautifulSoup(html, "lxml")
    stamp = re.search(r"Last Update:\s*([A-Za-z]{3})\.?\s*(\d{1,2})\s+(\d{4})", html)
    asof = dt.datetime.strptime(" ".join(stamp.groups()), "%b %d %Y").date() if stamp else (fallback_date or dt.date.today())
    rows = []
    for t in soup.find_all("table"):
        trs = [[c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])] for tr in t.find_all("tr")]
        trs = [x for x in trs if any(x)]
        if not trs or trs[0][:1] != ["Item"] or "Session Average" not in trs[0]:
            continue
        ia, ic = trs[0].index("Session Average"), len(trs[0]) - 2  # change column precedes "History"
        for x in trs[1:]:
            item = re.sub(r"\s+", " ", x[0])
            cat = category(item)
            if cat and len(x) > ia and num(x[ia]) is not None:
                rows.append({"date": str(asof), "category": cat, "item": item, "avg_usd": num(x[ia]),
                             "change_pct": num(x[ic]) if len(x) > ic else None})
    return asof, rows


def main():
    r = requests.get(URL, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    asof, rows = parse(r.text)
    if not rows:
        raise SystemExit("no spot rows parsed; page layout changed?")
    new = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    if os.path.exists(OUT):
        old = pd.read_csv(OUT)
        new = pd.concat([old[old.date != str(asof)], new])
    new.sort_values(["date", "category", "item"]).to_csv(OUT, index=False)
    print(f"spot prices {asof}: {len(rows)} items, history {new.date.nunique()} days")


if __name__ == "__main__":
    main()
