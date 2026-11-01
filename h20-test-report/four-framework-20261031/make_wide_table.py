"""Wide comparison table: one row per configuration, one column per framework.

Input  : data/all.csv  (source-merged matrix)
Output : data/wide-168.csv          machine-readable, all 168 configurations
         report-wide-20261031.md    markdown, one table per batch

Cell = batch_mean_ms (lower is better). Ratios are relative to vllm-gr.
vLLM only covers batch 1 / beam 32-256, so the other cells stay empty.
"""

import csv
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

FRAMES = [("gr", "vllm-gr"), ("sid_gr", "sid-gr"), ("sglang", "sglang"), ("vllm", "vllm")]
LENGTHS = [256, 512, 1024, 2048, 4096, 8192, 10240]
BEAMS = [32, 64, 128, 256, 512, 1024]
BATCHES = [1, 2, 4, 8]


def load():
    cells = {}
    with open(os.path.join(DATA, "all.csv"), encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["status"] != "complete":
                continue
            key = (int(r["length"]), int(r["beam"]), int(r["batch"]))
            cells[(r["backend"], key)] = float(r["batch_mean_ms"])
    return cells


def main():
    cells = load()
    rows = []
    for b in BATCHES:
        for w in BEAMS:
            for l in LENGTHS:
                key = (l, w, b)
                vals = {be: cells.get((be, key)) for be, _ in FRAMES}
                rows.append(
                    {
                        "config": f"bw{w}_in{l}_b{b}_U",
                        "length": l,
                        "beam": w,
                        "batch": b,
                        "mode": "U",
                        **{name: ("" if v is None else f"{v:.1f}") for (be, name), v in
                           zip(FRAMES, [vals[be] for be, _ in FRAMES])},
                        "sid-gr/gr": "" if not (vals["sid_gr"] and vals["gr"])
                        else f"{vals['sid_gr'] / vals['gr']:.3f}",
                        "sglang/gr": "" if not (vals["sglang"] and vals["gr"])
                        else f"{vals['sglang'] / vals['gr']:.3f}",
                        "vllm/gr": "" if not (vals["vllm"] and vals["gr"])
                        else f"{vals['vllm'] / vals['gr']:.1f}",
                    }
                )

    cols = ["config", "length", "beam", "batch", "mode",
            "vllm-gr", "sid-gr", "sglang", "vllm",
            "sid-gr/gr", "sglang/gr", "vllm/gr"]
    out = os.path.join(DATA, "wide-168.csv")
    with open(out, "w", encoding="utf-8", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=cols)
        wr.writeheader()
        wr.writerows(rows)
    print(f"wrote {out}: {len(rows)} rows")

    md = [
        "# 逐配置横向对比表（批延迟 ms，越小越好）",
        "",
        "> 口径：BF16、TP=1、`gpu_memory_utilization=0.6`、`eos_policy=fixed3`、`rounds=3` × 16 批。"
        "数值 = `batch_mean_ms`。",
        "> 配置命名：`bw{beam}_in{length}_b{batch}_U`。",
        "> 比值列 = 该框架 ÷ vllm-gr 同点；> 1 表示比 vllm-gr 慢。",
        "> vLLM 只覆盖 **batch 1 / beam 32–256**；168 点全矩阵按实测斜率外推需约 510 GPU·小时，未跑。",
        "",
        f"完整机器可读版：`data/wide-168.csv`（{len(rows)} 行）。",
        "",
    ]
    for b in BATCHES:
        sel = [r for r in rows if r["batch"] == b]
        md += [
            f"## batch = {b}（{len(sel)} 个配置）",
            "",
            "| 配置 | vllm-gr | sid-gr | sglang | vllm | sid-gr/gr | sglang/gr | vllm/gr |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for r in sel:
            md.append(
                f"| {r['config']} | {r['vllm-gr'] or '—'} | {r['sid-gr'] or '—'} | "
                f"{r['sglang'] or '—'} | {r['vllm'] or '—'} | {r['sid-gr/gr'] or '—'} | "
                f"{r['sglang/gr'] or '—'} | {r['vllm/gr'] or '—'} |"
            )
        md.append("")

    mdp = os.path.join(HERE, "report-wide-20261031.md")
    with open(mdp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md))
    print(f"wrote {mdp}")


if __name__ == "__main__":
    main()
