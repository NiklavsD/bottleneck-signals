import pandas as pd, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
exec(open("backtest/run_v2.py").read().split("if __name__")[0])
g = GROUPS["memory"]
ind = build(g)
b = basket_returns(g["basket"])
b = b[b.index >= ind.decision.iloc[0]]
lvl = (1 + b.fillna(0)).cumprod() * 100
off = (~ind.set_index("decision")["ruleM"]).reindex(lvl.index, method="ffill").fillna(False).astype(bool)
SURF, INK, MUTED, S1, OFFC = "#fcfcfb", "#0b0b0b", "#52514e", "#2a78d6", "#eb6834"
plt.rcParams.update({"font.size": 10, "axes.edgecolor": "#c9c8c2", "xtick.color": MUTED, "ytick.color": MUTED,
                     "axes.facecolor": SURF, "figure.facecolor": SURF})
fig, (ax, bx) = plt.subplots(2, 1, figsize=(13, 7.5), sharex=True, gridspec_kw={"height_ratios": [2.3, 1]})
for a in (ax, bx):
    a.axvspan(pd.Timestamp(OOS[0]), pd.Timestamp(OOS[1]), color="#f1f0ec", zorder=0)
    a.spines[["top", "right"]].set_visible(False); a.grid(axis="y", color="#e6e5df", lw=0.8)
ax.fill_between(lvl.index, 1, lvl.max() * 3, where=off, color=OFFC, alpha=0.18, linewidth=0, step="post")
ax.plot(lvl.index, lvl, color=S1, lw=2)
ax.set_yscale("log"); ax.set_ylim(lvl.min() * 0.8, lvl.max() * 1.4)
ax.set_title("Memory/storage basket (11 names incl. laggards), rebased to 100, log scale", loc="left", color=INK)
ax.text(pd.Timestamp("2008-04-01"), lvl.max() * 0.9, "OUT-OF-SAMPLE 2008–2017\n(rules never saw this)", color=MUTED, fontsize=9, va="top")
ax.text(pd.Timestamp("2017-05-01"), lvl.max() * 0.9, "IN-SAMPLE 2017–2026", color=MUTED, fontsize=9, va="top")
bx.axhline(0, color=MUTED, lw=0.8)
bx.plot(ind.decision, ind.C, color=INK, lw=2)
bx.fill_between(ind.decision, -10, 10, where=~ind.ruleM, color=OFFC, alpha=0.18, linewidth=0, step="post")
bx.set_ylim(ind.C.min() - 0.5, ind.C.max() + 0.5)
bx.set_title("Memory pressure index (TW memory revenue + Korea memory exports, z-score)", loc="left", color=MUTED, fontsize=9.5)
bx.xaxis.set_major_locator(mdates.YearLocator(2))
fig.text(0.01, 0.005, "Orange = Rule M says OUT (tight AND slowing). Pre-registered v2, point-in-time decisions on the 16th of the month after data. Not investment advice.",
         color=MUTED, fontsize=9)
fig.tight_layout(rect=(0, 0.03, 1, 1))
fig.savefig("artifacts/memory-signal-v2.png", dpi=110)
