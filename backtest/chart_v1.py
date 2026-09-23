import pandas as pd, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
PX = pd.read_csv("data/processed/prices.csv", index_col=0, parse_dates=True)
B = {"Memory / storage": ("memory", ["MU", "WDC", "STX", "000660.KS"]),
     "Optics": ("optics", ["LITE", "COHR", "AAOI", "FN", "CIEN"]),
     "Power equipment": ("power", ["ETN", "HUBB", "POWL", "VRT", "SIE.DE", "1519.TW"])}
SURF, INK, MUTED, S1, ON = "#fcfcfb", "#0b0b0b", "#52514e", "#2a78d6", "#1baf7a"
plt.rcParams.update({"font.size": 10, "axes.edgecolor": "#c9c8c2", "axes.labelcolor": MUTED,
                     "xtick.color": MUTED, "ytick.color": MUTED, "axes.facecolor": SURF, "figure.facecolor": SURF})
fig, axes = plt.subplots(2, 3, figsize=(16, 7.5), gridspec_kw={"height_ratios": [2.2, 1]}, sharex="col")
for j, (title, (g, tk)) in enumerate(B.items()):
    ind = pd.read_csv(f"backtest/v1_{g}_index.csv", index_col=0, parse_dates=True)
    ind["decision"] = pd.to_datetime(ind["decision"])
    r = PX[tk].ffill(limit=5).pct_change(fill_method=None).mean(axis=1)
    r = r[r.index >= ind.decision.iloc[0]]
    lvl = (1 + r.fillna(0)).cumprod() * 100
    ax, bx = axes[0, j], axes[1, j]
    pos = ind.set_index("decision")["ruleA"].reindex(lvl.index, method="ffill").fillna(False).astype(bool)
    ax.fill_between(lvl.index, 1, lvl.max() * 2, where=pos, color=ON, alpha=0.13, linewidth=0, step="post")
    ax.plot(lvl.index, lvl, color=S1, lw=2)
    ax.set_yscale("log"); ax.set_ylim(lvl.min() * 0.8, lvl.max() * 1.3)
    ax.set_title(f"{title}: basket, rebased to 100 (log)", loc="left", color=INK, fontsize=11)
    ax.grid(axis="y", color="#e6e5df", lw=0.8); ax.spines[["top", "right"]].set_visible(False)
    bx.axhline(0, color=MUTED, lw=0.8)
    bx.plot(ind.decision, ind.C, color=INK, lw=2)
    bx.fill_between(ind.decision, -8, 8, where=ind.ruleA, color=ON, alpha=0.13, linewidth=0, step="post")
    bx.set_ylim(min(-3, ind.C.min() * 1.1), max(3, ind.C.max() * 1.1))
    bx.set_title("Pressure index (z-score), plotted at its decision date", loc="left", color=MUTED, fontsize=9.5)
    bx.grid(axis="y", color="#e6e5df", lw=0.8); bx.spines[["top", "right"]].set_visible(False)
fig.text(0.01, 0.005, "Green shading = Rule A 'tightening' ON (index > 0 and rising). Taiwan monthly revenue + US PPI, point-in-time. Pre-registered v1; baskets are hand-picked today (hindsight bias).",
         color=MUTED, fontsize=9)
fig.tight_layout(rect=(0, 0.03, 1, 1))
fig.savefig("artifacts/bottleneck-backtest-v1.png", dpi=110)
