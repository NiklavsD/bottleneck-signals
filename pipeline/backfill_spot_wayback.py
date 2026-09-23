"""Backfill the free homepage spot tables, with an auditable Wayback source per row.

Run from any directory with the repository's Python environment. Raw responses and
the CDX index are cached under data/raw/wayback_dramx (already gitignored).
Use --offline to rebuild without network access, or --refresh-index for new captures.
Prices loaded only by JavaScript cannot be recovered from a raw homepage capture.
Specifications (speed, organization and eTT grade) remain separate series.
"""

import argparse
import bisect
import concurrent.futures
import csv
import datetime as dt
import email.utils
import json
import math
from pathlib import Path
import re
import threading
import time
from collections import Counter, defaultdict

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data/raw/wayback_dramx"
OUT = ROOT / "data/context/spot_history.csv"
REPORT = ROOT / "work/spot/coverage.txt"
CDX = "https://web.archive.org/cdx/search/cdx"
CDX_PARAMS = {"url": "dramexchange.com/", "output": "json", "from": "2010",
              "filter": "statuscode:200", "collapse": "timestamp:8"}
FIELDS = ["date", "category", "item", "avg_usd", "source_snapshot"]
UA = "Mozilla/5.0 (compatible; LeadtimeBot/0.1; Wayback spot history research)"
UPDATE = re.compile(r"Last\s*Update\s*:?", re.I)


def clean(text):
    return re.sub(r"\s+", " ", text).strip()


def snapshot_url(timestamp):
    return f"https://web.archive.org/web/{timestamp}id_/https://www.dramexchange.com/"


class Downloader:
    """Two workers maximum; space request starts and honor retry backoff."""

    def __init__(self, delay=2.0, attempts=4):
        self.delay = delay
        self.attempts = attempts
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.local = threading.local()

    def get(self, url, **kwargs):
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
            self.local.session.headers.update({"User-Agent": UA})
        for attempt in range(self.attempts):
            with self.lock:
                time.sleep(max(0, self.next_request - time.monotonic()))
                self.next_request = time.monotonic() + self.delay
            try:
                response = self.local.session.get(url, timeout=(20, 60), **kwargs)
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                response = getattr(exc, "response", None)
                status = response.status_code if response is not None else None
                if status in (400, 401, 403, 404, 410) or attempt + 1 == self.attempts:
                    raise
                pause = 2 ** (attempt + 2)
                if response is not None:
                    retry = response.headers.get("Retry-After", "")
                    if retry.isdigit():
                        pause = max(pause, int(retry))
                    elif retry:
                        try:
                            pause = max(pause, email.utils.parsedate_to_datetime(retry).timestamp() - time.time())
                        except (ValueError, TypeError):
                            pass
                print(f"Retry {attempt + 1}: {url}: {exc}; backoff {pause:.0f}s", flush=True)
                with self.lock:
                    self.next_request = max(self.next_request, time.monotonic() + pause)
        raise RuntimeError("unreachable")


def load_index(downloader, offline=False, refresh=False):
    path = CACHE / "cdx.json"
    if refresh or not path.exists():
        if offline:
            raise ValueError("No cached CDX index; run once without --offline")
        response = downloader.get(CDX, params=CDX_PARAMS)
        data = response.json()
        validate_index(data)
        path.write_text(json.dumps(data), encoding="utf-8")
    data = json.loads(path.read_text(encoding="utf-8"))
    validate_index(data)
    return [dict(zip(data[0], row)) for row in data[1:]]


def validate_index(data):
    if not isinstance(data, list) or not data or "timestamp" not in data[0]:
        raise ValueError("Invalid or empty CDX index")


def select_snapshots(index):
    months = defaultdict(set)
    for row in index:
        ts = row["timestamp"]
        if row.get("statuscode", "200") == "200" and re.fullmatch(r"\d{14}", ts) and ts[:4] >= "2010":
            months[ts[:6]].add(ts)
    selected = set()
    for month, timestamps in months.items():
        for day in (1, 15):
            target = dt.date(int(month[:4]), int(month[4:]), day)
            selected.add(min(timestamps, key=lambda ts: (
                abs((dt.datetime.strptime(ts[:8], "%Y%m%d").date() - target).days), ts)))
    return sorted(selected)


