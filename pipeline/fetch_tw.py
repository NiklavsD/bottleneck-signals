"""Taiwan monthly revenue (MOPS) for every TWSE (sii) and TPEx (otc) listed company.

Companies must publish month M revenue by the 10th of M+1, so the series is
point-in-time usable from that date. Raw pages are cached under data/raw/tw/.
"""
import os, sys, time, datetime as dt, requests, pandas as pd
from bs4 import BeautifulSoup

RAW = "data/raw/tw"
OUT = "data/processed/tw_revenue.csv"
UA = {"User-Agent": "Mozilla/5.0"}

def page(market, y, m):
    path = f"{RAW}/{market}_{y}_{m:02d}.html"
    if os.path.exists(path) and os.path.getsize(path) > 20000:
        return open(path, encoding="utf-8").read()
    # Older periods are published without the "_0" suffix, so try both forms.
    urls = [f"https://mopsov.twse.com.tw/nas/t21/{market}/t21sc03_{y-1911}_{m}{sfx}.html" for sfx in ("_0", "")]
    for attempt in range(4):
        try:
            r = requests.get(urls[attempt % 2], headers=UA, timeout=30)
            html = r.content.decode("big5", errors="ignore")
            if len(html) > 20000:
                open(path, "w", encoding="utf-8").write(html)
                time.sleep(0.6)
                return html
        except requests.RequestException:
            pass
        time.sleep(3 * (attempt + 1))
    return None

def parse(html):
    s = BeautifulSoup(html, "lxml")
    for tr in s.find_all("tr"):
        c = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(c) >= 5 and c[0][:4].isdigit():
            try:
                yield c[0], c[1], float(c[2].replace(",", ""))
            except ValueError:
                continue

def main(start=2014):
    today = dt.date.today()
    jobs = [(mk, y, m) for y in range(start, today.year + 1) for m in range(1, 13)
            if (y, m) < (today.year, today.month) for mk in ("sii", "otc")]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(6) as ex:
        pages = list(ex.map(lambda j: (j, page(*j)), jobs))
    rows = []
    for (market, y, m), html in pages:
        if html is None:
            print("missing", market, y, m, file=sys.stderr, flush=True)
            continue
        for code, name, rev in parse(html):
            rows.append((f"{y}-{m:02d}-01", market, code, name, rev))
    df = pd.DataFrame(rows, columns=["month", "market", "code", "name", "revenue_k_twd"])
    df = df.drop_duplicates(["month", "code"])
    df.to_csv(OUT, index=False)
    print(len(df), "rows ->", OUT)

if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 2014)
