# H20 四框架 Beam 离线矩阵（2026-10-31 / 11-01）

单张 H20-3e，BF16、TP=1、`gpu_memory_utilization=0.6`、`eos_policy=fixed3`（每序列固定生成 3 token，
忽略 EOS）、`rounds=3` × 每轮 16 批。长度 256/512/1024/2048/4096/8192/10240 × beam
32/64/128/256/512/1024 × batch 1/2/4/8。

## 结论

1. **vllm-gr（`decode_graph` @ `33a1fdb`）在三家全矩阵上中位最快**：SGLang 中位 2.08×（1.23–5.45×，
   168/168 点全慢）；SID-GR 中位 1.03×（0.89–1.10×，50/168 点更快）。
2. **SGLang 的劣势随 beam 单调放大**：W=32 1.35× → W=128 1.95× → W=256 2.42× → W=512 3.25× →
   W=1024 3.96×；对输入长度几乎不敏感（2.08–2.21×），对 batch 也不敏感（2.01–2.13×）。
3. **SID-GR 与 GR 的分界是二维的**：短输入 + 宽 beam 时 SID-GR 赢（W=1024 / L≤4k 为 0.90–0.99×，
   W=512 / L≤2k 为 0.89–0.99×，W=256 / L≤512 为 0.91/0.94×），W≤128 一律落后 1.02–1.08×。
4. **vLLM 原生 beam search 慢 28–529×**，且起点就高：L=256/W=32 已是 421.9 ms（vllm-gr 15.1 ms），
   L=10240/W=256 达 184.9 s。这是 Python 层逐 beam 展开的路径，不代表 vLLM 的 GPU 能力。

## 文件

| 文件 | 内容 |
| --- | --- |
| [`01-four-framework-matrix.md`](01-four-framework-matrix.md) | **主报告**：三框架 168 点 + vLLM 28 点，含逐配置横向表、按 beam/长度/batch 汇总、比值热图、二维分界分析 |
| [`02-gr-single-framework.md`](02-gr-single-framework.md) | vllm-gr 单框架 168 点深度：长度/beam/合批的缩放律与边际成本 |
| [`03-wide-table.md`](03-wide-table.md) | 逐配置横向表全文（168 行，按 batch 分四张） |
| [`04-four-framework-45pt.md`](04-four-framework-45pt.md) | 四家可比的 45 点交集版本（含启动开销与口径披露） |
| [`data/all.csv`](data/all.csv) | 合并原始数据 532 行；`source` 列区分 `gr-168` / `sglang-168` / `sidgr-168` / `vllm-a` / `vllm-b` |
| [`data/wide-168.csv`](data/wide-168.csv) | 横向表机器可读版：行 = 配置，列 = 四框架延迟 + 三个比值 |
| `data/{gr,sglang,sidgr}-168.csv`、`data/vllm-{a,b}.csv` | 各作业的 `offline_summary.csv` 原件 |
| `figs/fig1..fig8.svg` | 全部图（SVG） |
| `scripts/` | 复现用的作业脚本与配置：`submit.sbatch`、`inside.sh`、`patch_adapter.py`（工具补丁）、四份 config JSON |
| `make_*.py` | 数据合并（`make_all_csv.py`）、横向表（`make_wide_table.py`）、出图（`make_charts_v2/3/4.py`），在包目录下运行即可重建 |

## 口径要点

- 数值列是 `batch_mean_ms`：一次完整批调用的墙钟时间，不除以 batch；包含请求准备、调度/IPC、
  prefill、decode、beam 决策、D2H 与结果物化，不含 tokenizer、引擎初始化与预热。
- `U` 为全词表无约束模式；本矩阵只跑 U。
- `status=complete` 表示流程完成且输出结构检查通过，不等于精度验收。
- vLLM 只有 batch 1 / beam 32–256 的 28 个格子；全矩阵按实测斜率外推约需 510 GPU·小时，
  单点最坏约 54 小时，超出单作业 12 小时上限，因此未跑。
- vllm-gr 硬限制 `batch ≤ 8`；beam > 256 需要显式 `candidate_width = min(beam, 256)`。

## 复现

作业：vllm-gr `4151`（1:35:05）、SID-GR `4218`、SGLang `4221`、vLLM `4224`（5:24）/ `4225`（6:52）。
三家用同一容器镜像、同一套评测工具（vendor 补丁 `520e68c`，仅放宽 GR adapter 的 beam 上限并补
`candidate_width`）。
