"""H20 GR 168-point matrix (decode_graph 33a1fdb): figures.

Data: data/all.csv, backend == "gr"  (lengths 256..10240 x beams 32..1024 x batches 1/2/4/8)
Output: figs/fig3-length-scaling.svg, fig4-batch-gain.svg, fig5-beam-cost.svg (+png)
"""

import csv
import math
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
FIGD = os.path.join(HERE, "figs")
os.makedirs(FIGD, exist_ok=True)

# validated categorical order (dataviz reference palette, slots 1-6)
COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#e3e2dd"

plt.rcParams.update(
    {
        "font.family": ["Microsoft YaHei", "SimHei", "DejaVu Sans"],
        "font.size": 9,
        "axes.edgecolor": INK2,
        "axes.labelcolor": INK2,
        "xtick.color": INK2,
        "ytick.color": INK2,
        "text.color": INK,
        "axes.titlesize": 10,
        "axes.titleweight": "semibold",
        "svg.fonttype": "none",
    }
)

L = [256, 512, 1024, 2048, 4096, 8192, 10240]
W = [32, 64, 128, 256, 512, 1024]
B = [1, 2, 4, 8]

rows = [
    r
    for r in csv.DictReader(open(os.path.join(HERE, "data", "all.csv"), encoding="utf-8"))
    if r["backend"] == "gr"
]
D = {
    (int(r["length"]), int(r["beam"]), int(r["batch"])): float(r["batch_mean_ms"])
    for r in rows
}

XLAB = ["256", "512", "1k", "2k", "4k", "8k", "10k"]
XT = list(range(len(L)))


def style(ax):
    ax.set_xticks(XT)
    ax.set_xticklabels(XLAB)
    ax.grid(True, axis="y", color=GRID, lw=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)


def legend(ax, labels, ncol=6, y=-0.18):
    from matplotlib.lines import Line2D

    h = [Line2D([], [], color=c, lw=2, label=t) for c, t in zip(COLORS, labels)]
    ax.legend(
        handles=h,
        ncol=ncol,
        frameon=False,
        loc="upper center",
        bbox_to_anchor=(0.5, y),
        handlelength=1.6,
        columnspacing=1.4,
        fontsize=9,
    )


def figure_legend(fig, labels, ncol, y=0.005):
    from matplotlib.lines import Line2D

    h = [Line2D([], [], color=c, lw=2, label=t) for c, t in zip(COLORS, labels)]
    fig.legend(
        handles=h,
        ncol=ncol,
        frameon=False,
        loc="lower center",
        bbox_to_anchor=(0.5, y),
        handlelength=1.6,
        columnspacing=1.8,
        fontsize=9.5,
    )


# ---------- fig3: latency vs input length, one panel per batch ----------
fig, axes = plt.subplots(2, 2, figsize=(11.0, 6.6), sharey=True, sharex=True)
for ax, b in zip(axes.ravel(), B):
    for i, w in enumerate(W):
        y = [D[(l, w, b)] for l in L]
        ax.plot(XT, y, color=COLORS[i], lw=2, marker="o", ms=4.5, zorder=3)
    style(ax)
    ax.set_yscale("log")
    ax.set_title("batch = %d" % b, loc="left")
    ax.set_ylim(12, 4600)
    ax.set_yticks([15, 30, 100, 300, 1000, 3000])
    ax.set_yticklabels(["15", "30", "100", "300", "1000", "3000"])
for ax in axes[1]:
    ax.set_xlabel("输入长度（token）")
for ax in axes[:, 0]:
    ax.set_ylabel("批延迟（ms，对数轴）")
figure_legend(fig, ["beam %d" % w for w in W], ncol=6, y=0.0)
fig.suptitle(
    "vllm-gr decode_graph 33a1fdb @ H20-3e：批延迟随输入长度变化（168 点全量）",
    fontsize=12,
    fontweight="bold",
    y=0.98,
)
fig.tight_layout(rect=(0, 0.05, 1, 0.96))
fig.savefig(os.path.join(FIGD, "fig3-length-scaling.svg"), bbox_inches="tight")
fig.savefig(os.path.join(FIGD, "fig3-length-scaling.png"), dpi=150, bbox_inches="tight")
plt.close(fig)

# ---------- fig4: batch throughput gain over batch=1 ----------
fig, ax = plt.subplots(figsize=(7.6, 3.8))
for i, w in enumerate(W):
    y = [8 * D[(l, w, 1)] / D[(l, w, 8)] for l in L]
    ax.plot(XT, y, color=COLORS[i], lw=2, marker="o", ms=4.5, zorder=3)
style(ax)
ax.set_xlim(-0.2, 6.2)
ax.set_ylim(1.0, 1.7)
ax.set_ylabel("吞吐倍数（batch 8 ÷ batch 1）")
ax.set_xlabel("输入长度（token）")
ax.axhline(1.0, color=INK2, lw=1, ls=(0, (4, 3)))
ax.tick_params(axis="x", rotation=45)
ax.set_title("8 路合批的吞吐增益：只在短输入 + 窄 beam 下成立")
figure_legend(fig, ["beam %d" % w for w in W], ncol=6, y=0.0)
fig.tight_layout(rect=(0, 0.06, 1, 1))
fig.savefig(os.path.join(FIGD, "fig4-batch-gain.svg"), bbox_inches="tight")
fig.savefig(os.path.join(FIGD, "fig4-batch-gain.png"), dpi=150, bbox_inches="tight")
plt.close(fig)

# ---------- fig5: beam cost multiplier (1024 vs 32) ----------
fig, ax = plt.subplots(figsize=(7.6, 3.8))
for i, b in enumerate(B):
    y = [D[(l, 1024, b)] / D[(l, 32, b)] for l in L]
    ax.plot(XT, y, color=COLORS[i], lw=2, marker="o", ms=4.5, zorder=3)
style(ax)
ax.set_xlim(-0.2, 6.2)
ax.set_ylim(1.0, 11.5)
ax.set_yticks([1,2,3,4,6,8,10])
ax.set_ylabel("延迟倍数（beam 1024 ÷ beam 32）")
ax.set_xlabel("输入长度（token）")
ax.axhline(1.0, color=INK2, lw=1, ls=(0, (4, 3)))
ax.tick_params(axis="x", rotation=45)
ax.set_title("beam 加宽的代价随输入变长而消失（输出仅 3 token，长输入被 prefill 支配）")
figure_legend(fig, ["batch %d" % b for b in B], ncol=4, y=0.0)
fig.tight_layout(rect=(0, 0.06, 1, 1))
fig.savefig(os.path.join(FIGD, "fig5-beam-cost.svg"), bbox_inches="tight")
fig.savefig(os.path.join(FIGD, "fig5-beam-cost.png"), dpi=150, bbox_inches="tight")
plt.close(fig)

print("wrote fig3/4/5 to", FIGD)
