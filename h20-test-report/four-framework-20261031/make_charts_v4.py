"""Four-framework latency vs input length (batch = 1, log y).

One panel per beam width (32 / 64 / 128 / 256) - the widths vLLM could cover.
Reads data/all.csv; falls back to data/all.csv.new when the merge is locked.
"""

import csv
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
FIGD = os.path.join(HERE, "figs")
DATA = os.path.join(HERE, "data")

INK, INK2, GRID = "#0b0b0b", "#52514e", "#e4e3df"
plt.rcParams.update(
    {
        "font.family": ["Microsoft YaHei", "SimHei", "DejaVu Sans"],
        "font.size": 9,
        "text.color": INK,
        "axes.labelcolor": INK2,
        "xtick.color": INK2,
        "ytick.color": INK2,
        "axes.edgecolor": GRID,
        "svg.fonttype": "none",
    }
)

SRC = os.path.join(DATA, "all.csv")
alt = os.path.join(DATA, "all.csv.new")  # left behind when the merge could not swap
if os.path.exists(alt) and (
    not os.path.exists(SRC) or os.path.getmtime(alt) > os.path.getmtime(SRC)
):
    SRC = alt
print("source:", os.path.basename(SRC))

SERIES = [
    ("gr", "GR", "#2a78d6", "o"),
    ("sid_gr", "SID-GR", "#eb6834", "s"),
    ("sglang", "SGLang", "#1baf7a", "^"),
    ("vllm", "vLLM", "#eda100", "D"),
]

L = [256, 512, 1024, 2048, 4096, 8192, 10240]
LD = ["256", "512", "1k", "2k", "4k", "8k", "10k"]

data = {}
with open(SRC, encoding="utf-8") as fh:
    for r in csv.DictReader(fh):
        if r["batch"] == "1" and r["status"] == "complete":
            data[(r["backend"], int(r["length"]), int(r["beam"]))] = float(
                r["batch_mean_ms"]
            )

fig, axes = plt.subplots(2, 2, figsize=(9.4, 6.4), sharex=True, sharey=True)
for ax, w in zip(axes.flat, (32, 64, 128, 256)):
    for backend, label, color, marker in SERIES:
        pts = [(l, data[(backend, l, w)]) for l in L if (backend, l, w) in data]
        if not pts:
            continue
        xs, ys = zip(*pts)
        ax.plot(xs, ys, color=color, linewidth=2, marker=marker, markersize=5,
                label=label, markeredgecolor="white", markeredgewidth=1)
    ax.set_yscale("log")
    ax.set_xscale("log", base=2)
    ax.set_xticks(L, LD, rotation=35, ha="right", rotation_mode="anchor", fontsize=8.5)
    ax.minorticks_off()
    ax.grid(True, which="major", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.set_title(f"beam = {w}", loc="left", fontsize=10.5, color=INK)
    ax.tick_params(length=0)

for ax in axes[:, 0]:
    ax.set_ylabel("批延迟（ms，对数）")
for ax in axes[1, :]:
    ax.set_xlabel("输入长度（token）")

handles, labels = axes[0, 0].get_legend_handles_labels()
fig.legend(handles, labels, loc="lower center", ncol=4, frameon=False,
           bbox_to_anchor=(0.5, -0.01), fontsize=9.5)
fig.suptitle("四框架批延迟随输入长度的变化（batch = 1）", x=0.012, ha="left",
             fontsize=12.5, fontweight="bold", y=1.0)
fig.text(0.012, -0.055,
         "纵轴为对数刻度。vLLM 只覆盖 beam 32–256 与 batch 1；其余三家务有 168 点全矩阵。",
         fontsize=8.5, color=INK2, ha="left")
fig.tight_layout(rect=(0, 0.03, 1, 0.97))
fig.savefig(os.path.join(FIGD, "fig8-four-framework-latency.svg"), bbox_inches="tight")
fig.savefig(os.path.join(FIGD, "fig8-four-framework-latency.png"), dpi=150, bbox_inches="tight")
print("wrote fig8")
