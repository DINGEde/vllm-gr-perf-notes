# Prefill 缓存命中 vs 未命中 Profiling 对比

> 单请求（B=1）prefill 阶段的 Kineto trace 对比：同一请求在**命中前缀缓存**（只重算 tail）与**未命中**（重算整段 1024 token）两种状态下，GPU 回放与 CPU 后处理的时间分布。
>
> **场景**：OneRec 推荐，`beam_width=128`、`input_length=1024`、`max_tokens=5`（`SID_STEPS=3`），模型 `OpenOneRec/OneRec-1.7B`，`attention_config={"backend": "CUSTOM"}` 走 ConstraintTable 的 worker-decision 路径。
>
> **口径**：`*_wall` / `*_gpu` 为 20 次采样的均值（`run.json` 的 `timing.samples_ms`，单位 ms）；`prefill_graph_replay_t0` 的 CUDA 时间为单次 profiled run 的 Kineto 记录（`profiler_table.txt`）。

## 0. 场景配置

两个场景唯一变量是 `cache_state`，其余完全一致：

| 参数 | HIT | MISS |
| ---- | --- | ---- |
| `cache_state` | hit（前缀命中） | miss（每次请求前 `reset_prefix_cache()`） |
| 本次调度 token 数 | 16（tail = `((1024−1) mod 16)+1`） | 1024（整段） |
| prefill CUDA graph | `FULL bucket=16 num_reqs=1` | `FULL bucket=1024 num_reqs=1` |
| `beam_width` | 128 | 128 |
| `input_length` | 1024 | 1024 |
| `max_tokens` | 5 | 5 |
| `constraint_backend` | constraint_table | constraint_table |
| `kv_bound` | 1024 | 1024 |
| `async_scheduling` | True | True |
| 成功回放次数 | 21 / 21（全 bucket=16） | 21 / 21（全 bucket=1024） |

命中时 N=1024 命中 1008 token，只重算 tail=16；未命中时整段 1024 token 全部重算。

## 1. 核心对比数据

| 指标（ms，均值） | HIT | MISS | Δ（miss→hit） |
| ---------------- | --- | ---- | ------------- |
| `prefill_graph_replay_gpu` | 5.950 | 30.610 | **−24.66** |
| `beam_prefill_postprocess_wall` | 5.179 | 29.976 | **−24.80** |
| `beam_worker_decision_wall` | 5.186 | 29.982 | **−24.80** |
| `sample_tokens_wall` | 5.573 | 30.355 | −24.78 |
| `execute_model_gpu` | 4.527 | 16.741 | −12.21 |
| `frontend_engine_step_collect_wall` | 11.203 | 35.468 | −24.27 |
| **`prefill_window_wall`** | **14.841** | **39.052** | **−24.21** |
| **`request_e2e_wall`** | **47.896** | **72.010** | **−24.11** |
| `frontend_materialize_beams_wall` | 2.625 | 2.755 | −0.13（噪声内） |

单次 profiled run 的 CUDA 记录（`prefill_graph_replay_t0`）：**HIT 7.034 ms vs MISS 31.051 ms**，Δ −24.02 ms，与 20 次均值一致。

## 2. 结论

1. **缓存命中在 prefill 阶段省 ~24 ms**：`prefill_window_wall` 39.05 → 14.84 ms，端到端 `request_e2e_wall` 72.0 → 47.9 ms，两者 Δ 一致（~24.1 ms），说明 decode 阶段几乎不受影响。

2. **节省全部来自 prefill 回放量级**：GPU 侧 `prefill_graph_replay_gpu` 从整段 1024 token（bucket=1024）降到 tail 16 token（bucket=16），30.6 → 6.0 ms。这是命中 vs 未命中的唯一物理差异——只重算 tail 而非整段。

3. **CPU 侧 `beam_prefill_postprocess` / `beam_worker_decision` 的 wall 跟随 GPU 减少**（30 → 5.2 ms）：这两个 host 函数内部存在等待 GPU prefill 完成的同步点，其 wall 几乎等于 `prefill_graph_replay_gpu`，因此不是独立的 CPU 优化项，而是「GPU 快，同步等得就短」。

4. **decode 阶段（`frontend_materialize_beams` ~2.6 ms）命中/未命中一致**，符合预期——该阶段消费的是 prefill 产出的 logits，与是否命中前缀缓存无关。

## 3. Trace 文件

原始 Kineto chrome trace（可用 `chrome://tracing` 打开 `.pt.trace.json`）与配套产物：

- HIT：`traces/onerec_prefill_hit_b1_l1024_20260917_154639/`
- MISS：`traces/onerec_prefill_miss_b1_l1024_20260917_161744/`

每个目录三件套：`*.pt.trace.json`（Kineto trace）、`run.json`（场景 + timing 采样）、`profiler_table.txt`（`torch.profiler` 聚合表）。

生成脚本：`vllm-gr` 仓库 `test_profilling/profile_onerec_prefill_full_b1_l1024.py`（`--cache-state {hit,miss}` 切换）。
