"""Pre-publication checks for the physical observations used by active calls.

These checks do not change index aggregation or model thresholds. They reject
partial refreshes that would otherwise silently remove mature input members.
Three-month growth needs the due month, two preceding months, and their
prior-year counterparts. PPI YoY needs the due month and its prior-year value.
V6 members without prior-year history remain ineligible under the registered
present-in-both-periods rule; they are not forced into the index by this guard.
"""
import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.run_v2 import GROUPS, PPI


class Inputs:
    def __init__(self, root):
        self.folder = Path(root) / "data" / "processed"
        self.cache = {}

    def panel(self, filename, member=None, value=None):
        cache_key = filename, member, value
        if cache_key not in self.cache:
            try:
                if member:
                    frame = pd.read_csv(self.folder / filename, usecols=["month", member, value],
                                        dtype={member: str})
                    frame["month"] = pd.to_datetime(frame["month"]).dt.to_period("M").dt.to_timestamp()
                    frame[value] = pd.to_numeric(frame[value], errors="coerce")
                    panel = frame.pivot_table(index="month", columns=member, values=value)
                else:
                    panel = pd.read_csv(self.folder / filename, index_col=0, parse_dates=True)
                    panel.index = pd.to_datetime(panel.index).to_period("M").to_timestamp()
                    panel = panel.apply(pd.to_numeric, errors="coerce").groupby(level=0).last()
            except (OSError, ValueError, KeyError):
                raise RuntimeError(f"ABORT: cannot read required physical source {filename}") from None
            self.cache[cache_key] = panel.sort_index()
        return self.cache[cache_key]


def due_month(now, lag):
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Physical freshness validation requires an aware timestamp")
    today = pd.Timestamp(now.astimezone(dt.timezone.utc).date())
    decision_month = today.replace(day=1)
    if today.day < 16:
        decision_month -= pd.offsets.MonthBegin(1)
    return decision_month - pd.DateOffset(months=lag)


def require_series(panel, member, label, due, rolling=3, allow_new=False, positive=False):
    if member not in panel:
        raise RuntimeError(f"ABORT: {label} {member} is missing from its physical source")
    series = pd.to_numeric(panel[member], errors="coerce")
    observed = series[np.isfinite(series)]
    if observed.empty:
        raise RuntimeError(f"ABORT: {label} {member} has no finite physical observations")
    recent = pd.date_range(end=due, periods=rolling, freq="MS")
    prior = recent - pd.DateOffset(years=1)
    if allow_new and observed.index.min() > prior.min():
        # A newly listed/reclassified member cannot contribute to the registered
        # present-in-both-periods growth yet. Do not invent its earlier history.
        return False
    for month in list(recent) + list(prior):
        value = series.get(month, np.nan)
        if not np.isfinite(value) or (positive and value <= 0):
            raise RuntimeError(f"ABORT: {label} {member} is missing or invalid for required month {month:%Y-%m}")
    return True


def require_members(panel, members, label, due, allow_new=False):
    eligible = [require_series(panel, member, label, due, allow_new=allow_new) for member in members]
    if not eligible or not any(eligible):
        raise RuntimeError(f"ABORT: {label} has no mature members with required physical observations")


def validate_active_sources(root, calls, now):
    """Check required physical sources for current v4 and v6 calls before logging."""
    active = [call for call in calls if call.get("state") != "NO CALL"]
    if not active:
        return
    inputs = Inputs(root)
    themes = None
    for call in active:
        name, model = call["bottleneck"], call.get("model", "")
        if model.startswith("v4") and name in ("memory", "optics", "power"):
            tw = inputs.panel("tw_revenue.csv", "code", "revenue_k_twd")
            require_members(tw, GROUPS[name]["tw"], "Taiwan revenue", due_month(now, 1))
            if name == "memory":
                source = call.get("korea_source")
                if source == "korea_customs":
                    kr = inputs.panel("korea_customs_exports.csv", "hs", "usd")
                elif source == "comtrade":
                    kr = inputs.panel("korea_chip_exports.csv")
                else:
                    raise RuntimeError("ABORT: memory call is missing its selected Korea source")
                require_series(kr, "854232", f"Memory Korea exports ({source})", due_month(now, 1))
            elif name == "power":
                fred = inputs.panel("fred.csv")
                for series in PPI:
                    require_series(fred, series, "Power FRED PPI", due_month(now, 1),
                                   rolling=1, positive=True)
        elif model == "v6":
            if themes is None:
                try:
                    themes = {theme["slug"]: theme for theme in json.loads(
                        (Path(root) / "config" / "themes.json").read_text(encoding="utf-8"))}
                except (OSError, ValueError, KeyError):
                    raise RuntimeError("ABORT: cannot read active theme physical-source configuration") from None
            if name not in themes:
                raise RuntimeError(f"ABORT: active v6 theme {name} has no physical-source configuration")
            theme = themes[name]
            components = call.get("source_components")
            if not isinstance(components, list) or not components:
                raise RuntimeError(f"ABORT: active v6 theme {name} is missing physical-component metadata")
            for component in components:
                if component == "tw_revenue":
                    panel = inputs.panel("tw_revenue.csv", "code", "revenue_k_twd")
                    members = [item["code"] for item in theme.get("tw_signals", [])]
                    require_members(panel, members, f"{name} Taiwan revenue", due_month(now, 1), allow_new=True)
                elif component == "us_imports":
                    panel = inputs.panel("us_imports_all.csv", "hs", "usd")
                    members = [item["hs"] for item in theme.get("us_import_hs6", [])]
                    require_members(panel, members, f"{name} US imports", due_month(now, 2), allow_new=True)
                elif component == "kr_exports":
                    panel = inputs.panel("korea_customs_exports.csv", "hs", "usd")
                    members = [item["hs"] for item in theme.get("kr_export_hs", [])]
                    require_members(panel, members, f"{name} Korea exports", due_month(now, 1), allow_new=True)
                else:
                    raise RuntimeError(f"ABORT: active v6 theme {name} has an unknown physical component")
        else:
            raise RuntimeError(f"ABORT: unsupported active model source configuration for {name}")
