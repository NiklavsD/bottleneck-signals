"""US monthly import values for HS6 chapters 28, 84, 85 and 90, since 2013.

Historical request failures abort the refresh and preserve the existing processed dataset.
Only not-yet-due, previously unpublished months may return HTTP 204 without an error.
"""
import datetime as dt
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/raw/census_hs6"
OUT = ROOT / "data/processed/us_imports_all.csv"
CH = ("28", "84", "85", "90")


class FetchError(RuntimeError):
    pass


def expected_latest(today):
    """Conservative publication cutoff: month M is available on the 16th of M+2."""
    month = pd.Timestamp(today).replace(day=1)
    return (month - pd.offsets.MonthBegin(2 if today.day >= 16 else 3)).strftime("%Y-%m")


def validate_rows(rows, ym):
    if not isinstance(rows, list) or not rows:
        raise FetchError(f"Census {ym}: missing commodity rows")
    seen = set()
    for row in rows:
        if not isinstance(row, list) or len(row) < 3:
            raise FetchError(f"Census {ym}: malformed commodity row")
        code = row[0]
        if len(code) != 6 or not code.isdigit() or code[:2] not in CH or code in seen:
            raise FetchError(f"Census {ym}: invalid or duplicate HS6 code")
        value = float(row[2])
        if not pd.notna(value) or value < 0 or value == float("inf"):
            raise FetchError(f"Census {ym}: invalid import value")
        seen.add(code)
    return rows


def atomic_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def get(ym, key, *, recent=(), today=None, known_months=(), raw_dir=RAW):
    today = today or dt.date.today()
    path = Path(raw_dir) / f"{ym}.json"
    if path.exists() and ym not in recent:
        return validate_rows(json.loads(path.read_text(encoding="utf-8")), ym)
    previously_published = path.exists() or ym in known_months
    url = "https://api.census.gov/data/timeseries/intltrade/imports/hs"
    params = {"get": "I_COMMODITY,I_COMMODITY_SDESC,GEN_VAL_MO", "COMM_LVL": "HS6", "time": ym, "key": key}
    for attempt in range(4):
        try:
            response = requests.get(url, params=params, timeout=180)
            if response.status_code == 204:
                if previously_published or ym <= expected_latest(today):
                    raise FetchError(f"Census {ym}: previously available or due month is missing")
                return []
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list) or len(payload) < 2:
                raise FetchError(f"Census {ym}: malformed API response")
            rows = validate_rows([row for row in payload[1:] if isinstance(row, list) and row and row[0][:2] in CH], ym)
            atomic_text(path, json.dumps(rows))
            return rows
        except (requests.RequestException, ValueError, FetchError, TypeError, IndexError):
            if attempt < 3:
                time.sleep(5 * (attempt + 1))
    # Do not include the request exception: its URL can contain the API key.
    raise FetchError(f"Census {ym}: fetch failed after four attempts; processed data was preserved") from None


def refresh(key, *, today=None, months=None, output=OUT, raw_dir=RAW):
    today = today or dt.date.today()
    if months is None:
        months = [f"{y}-{m:02d}" for y in range(2013, today.year + 1) for m in range(1, 13)
                  if (y, m) < (today.year, today.month)]
    output = Path(output)
    known = set(pd.read_csv(output, usecols=["month"]).month.str[:7]) if output.exists() else set()
    recent = set(months[-3:])
    with ThreadPoolExecutor(3) as ex:
        results = list(ex.map(lambda ym: (ym, get(ym, key, recent=recent, today=today,
                                                 known_months=known, raw_dir=raw_dir)), months))
    out = [(ym + "-01", row[0], row[1], float(row[2])) for ym, rows in results for row in rows]
    frame = pd.DataFrame(out, columns=["month", "hs", "desc", "usd"])
    if frame.empty:
        raise FetchError("Census refresh returned no data; processed data was preserved")
    available = set(frame.month.str[:7])
    if not known.issubset(available):
        raise FetchError("Census refresh lost previously published months; processed data was preserved")
    atomic_text(output, frame.to_csv(index=False))
    print(frame.shape, frame.month.min(), frame.month.max(), frame.hs.nunique(), "codes")
    return frame


def main():
    values = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        with open(env_file) as handle:
            for line in handle:
                if "=" in line and not line.lstrip().startswith("#"):
                    name, value = line.strip().split("=", 1)
                    values[name] = value
    key = os.environ.get("CENSUS_API_KEY") or values.get("CENSUS_API_KEY")
    if not key:
        raise FetchError("CENSUS_API_KEY is required in the environment or .env (Census requires keys for all queries).")
    refresh(key)


if __name__ == "__main__":
    main()
