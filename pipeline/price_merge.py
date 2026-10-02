"""Shared guard for price files rebuilt from a fresh Yahoo download.

Yahoo sometimes returns an empty or truncated history for a ticker (2026-09-30: KLAC, WDC, GOOGL, HUBB and
^TWII came back empty). Rewriting the file from that download silently deletes those tickers. This keeps the
previous column for any ticker whose new history is empty or clearly shorter than what the file already had.
"""
import os

import pandas as pd

MAX_CARRIED = 3  # more carry-overs than this means the download itself is unhealthy: exit non-zero


def read_previous(path):
    if not os.path.exists(path):
        return None
    return pd.read_csv(path, index_col=0, parse_dates=True)


def merge_prices(new, prev, tickers, min_ratio=0.9):
    """Return (merged, carried, missing).

    A ticker is carried over (previous column kept as-is) when its new history is empty, or has fewer than
    `min_ratio` x the non-missing closes the previous file held. `missing` lists tickers with no usable new data
    and nothing to carry over.
    """
    new = new.reindex(columns=list(tickers))
    carried, missing = [], []
    for t in tickers:
        n_new = int(new[t].notna().sum())
        n_old = int(prev[t].notna().sum()) if prev is not None and t in prev else 0
        if n_old and n_new < min_ratio * n_old:
            carried.append(t)
        elif n_new == 0:
            missing.append(t)
    if carried:
        # extend the index only by dates the carried columns need, so other tickers' rows are untouched
        extra = prev.index[prev[carried].notna().any(axis=1)]
        new = new.reindex(new.index.union(extra))
        for t in carried:
            new[t] = prev[t].reindex(new.index)
    return new.sort_index(), carried, missing


def report(carried, missing):
    """Print what happened; return True if the run should exit non-zero."""
    if carried:
        print(f"WARNING: kept previous prices for {len(carried)} ticker(s) with empty/short new history: "
              + ", ".join(carried), flush=True)
    if missing:
        print(f"WARNING: no data and nothing to carry over for: {', '.join(missing)}", flush=True)
    if len(carried) > MAX_CARRIED:
        print(f"ERROR: {len(carried)} tickers carried over (> {MAX_CARRIED}); the download looks unhealthy", flush=True)
        return True
    return False
