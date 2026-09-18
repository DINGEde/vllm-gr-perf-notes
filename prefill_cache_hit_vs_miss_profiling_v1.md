# Prefill 缓存命中 vs 未命中 Profiling 对比（Beam Search V1）

> 单请求（B=1）prefill 阶段的 Kineto trace 对比：同一请求在**命中前缀缓存**（只重算 tail）与**未命中**（重算整段 1024 token）两种状态下，GPU 回放与端到端的时间分布。
>
> **场景**：OneRec 推荐，`beam_width=128`、`input_length=1024`、`max_tokens=5`（`generated_steps=3`），模型 `OpenOneRec/OneRec-1.7B`，`attention_backend="CUSTOM"` 走 **CUDA Constraint Table 的 worker-decision 路径**。
>
> **接口**：本笔记走 **Beam Search V1**（`GRLLM.beam_search_v1`，EngineCore 持有的异步调度，`execution_mode="v1"`），区别于旧笔记的 legacy `GRLLM.beam_search`。revision `f7724d3`（origin/decode_graph）。
>
> **口径**：`*_wall` / `*_gpu` 为 20 次采样的均值（`run.json` 的 `timing.summary_ms`，单位 ms）；`prefill_graph_replay` 的 Kineto device 时间为单次 profiled run 的 `profiler_table.txt` 记录。

## 0. 场景配置

两个场景唯一变量是 `cache_state`，其余完全一致：

| 参数 | HIT | MISS |
| ---- | --- | ---- |
| `interface` | beam_search_v1 | beam_search_v1 |
| `execution_mode` | v1 | v1 |
| `cache_state` | hit（前缀命中） | miss（每次请求前 `reset_prefix_cache()`） |
| 本次调度 token 数 | 16（tail = `((1024−1) mod 16)+1`） | 1024（整段） |
| prefill CUDA graph | `FULL bucket=16 num_reqs=1` | `FULL bucket=1024 num_reqs=1` |
| `beam_width` | 128 | 128 |
| `input_length` | 1024 | 1024 |
| `max_tokens` | 5 | 5 |
| `constraint_backend` | cuda | cuda |
| `kv_bound` | 1024 | 1024 |
| `async_scheduling` | True | True |
| 成功回放次数 | 21 / 21（全 bucket=16） | 21 / 21（全 bucket=1024） |

命中时 N=1024 命中 1008 token，只重算 tail=16；未命中时整段 1024 token 全部重算。

## 1. 核心对比数据

| 指标（ms，均值） | HIT | MISS | Δ（miss→hit） |
| ---------------- | --- | ---- | ------------- |
| `prefill_graph_replay_gpu` | 5.927 | 30.581 | **−24.65** |
| `prefill_graph_replay_wall` | 0.312 | 0.383 | −0.07（host 侧） |
| **`request_e2e_wall`** | **34.585** | **59.419** | **−24.83** |

单次 profiled run 的 Kineto CUDA 记录（`prefill_graph_replay` device 时间）：**HIT 7.093 ms vs MISS 31.040 ms**，Δ −23.95 ms，与 20 次均值（−24.65 ms）量级一致。

## 2. 与 legacy `beam_search` 的对比

prefill 回放是同一套 native model-runner 机制，所以回放 GPU 时间几乎不变；变化全在非 prefill 段（decode + frontend）：

| 指标（ms，均值） | legacy HIT | v1 HIT | legacy MISS | v1 MISS |
| ---------------- | ---------- | ------ | ----------- | ------- |
| `prefill_graph_replay_gpu` | 5.950 | 5.927 | 30.610 | 30.581 |
| **`request_e2e_wall`** | 47.896 | **34.585** | 72.010 | **59.419** |

- `prefill_graph_replay_gpu` 两者逐位接近（Δ 内 <0.03 ms），印证 prefill 回放未被 V1 改动。
- `request_e2e_wall` v1 系统性地低约 **13 ms**（hit 47.9→34.6、miss 72.0→59.4）：V1 用异步调度 + 持久化 GPU beam state，省掉了 legacy 逐 token 的 CPU 决策循环。

## 3. 结论

1. **缓存命中在 prefill 阶段省 ~24.65 ms**：`prefill_graph_replay_gpu` 30.58 → 5.93 ms，端到端 `request_e2e_wall` 59.42 → 34.59 ms，两者 Δ 一致（~24.8 ms），说明 decode 阶段几乎不受命中影响。

2. **节省全部来自 prefill 回放量级**：GPU 侧从整段 1024 token（bucket=1024）降到 tail 16 token（bucket=16）。这是命中 vs 未命中的唯一物理差异——只重算 tail 而非整段。此结论与 legacy 一致，因为回放机制未变。

3. **V1 比 legacy 在端到端快 ~13 ms**，但这一差距**与命中与否无关**（hit/miss 各约 −13 ms），来自 V1 异步调度的 decode 路径优化，而非 prefill 回放。

4. **输出语义一致**：hit/miss 的 `best_beam_sid_token_ids` 完全相同（`[156597, 165559, 168909]`），但 128-beam 全量分数 digest 有细微差异（hit `690304f1…` vs miss `386bec5c…`）——这是已知的 batch 形状相关 GEMM 数值差异（bucket=16 的 M=8 vs bucket=1024 的 M=128），不是逻辑错误。

## 4. Trace 文件

原始 Kineto chrome trace（可用 `chrome://tracing` 打开 `.pt.trace.json`）与配套产物：

- HIT：`traces/onerec_prefill_v1_hit_b1_l1024_20260918_025447/`
- MISS：`traces/onerec_prefill_v1_miss_b1_l1024_20260918_025826/`

每个目录三件套：`*.pt.trace.json`（Kineto trace）、`run.json`（场景 + timing 采样）、`profiler_table.txt`（`torch.profiler` 聚合表）。

生成脚本：`vllm-gr` 仓库 `test_profilling/profile_onerec_prefill_v1_b1_l1024.py`（`--cache-state {hit,miss}` 切换）。

> 旧版 legacy 接口的同款对比见 [prefill_cache_hit_vs_miss_profiling.md](prefill_cache_hit_vs_miss_profiling.md)。
