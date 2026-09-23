"""Prices (yfinance), FRED series and SEC XBRL facts. All cached to data/processed/."""
import time, json, requests, pandas as pd, yfinance as yf

TICKERS = [
    # memory / storage
    "MU", "WDC", "STX", "SNDK", "000660.KS", "005930.KS", "2408.TW",
    # optics / data movement
    "LITE", "COHR", "AAOI", "FN", "CIEN", "CRDO", "3081.TWO", "VIAV",
    # power equipment
    "GEV", "ETN", "VRT", "POWL", "HUBB", "1519.TW", "SIE.DE",
    # v2: every Taiwan signal company, plus incumbents that were listed before 2016
    "2344.TW", "2337.TW", "2451.TW", "8299.TWO", "3260.TWO",
    "2455.TW", "4971.TWO", "3163.TWO", "3363.TWO", "2345.TW",
    "1503.TW", "1513.TW", "1514.TW", "ABBN.SW", "SU.PA", "6501.T",
    # benchmarks
    "SPY", "QQQ", "SOXX", "XLI",
]
FRED = {
    "PCU335311335311": "PPI transformers",
    "PCU335313335313": "PPI switchgear",
    "PCU333611333611": "PPI turbines & generator sets",
    "PCU334413334413": "PPI semiconductors",
    "PCU334112334112": "PPI computer storage devices",
    "IPG3344S": "Industrial production semis",
    "XTEXVA01KRM667S": "Korea total exports (USD, SA)",
}
SEC_CIK = {"MSFT": 789019, "GOOGL": 1652044, "AMZN": 1018724, "META": 1326801, "ORCL": 1341439,
           "MU": 723125, "WDC": 106040, "STX": 1137789,
           "ETN": 1551182, "HUBB": 48898, "POWL": 80420, "VRT": 1674101, "GEV": 1996810}
SEC_UA = {"User-Agent": "ai-market-analysis research contact@example.com"}

def prices():
    df = yf.download(TICKERS, start="2004-01-01", auto_adjust=True, progress=False, threads=True)["Close"]
    df.to_csv("data/processed/prices.csv")
    print("prices", df.shape, df.index.max())

def fred():
    out = {}
    for sid in FRED:
        for a in range(3):
            try:
                s = pd.read_csv(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}", index_col=0, parse_dates=True)
                out[sid] = pd.to_numeric(s.iloc[:, 0], errors="coerce"); break
            except Exception as e:
                time.sleep(5 * (a + 1))
    df = pd.DataFrame(out)
    df.to_csv("data/processed/fred.csv")
    print("fred", df.shape)

def sec():
    rows = []
    concepts = ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
                "InventoryNet", "CostOfRevenue", "CostOfGoodsAndServicesSold", "Revenues",
                "RevenueFromContractWithCustomerExcludingAssessedTax", "RevenueRemainingPerformanceObligation"]
    for tk, cik in SEC_CIK.items():
        j = requests.get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", headers=SEC_UA, timeout=60).json()
        g = j["facts"].get("us-gaap", {})
        for c in concepts:
            for u in g.get(c, {}).get("units", {}).get("USD", []):
                rows.append(dict(ticker=tk, concept=c, start=u.get("start"), end=u["end"], val=u["val"],
                                 filed=u["filed"], form=u.get("form"), fp=u.get("fp")))
        time.sleep(0.3)
    df = pd.DataFrame(rows)
    df.to_csv("data/processed/sec_facts.csv", index=False)
    print("sec", df.shape)

if __name__ == "__main__":
    prices(); fred(); sec()
