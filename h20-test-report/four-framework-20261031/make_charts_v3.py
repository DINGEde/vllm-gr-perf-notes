"""168-point matrix: SGLang and SID-GR relative to GR (batch = 1).

figs/fig6-sidgr-ratio-heatmap.svg   diverging around 1.0
figs/fig7-sglang-ratio-heatmap.svg  sequential
"""

import csv
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm

HERE = os.path.dirname(os.path.abspath(__file__))
FIGD = os.path.join(HERE, "figs")
os.makedirs(FIGD, exist_ok=True)

INK, INK2 = "#0b0b0b", "#52514e"
plt.rcParams.update(
    {
        "font.family": ["Microsoft YaHei", "SimHei", "DejaVu Sans"],
        "font.size": 9,
        "text.color": INK,
        "axes.labelcolor": INK2,
        "xtick.color": INK2,
        "ytick.color": INK2,
        "svg.fonttype": "none",
    }
)

L = [256, 512, 1024, 2048, 4096, 8192, 10240]
LD = ["256", "512", "1k", "2k", "4k", "8k", "10k"]
W = [32, 64, 128, 256, 512, 1024]
WD = ["32", "64", "128", "256", "512", "1024"]


def load(backend):
    """Read one backend out of the merged matrix (data/all.csv)."""
    out = {}
    with open(os.path.join(HERE, "data", "all.csv"), encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["backend"] == backend and r["status"] == "complete":
                out[(int(r["length"]), int(r["beam"]), int(r["batch"]))] = float(
                    r["batch_mean_ms"]
                )
    return out


GR = load("gr")
SGL = load("sglang")
SID = load("sid_gr")


def grid(D, b=1):
    return np.array([[D[(l, w, b)] / GR[(l, w, b)] for l in L] for w in W])


def draw(mat, cmap, title, note, out, norm=None, fmt="%.2f", cbar_label=""):
    fig, ax = plt.subplots(figsize=(8.2, 4.4))
    im = ax.imshow(mat, cmap=cmap, norm=norm, aspect="auto", origin="lower")
    ax.set_xticks(range(len(L)), LD)
    ax.set_yticks(range(len(W)), WD)
    ax.set_xlabel("输入长度（token）")
    ax.set_ylabel("beam 宽度")
    for i in range(mat.shape[0]):
        for j in range(mat.shape[1]):
            v = mat[i, j]
            # text stays ink-colored; contrast handled by the cell value label
            ax.text(
                j,
                i,
                fmt % v,
                ha="center",
                va="center",
                fontsize=8.5,
                color="#fcfcfb" if norm is not None and abs(v - 1.0) > 0.06 else "#0b0b0b",
            )
    ax.set_title(title, loc="left", fontsize=11.5, fontweight="bold", pad=10)
    fig.text(0.01, -0.075, note, fontsize=8.5, color=INK2, ha="left")
    cb = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
    cb.set_label(cbar_label, color=INK2, fontsize=9)
    cb.outline.set_visible(False)
    fig.tight_layout()
    fig.savefig(os.path.join(FIGD, out + ".svg"), bbox_inches="tight")
    fig.savefig(os.path.join(FIGD, out + ".png"), dpi=150, bbox_inches="tight")
    plt.close(fig)


# diverging: blue (faster than GR) - neutral - orange (slower than GR)
div = LinearSegmentedColormap.from_list(
    "ratio", ["#1b6fc4", "#8fbdea", "#f0efec", "#f4a97f", "#d4551f"]
)
m = grid(SID)
draw(
    m,
    div,
    "SID-GR 对 GR 延迟比（batch = 1）",
    "低于 1（蓝）表示 SID-GR 更快，高于 1（橙）表示 GR 更快；列向右输入变长，行向上 beam 变宽。",
    "fig6-sidgr-ratio-heatmap",
    norm=TwoSlopeNorm(vmin=0.88, vcenter=1.0, vmax=1.12),
    cbar_label="延迟比",
)

seq = LinearSegmentedColormap.from_list("seq", ["#e8f0fa", "#c6dbf2", "#7fb0e0", "#3d84c6", "#1b5b96"])
m2 = grid(SGL)
draw(
    m2,
    seq,
    "SGLang 对 GR 延迟比（batch = 1）",
    "数值越大 = GR 领先越多。beam 越宽、输入越长，差距越大（1.27× → 5.31×）。",
    "fig7-sglang-ratio-heatmap",
    cbar_label="延迟比",
)

print("wrote fig6/fig7")
