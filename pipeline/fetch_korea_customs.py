#!/usr/bin/env python3
"""Download monthly Korean chip exports from the free Korea Customs portal.

Requires requests and beautifulsoup4; no browser, API key, or account.
Run: /home/nik/projects/ai-market-analysis/.venv/bin/python fetch_korea_customs.py
See README.md for the endpoint contract, units, and validation methodology.
"""

import argparse
import calendar
import csv
import hashlib
import io
import json
import re
import statistics
import sys
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import requests
from bs4 import BeautifulSoup


BASE = "https://tradedata.go.kr"
INDEX = BASE + "/cts/index.do"
ENDPOINT = BASE + "/cts/hmpg/retrieveTrade.do"
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PROC = ROOT / "data/processed"
START = "2013-01"
HS_CODES = ("8542", "854232")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class CustomsClient:
    """One sequential request at a time, with at least 0.6s between starts."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "KoreaMonthlyExports/1.0 (requests; public trade statistics)",
            "Referer": INDEX,
            "X-Requested-With": "XMLHttpRequest",
        })
        self.last_start = 0.0

    def request(self, method, url, **kwargs):
        for attempt in range(4):
            time.sleep(max(0, 0.6 - (time.monotonic() - self.last_start)))
            self.last_start = time.monotonic()
            try:
                response = self.session.request(
                    method, url, timeout=(15, 90), allow_redirects=False, **kwargs
                )
                if response.status_code == 429 or response.status_code >= 500:
                    response.raise_for_status()
                if 300 <= response.status_code < 400:
                    raise ValueError(f"Unexpected redirect from {url}")
                response.raise_for_status()
                response.encoding = "utf-8"
                return response
            except (requests.ConnectionError, requests.Timeout, requests.HTTPError) as exc:
                retryable = (not isinstance(exc, requests.HTTPError)
                             or exc.response.status_code == 429
                             or exc.response.status_code >= 500)
                if not retryable or attempt == 3:
                    raise
                delay = 2 ** (attempt + 1)
                if isinstance(exc, requests.HTTPError):
                    retry_after = exc.response.headers.get("Retry-After", "")
                    if retry_after.isdigit():
                        delay = max(delay, int(retry_after))
                print(f"Retrying after {delay}s: {exc}", file=sys.stderr)
                time.sleep(delay)


def latest_published_month(html):
    """Use the finalized-statistics panel, never the partial-month panel."""
    panel = BeautifulSoup(html, "html.parser").select_one("li.definitStat .upArea p b")
    if panel is None:
        raise ValueError("Cannot find finalized-statistics date on the homepage")
    match = re.fullmatch(r"(\d{4})\.(\d{2})\.01\s*~\s*(\d{2})\.(\d{2})",
                         panel.get_text(strip=True))
    if not match:
        raise ValueError(f"Unrecognized publication period: {panel.get_text()!r}")
    year, month, end_month, end_day = map(int, match.groups())
    if month != end_month or end_day != calendar.monthrange(year, month)[1]:
        raise ValueError("Homepage publication period is not a full calendar month")
    if date(year, month, end_day) >= date.today():
        raise ValueError("Publication period is not a completed calendar month")
    return f"{year:04d}-{month:02d}"


def months_between(start, end):
    year, month = map(int, start.split("-"))
    result = []
    while f"{year:04d}-{month:02d}" <= end:
        result.append(f"{year:04d}-{month:02d}-01")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return result


def query_params(hs, start, end):
    return {
        "tradeKind": "ETS_MNK_1020000A",  # By commodity, all partner countries.
        "priodKind": "MON",
        "priodFr": start.replace("-", ""),
        "priodTo": end.replace("-", ""),
        "statsBase": "acptDd",  # Customs acceptance date, not departure date.
        "ttwgTpcd": "1000",  # Weight in tonnes; does not control value units.
        "selectPaging": "1",
        "showPagingLine": "100",
        "sortColumn": "",
        "sortOrder": "",
        "hsSgnGrpCol": f"HS{len(hs)}_SGN",
        "hsSgnWhrCol": f"HS{len(hs)}_SGN",
        "hsSgn": hs,
        "subHsSgn": "",  # Return the requested aggregate, not its children.
    }


def parse_response(payload, hs, start, end):
    if payload.get("searchresult") != "OK" or not isinstance(payload.get("items"), list):
        raise ValueError(f"Unsuccessful Customs response: {str(payload)[:300]}")
    items = payload["items"]
    # A one-year, one-HS query fits on one page (12 months plus grand total).
    # Never silently accept a truncated response if the endpoint changes.
    if int(payload["count"]) != len(items):
        raise ValueError("Truncated response: returned items do not match count")
    rows = {}
    total_count = 0
    for item in items:
        title = item["priodTitle"].strip()
        if title == "총계":
            total_count += 1
            continue
        if not re.fullmatch(r"\d{4}\.\d{2}", title):
            raise ValueError(f"Unexpected month: {title!r}")
        month = title.replace(".", "-") + "-01"
        if item["hsSgn"].strip() != hs or month in rows:
            raise ValueError(f"Wrong HS or duplicate month: {hs}, {month}")
        amount = str(item["expUsdAmt"]).strip().replace(",", "")
        if not re.fullmatch(r"\d+(?:\.\d+)?", amount):
            raise ValueError(f"Invalid or suppressed export amount: {amount!r}")
        usd = Decimal(amount) * 1000  # Portal explicitly reports thousand USD.
        if usd != usd.to_integral_value():
            raise ValueError(f"Non-integral USD amount: {usd}")
        rows[month] = {"month": month, "hs": hs, "usd": int(usd)}
    expected = set(months_between(start, end))
    if total_count != 1 or set(rows) != expected:
        raise ValueError(
            f"Incomplete/unexpected months for HS {hs}: "
            f"missing={sorted(expected - set(rows))}, extra={sorted(set(rows) - expected)}"
        )
    return [rows[month] for month in sorted(rows)]


def validate(rows, reference):
    """Compare against the specified Comtrade file, using Comtrade as denominator."""
    official = {(r["month"], r["hs"]): r["usd"] for r in rows}
    with reference.open(newline="") as handle:
        reference_rows = list(csv.DictReader(handle))
    ref = {}
    for row in reference_rows:
        month = row["month"]
        if "2013-01-01" <= month <= "2025-12-01":
            if month in ref:
                raise ValueError(f"Duplicate reference month: {month}")
            ref[month] = row
    expected = months_between("2013-01", "2025-12")
    missing_reference = sorted(set(expected) - set(ref))
    if not ref:
        raise ValueError("Reference contains no months in the validation period")
    if missing_reference:
        print(f"Reference has missing months (excluded from comparisons): {missing_reference}",
              file=sys.stderr)
    comparisons, summary = [], {}
    for hs in HS_CODES:
        signed, absolute, differences = [], [], []
        for month in expected:
            if month not in ref:
                continue
            baseline = Decimal(ref[month][hs])
            if not baseline.is_finite() or baseline <= 0:
                raise ValueError(f"Invalid reference value: {hs}, {month}")
            value = official[(month, hs)]
            difference = Decimal(value) - baseline
            percent = float(difference / baseline * 100)
            signed.append(percent)
            absolute.append(abs(percent))
            differences.append(float(difference))
            comparisons.append({
                "month": month, "hs": hs, "customs_usd": value,
                "comtrade_usd": str(baseline), "difference_usd": str(difference),
                "signed_pct_difference": percent, "absolute_pct_difference": abs(percent),
            })
        summary[hs] = {
            "months": len(signed),
            "missing_reference_months": missing_reference,
            "median_absolute_pct_difference": statistics.median(absolute),
            "median_signed_pct_difference": statistics.median(signed),
            "max_absolute_pct_difference": max(absolute),
            "max_absolute_difference_usd": max(map(abs, differences)),
            "mean_signed_difference_usd": statistics.mean(differences),
            "months_within_1000_usd": sum(abs(d) <= 1000 for d in differences),
        }
    return comparisons, summary


def csv_text(rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def atomic_write(path, content):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path,
                        default=PROC / "korea_chip_exports.csv")
    args = parser.parse_args()
    if not args.reference.is_file():
        parser.error(f"Validation reference not found: {args.reference}")
    raw_dir = ROOT / "data/raw/korea_customs"
    raw_dir.mkdir(parents=True, exist_ok=True)
    client = CustomsClient()
    started_at = utc_now()
    homepage = client.request("GET", INDEX)
    latest = latest_published_month(homepage.text)
    if latest < "2025-12":
        raise ValueError("Published coverage is insufficient for required validation")
    atomic_write(raw_dir / "index.html", homepage.text)
    print(f"Latest finalized month: {latest}", flush=True)
    rows, requests_log = [], []
    for hs in HS_CODES:
        for year in range(2013, int(latest[:4]) + 1):
            start, end = f"{year}-01", min(f"{year}-12", latest)
            params = query_params(hs, start, end)
            response = client.request("POST", ENDPOINT, data=params)
            payload = response.json()
            parsed = parse_response(payload, hs, start, end)
            name = f"{hs}_{year}.json"
            atomic_write(raw_dir / name, response.text)
            requests_log.append({
                "fetched_at_utc": utc_now(), "method": "POST", "url": ENDPOINT,
                "parameters": params, "raw_file": f"data/raw/korea_customs/{name}",
                "sha256": hashlib.sha256(response.content).hexdigest(),
                "observations": len(parsed),
            })
            rows.extend(parsed)
            print(f"HS {hs}: {start} through {end}: {len(parsed)} months", flush=True)
    rows.sort(key=lambda row: (row["month"], row["hs"]))
    expected_count = len(months_between(START, latest)) * len(HS_CODES)
    if len(rows) != expected_count:
        raise ValueError("Unexpected output row count")
    by_key = {(r["month"], r["hs"]): r["usd"] for r in rows}
    for month in months_between(START, latest):
        if not 0 < by_key[(month, "854232")] <= by_key[(month, "8542")]:
            raise ValueError(f"Memory exports exceed all IC exports (or are zero): {month}")
    comparisons, summary = validate(rows, args.reference)
    metadata = {
        "source": "Korea Customs Service, 수출입무역통계, 수출입 실적 / 품목별",
        "homepage": INDEX, "endpoint": ENDPOINT,
        "started_at_utc": started_at, "completed_at_utc": utc_now(),
        "first_month": START + "-01", "last_month": latest + "-01",
        "rows": len(rows), "hs_codes": HS_CODES,
        "basis": "Customs acceptance date (acptDd), exports to all partners",
        "source_unit": "thousand USD", "usd_multiplier": 1000,
        "precision_note": "Source values are rounded to whole thousand USD.",
        "reference_file": str(args.reference.resolve()),
        "reference_sha256": hashlib.sha256(args.reference.read_bytes()).hexdigest(),
        "validation_period": ["2013-01-01", "2025-12-01"],
        "validation_formula": "100 * abs(customs_usd - comtrade_usd) / comtrade_usd",
        "validation": summary, "requests": requests_log,
    }
    atomic_write(PROC / "korea_customs_validation.csv", csv_text(comparisons))
    atomic_write(PROC / "korea_customs_metadata.json",
                 json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    # Publish the main CSV only after every month and comparison has been checked.
    atomic_write(PROC / "korea_customs_exports.csv", csv_text(rows))
    print(json.dumps(summary, indent=2))
    print(f"Wrote {len(rows)} rows to {PROC / 'korea_customs_exports.csv'}")
    print("Last six memory-export observations:")
    print(csv_text([r for r in rows if r["hs"] == "854232"][-6:]), end="")


if __name__ == "__main__":
    try:
        main()
    except (requests.RequestException, ValueError, KeyError, OSError) as exc:
        sys.exit(f"ERROR: {exc}")