def fetch_snapshot(timestamp, downloader, offline=False):
    path = CACHE / f"{timestamp}.html"
    if path.exists():
        return path.read_bytes()
    if offline:
        raise ValueError("not cached")
    response = downloader.get(snapshot_url(timestamp))
    actual = re.search(r"/web/(\d{14})", response.url)
    if not actual or actual[1] != timestamp:
        raise ValueError(f"replay redirected to a different capture: {response.url}")
    if b"<html" not in response.content.lower():
        raise ValueError("replay did not return HTML")
    path.write_bytes(response.content)
    return response.content


def update_date(text):
    """Old 'LastUpdate' and newer 'Last Update', including tagged dates."""
    if not UPDATE.search(text):
        return None
    text = UPDATE.split(clean(text), maxsplit=1)[-1].strip()
    patterns = [
        (r"([A-Za-z]{3,9})\.?\s*(\d{1,2}),?\s+(\d{4})", ("%b %d %Y", "%B %d %Y")),
        (r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", ("%Y %m %d",)),
        (r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", ("%m %d %Y",)),
    ]
    for pattern, formats in patterns:
        match = re.match(pattern, text)
        if match:
            for fmt in formats:
                try:
                    return dt.datetime.strptime(" ".join(match.groups()), fmt).date()
                except ValueError:
                    pass
    return None


def stable_item(item):
    item = clean(item).replace("×", "x").replace("*", "x")
    # The old DDR4 tables use '8G (1G*8)' for the same bit capacity.
    item = re.sub(r"^(DDR\d?\s+\d+)G(?=\s*\()", r"\1Gb", item)
    item = re.sub(r"\s*\(\s*([^()]+?)\s*\)\s*", r" (\1) ", item)
    item = re.sub(r"(\d)\s*(mhz|mbps)\b",
                  lambda m: m[1] + {"mhz": "MHz", "mbps": "Mbps"}[m[2].lower()], item, flags=re.I)
    item = re.sub(r"^(DDR[45]\s+\d+Gb)\s+(\d+[GM]x\d+)\b", r"\1 (\2)", item)
    item = clean(item)
    # Explicit capacity and memory technology; excludes eMMC, cards, contracts,
    # generic labels and the lists of product names in AJAX loading placeholders.
    if not re.search(r"\b\d+\s*(?:[GM]b|[GM]B)\b", item):
        return None
    if re.match(r"^(?:DDR\d?|SDRAM|GDDR\d+)\b", item, re.I):
        return item
    if re.match(r"^(?:(?:3D\s+)?(?:SLC|MLC|TLC|QLC)\s+\d+|\d+\s*[GM]b\b)", item, re.I) and re.search(r"\b(?:SLC|MLC|TLC|QLC)\b", item):
        return item
    return None


def classify(item, context):
    if "DIMM" in item.upper():
        return "dram_module"
    if item.upper().startswith("GDDR"):
        return "gddr"
    if item.upper().startswith(("DDR", "SDRAM")):
        return "dram_chip"
    # TLC appears both in the packaged Flash table and the Wafer table.
    return "nand_wafer" if "wafer" in context.lower() else "nand_chip"


def number(text):
    try:
        value = float(text.replace(",", "").strip())
        return value if math.isfinite(value) else None
    except ValueError:
        return None


def parse_snapshot(html, timestamp):
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    fallback = dt.datetime.strptime(timestamp[:8], "%Y%m%d").date()
    # Record update markers in document order, including dates split across tags.
    positions = {id(node): i for i, node in enumerate(soup.descendants)}
    markers = []
    for node in soup.find_all(string=UPDATE):
        stamp = update_date(str(node)) or update_date(node.parent.get_text(" ", strip=True))
        if stamp:
            markers.append((positions[id(node)], stamp))
    marker_positions = [pos for pos, _ in markers]
    rows, warnings = [], []
    for table in soup.find_all("table"):
        trs = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]
        header = None
        context = table.parent.get_text(" ", strip=True)[:250]
        tbody = table.find("tbody")
        context += " " + (tbody.get("id", "") if tbody else "")
        marker = bisect.bisect_left(marker_positions, positions[id(table)]) - 1
        asof = markers[marker][1] if marker >= 0 else fallback
        for tr in trs:
            cells = tr.find_all(["td", "th"], recursive=False)
            # Never interpret nested layout tables as price rows.
            if any(cell.find("table") for cell in cells):
                continue
            values = [clean(cell.get_text(" ", strip=True)) for cell in cells]
            labels = [v.lower() for v in values]
            if "item" in labels and "session average" in labels:
                header = labels
                marker = bisect.bisect_left(marker_positions, positions[id(tr)]) - 1
                asof = markers[marker][1] if marker >= 0 else fallback
                continue
            if header is None or len(values) != len(header):
                continue
            item = stable_item(values[header.index("item")])
            if not item:
                continue
            value = number(values[header.index("session average")])
            if value is None:
                warnings.append(f"{timestamp}: unavailable price: {item}")
                continue
            if asof > fallback + dt.timedelta(days=1):
                warnings.append(f"{timestamp}: SUSPICIOUS future page date {asof}: {item}")
            if (fallback - asof).days > 60:
                warnings.append(f"{timestamp}: stale page date {asof}: {item}")
            low = number(values[header.index("session low")]) if "session low" in header else None
            high = number(values[header.index("session high")]) if "session high" in header else None
            if low is not None and high is not None and not low <= value <= high:
                warnings.append(f"{timestamp}: SUSPICIOUS average outside [{low}, {high}]: {item} = {value}")
            rows.append(dict(date=str(asof), category=classify(item, context), item=item,
                             avg_usd=value, source_snapshot=snapshot_url(timestamp)))
    if not rows:
        reason = "JavaScript loading placeholders; prices absent from raw HTML" if "loading" in soup.get_text().lower() else "no supported spot tables"
        warnings.append(f"{timestamp}: {reason}")
    else:
        for body in soup.find_all("tbody", id=re.compile(r"(?:DramSpot|FlashSpot|ModuleSpot|GDDR|WaferSpot)", re.I)):
            if "loading" in body.get_text().lower():
                warnings.append(f"{timestamp}: {body['id']}: JavaScript loading placeholder; partial snapshot")
    return rows, warnings


def consolidate(rows):
    chosen, warnings = {}, []
    for row in sorted(rows, key=lambda r: r["source_snapshot"]):
        key = (row["date"], row["category"], row["item"])
        if key in chosen:
            prior = chosen[key]
            if prior["avg_usd"] != row["avg_usd"]:
                warnings.append(f"SUSPICIOUS conflicting same-date quote {key}: {prior['avg_usd']} ({prior['source_snapshot']}) -> {row['avg_usd']} ({row['source_snapshot']}); keep latest capture")
        chosen[key] = row
    return sorted(chosen.values(), key=lambda r: (r["date"], r["category"], r["item"])), warnings


def coverage_report(rows, selected, counts, warnings):
    series = defaultdict(list)
    for row in rows:
        series[(row["category"], row["item"])].append(row)
    lines = ["DRAMeXchange Wayback spot history", f"Selected snapshots: {len(selected)}; parsed: {counts['parsed']}; empty: {counts['empty']}; failed/missing: {counts['failed']}",
             f"Rows: {len(rows)}; series: {len(series)}", "",
             "category | item | first date | last date | points"]
    for (cat, item), points in sorted(series.items()):
        points.sort(key=lambda r: r["date"])
        lines.append(f"{cat} | {item} | {points[0]['date']} | {points[-1]['date']} | {len(points)}")
    lines += ["", "SANITY CHECKS (flags retained in CSV)"]
    key_points = 0
    flags = []
    for (cat, item), points in sorted(series.items()):
        key = cat == "dram_chip" and re.match(r"^(DDR4 8Gb|DDR3 4Gb)\b", item)
        if key:
            key_points += len(points)
        for point in points:
            if point["avg_usd"] <= 0:
                flags.append(f"SUSPICIOUS nonpositive: {item} {point['date']} = {point['avg_usd']}; {point['source_snapshot']}")
        if not key:
            continue
        for previous, point in zip(points, points[1:]):
            a, b = previous["avg_usd"], point["avg_usd"]
            if a > 0 and b > 0 and max(a / b, b / a) > 5:
                plausible = int(point["date"][:4]) in (2017, 2021, 2025, 2026)
                note = "plausible shortage year, NOT independently confirmed" if plausible else "outside suggested shortage years"
                flags.append(f"SUSPICIOUS >5x: {item}: {previous['date']} {a} -> {point['date']} {b} ({max(a/b,b/a):.2f}x; {note}); {previous['source_snapshot']} -> {point['source_snapshot']}")
    lines.append(f"Checked {key_points} DDR4 8Gb / DDR3 4Gb observations for positivity and consecutive >5x changes within identical specifications.")
    for family in ("DDR4 8Gb", "DDR3 4Gb"):
        if not any(cat == "dram_chip" and item.startswith(family) for cat, item in series):
            flags.append(f"MISSING key family: {family}; sanity check cannot pass for this family")
    lines.extend(flags or ["PASS: all prices positive; no >5x changes in the key series."])
    lines += ["", "CAVEATS", "Raw homepage captures only; AJAX-only snapshots cannot supply prices.",
              "Nearest available capture to each month's 1st/15th; a shared nearest capture is used once.",
              "Use each table's preceding Last Update date; snapshot date is the fallback.",
              "Keep speed, organization, capacity and eTT grades separate; no synthetic continuous benchmark.",
              "Normalize whitespace, unit spelling, multiplication signs, organization parentheses and old DDR4 G/Gb notation only.",
              "Repeated (date, category, item) quotes use the latest selected capture; conflicts are flagged.",
              "Flash-table TLC is nand_chip; only the Wafer table is nand_wafer.",
              "Cached CDX index is reused until --refresh-index is requested.", "", "PARSE / FETCH / CONFLICT WARNINGS"]
    lines.extend(sorted(set(warnings)) or ["None"])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="rebuild from cached index and HTML")
    parser.add_argument("--refresh-index", action="store_true")
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    if args.offline and args.refresh_index:
        parser.error("--offline and --refresh-index are incompatible")
    CACHE.mkdir(parents=True, exist_ok=True)
    downloader = Downloader()
    selected = select_snapshots(load_index(downloader, args.offline, args.refresh_index))
    if not selected:
        raise SystemExit("No captures selected; existing output left intact")
    print(f"Selected {len(selected)} snapshots: {selected[0]} .. {selected[-1]}", flush=True)
    rows, warnings, counts = [], [], Counter()

    def process(ts):
        try:
            result, notes = parse_snapshot(fetch_snapshot(ts, downloader, args.offline), ts)
            return ts, result, notes, "parsed" if result else "empty"
        except (requests.RequestException, ValueError, OSError) as exc:
            return ts, [], [f"{ts}: FAILED: {exc}"], "failed"

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(process, ts) for ts in selected]
        for done, future in enumerate(concurrent.futures.as_completed(futures), 1):
            ts, result, notes, status = future.result()
            rows.extend(result)
            warnings.extend(notes)
            counts[status] += 1
            print(f"[{done}/{len(selected)}] {ts}: {status}, {len(result)} rows", flush=True)
    rows, conflicts = consolidate(rows)
    report = coverage_report(rows, selected, counts, warnings + conflicts)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(report, encoding="utf-8")
    print(report)
    if not rows:
        raise SystemExit("No prices recovered; existing CSV left intact. See coverage report.")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    if counts["failed"]:
        raise SystemExit("Partial backfill written; failed captures are listed in coverage.txt. Rerun to retry.")


if __name__ == "__main__":
    main()
