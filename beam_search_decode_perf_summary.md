# Beam Search Decode 性能实验总览

> 结果展示与后续优化方向讨论文档。整合了从归因打点到 `gc.collect` 调度落地的完整实验链，附运行逻辑顺序，便于对照「代码在哪跑、时间花在哪、我们改了什么、还剩什么」。
>
> **场景**：OneRec 推荐，`beam_width=128`、`input_length=1024`、`max_tokens=5`（`decode_steps=2`），模型 `OpenOneRec/OneRec-1.7B`，`attention_config={"backend": "CUSTOM"}` 走 ConstraintTable 的 worker-decision 路径。
>
> **口径说明**：所有时间均为 `miss`（缓存未命中）样本的 p50，单位 ms。数据由 `tools/daily_benchmark/run_offline_benchmark.py` 用非侵入式 `mock.patch` 互斥分桶打点采集；`decode_overhead = decode_ms − engine_decode_ms`，闭合残差 <0.2ms。

## 0. 背景

vllm-gr 是给 vLLM 增加 catalog-constrained beam search 的 fork，面向推荐系统
（OneRec / OneRec-1.7B）。典型负载：

| 参数               | 值                                   |
| ------------------ | ------------------------------------ |
| `beam_width`       | 128                                  |
| prompt（SID 序列） | 1024 token                           |
| `max_tokens`       | 5（短生成，`decode_steps` 实测仅 2） |

推荐场景的两个特点决定了 CPU 侧开销不可忽略：

1. **beam_width 大**：很多前端操作被 128 束放大；
2. **单请求时延敏感**：`decode_overhead` ≈ 19 ms，占 decode 的 ~39%、e2e 的 ~20%。

## 1. 运行逻辑顺序

单次 `beam_search`（`vllm_gr/entrypoints/gr.py`）的执行流，按真实代码顺序：

```
beam_search() 入口 (gr.py:806)
│
├─ ① preprocess prompts → 建 BeamSearchInstance（128 束，初始化为 sid_begin token）
│     cached_prompts：decode prompt 文本一次，finalize 复用
│
├─ ② 计算 pre_calc = 1(sid_begin) + 1(sid_end) = 2
│     token_iter = range(max_tokens − pre_calc) = range(3)  # token 0=prefill, 1/2=decode
│
├─ ③ 分支：ConstraintTable → _custom_beam_search_batch（worker-decision 路径）
│         Trie / 无自定义 attention → native loop 路径（非本实验场景）
│
│   _custom_beam_search_batch (gr.py:427)  ── for token in token_iter:
│     Step 1  prepare       _prepare_beam_step_requests          0.97ms 构造 128 束下一步请求
│     Step 2  engine decode   _step_engine_and_collect_outputs   ~36ms  GPU前向  
│     Step 3  decision      _worker_decision_from_result →       5.13ms worker已 top-k 预选幸存束
│                           _worker_decision_flatten             解包 → 128 嵌套 tuple cache
│     Step 4  eos           complete_eos_candidates              0.23ms 物化 EOS 候选 → completed
│     Step 5  topk          select_top_indices                   0.00ms worker 路径跳过（≈0）
│     Step 6  materialize   materialize_selected_beams           2.18ms parent.tokens + [token_id] 全量拷贝
│     Step 7  cleanup       _beam_request_cleanup                循环结束后释放 session
│
└─ ④ finalize（循环后，一次性）
      ├─ 追加 sid_end_token_id 到活跃束
      ├─ sorted(completed) → top beam_width
      ├─ reconstruct_beam_logprobs    2.26ms（gc.disable() 包裹的紧循环）
      ├─ batch_decode                 1.31ms（优化 A：128 次 decode → 1 次 batch_decode，原 6.34ms）
      └─ BeamSearchOutput(sequences=best_beams)
```

**关键点**：

- `gc.collect(1)` 位于 `_step_engine_and_collect_outputs`（gr.py:268）的 while 循环内、`llm_engine.step()` **之前**——这是本轮优化的落地点。
- `gc.disable()/gc.enable()` 位于 finalize（gr.py:1142-1149），包住 `reconstruct_beam_logprobs` 循环——这是优化 A 配套的 GC 交互修正。

---

## 2. 时间实验时间线（按顺序）

### 实验 1 · ABBA 归因打点（2026-09-04）

用 `perf_counter_ns` 互斥分桶，4×50 样本，把 CPU 后处理 `decode_overhead`（当时 ~19ms）逐桶拆开：

| 阶段 | 耗时 | 频率 | 备注 |
|---|---|---|---|
| engine_decode | ~36ms | ×2 步 | GPU 前向，engine-driven chaining 已消除空闲等待（−5.8ms） |
| **detokenize** | 6.34ms | 一次性 | 128 次 `tokenizer.decode()` |
| **worker_decision_flatten** | 5.13ms | ×2 步 | 最大 CPU 桶 |
| reconstruct_beam_logprobs | 2.06ms | 一次性 | |
| materialize_selected_beams | 2.18ms | ×2 步 | 全量拷贝 prompt |
| loop glue | 1.67ms | ×2 步 | `sum((beams...),[])` 二次方拼接 |

**结论**：engine-driven chaining 的收益全落在 `engine_decode`（GPU 侧），CPU 后处理 `decode_overhead` 几乎不变（−0.43ms），需单独逐桶优化。因此「engine-driven」本身不能解决CPU 后处理开销，必须单独优化——这正是本文的目标。

### 实验 2 · 优化 A：detokenize batch 化（已落地，PR#333）

finalize 里 128 次 `tokenizer.decode()` 合并成 1 次 `tokenizer.batch_decode(list_of_lists)`：

- **6.34ms → 1.31ms（5.1×）**，finalize 整体 8.4ms → 3.6ms（−57%）。

- **GC 交互坑**：batch 化去掉了每束 Rust decode 释放 GIL 的摊薄，128 次 reconstruct 背靠背触发 cyclic-GC 停顿（logprobs 2.05→~5ms）。该路径无引用环，用 `gc.disable()/gc.enable()` 包住 reconstruct 循环 → 稳定 2.26ms。

- **约束**：`BeamSearchParams` 无 `n` 字段（返回恒 = beam_width），「只 detokenize n 条」不适用。

  #### 收益汇总

| 指标                         | 优化前                 | 优化后           | 收益                |
| ---------------------------- | ---------------------- | ---------------- | ------------------- |
| `cpu_finalize_detokenize_ms` | 6.34                   | **1.31**         | **−5.0 ms（5.1×）** |
| `cpu_finalize_logprobs_ms`   | 2.05（双峰，偶发 5ms） | **2.26（稳定）** | 消除偶发尖峰        |
| **finalize 合计**            | **8.36**               | **3.57**         | **−4.8 ms（−57%）** |

### 实验 3 · GC 归因：decision_flatten 的真凶是 gen0 cyclic GC

`gc.callbacks` 探针揭示：`worker_decision_flatten` 的 5.13ms 里，纯 Python 展开（unpack + list comp + cache 构造）仅 **~0.28ms**，其余 **~4.6ms 是恰好落在 flatten 里的 gen0 cyclic-GC 回收**。

- decode 阶段 GC 总计 **~7.5ms/请求**（gen0 主导，2 次/请求、单次 ~3.7ms），占 `decode_overhead`(17.2ms) 的 **44%**。
- 单次 gen0 回收异常昂贵：gen0 里堆积了引擎前向 + 128 束结果反序列化 + 前端 flatten/materialize 的短命容器对象。

### 实验 4 · 三个方案证伪（各有 benchmark 佐证）

| 方案 | 结果 | 失败原因 |
|---|---|---|
| numpy 向量化 unpack/flatten | 净 **+3ms** | 纯计算本就 0.28ms，numpy 分支引入新数组对象、改变 GC 触发时机 |
| flatten 内 `gc.disable()` | 无变化（17.21→17.13） | 只把停顿挪到下一分配点，`cpu_glue` 5.99→10.38ms |
| 循环级 `gc.disable()` | **净负**（decode 47.2→49.2ms） | 回收不再 overlap GPU wait，落到 CPU 关键路径，`engine_decode` +3.5ms |

**核心洞察**：GC 回收在 GPU wait 期是**免费**的、在 CPU 后处理关键路径是**真开销**。正确方向是「调度」回收，而非「禁用」。

### 实验 5 · GC 调度：`gc.collect(1)` 落地

在 `_step_engine_and_collect_outputs` 的 while 循环里、`llm_engine.step()` 之前加 `gc.collect(1)`（gen0+gen1），让回收与 engine-driven-chained 的 GPU 前向重叠。

- `collect(1)`（gen0+gen1）优于 `collect(0)`（只 gen0）：后者 gen1 垃圾累积到 finalize 触发回收，`cpu_glue` 涨、分布出尾部尖峰。
- 全量 `gc.collect()` 会扫整个堆（torch 张量、模型权重），不可取。

### 实验 6 · 正式 AB 实验（2026-09-07，最终数据）

ABBA 顺序（A=baseline 无 collect，B=collect(1)），4×50 prompts，每 arm 100 样本，对冲 GPU 时间漂移。结果见 §3。

---

## 3. 最终结果（正式 AB，每 arm 100 样本，miss p50）

| 指标 | baseline | collect(1) | Δ |
|---|---|---|---|
| **decode_ms**（总 wall time） | 47.253 | 44.597 | **−2.656 (−5.6%)** |
| decode_overhead_ms | 17.439 | 14.378 | −3.061 |
| decision_flatten_ms | 5.178 | 0.487 | −4.691 |
| cpu_decision_ms | 5.182 | 0.492 | −4.690 |
| decision_unpack_ms | 0.056 | 0.050 | −0.006 |
| cpu_glue_ms | 6.121 | 5.999 | −0.122 |
| cpu_materialize_ms | 2.177 | 2.201 | +0.024 |
| cpu_prepare_ms | 0.979 | 1.326 | +0.347 |
| cpu_eos_ms | 0.232 | 0.183 | −0.049 |
| cpu_finalize_logprobs_ms | 2.267 | 2.275 | +0.008 |
| engine_decode_ms | 29.318 | 30.829 | +1.510 |

**分布稳定性**（decision_flatten）：

- baseline：min 2.58 / max 7.94（GC 停顿的宽散布）
- collect(1)：min 0.47 / max 0.78（停顿彻底消失，无尖峰）

**分轮稳健性**（ABBA 设计的核心验证）：四轮 `decode_ms` p50 = 47.016 → 44.576 → 44.811 → 47.423。A 组恒 ~47、B 组恒 ~44.6，收益与时间顺序正交，排除 GPU 热漂移干扰。

**关于 `engine_decode_ms` +1.51ms 的说明**：这是**打点口径现象，非 GPU 变慢**。`gc.collect(1)` 落在 `_step_engine_and_collect_outputs` 内部，恰好被 `timed_step` mock 的计时区间覆盖，其自身 ~1.5ms CPU 时间被计入 `engine_decode` 桶。佐证：分轮 `engine_decode` 单调上升 29.1→32.3（纯时间漂移），A2（无 collect）反而最高。

**正确性**：4 轮全部 `completed=50/50`、`failed=0`、`returned_beams` 恒 128。`gc.collect` 只回收不可达 cyclic 垃圾，不改可达对象，输出逐字节一致。

---

## 4. 后续优化方向讨论（按性价比）

### ✅ 已落地

| 项 | 收益 |
|---|---|
| detokenize batch 化 | 6.34 → 1.31ms |
| reconstruct 循环 gc.disable | logprobs 2.05→~5 → 稳定 2.26ms |
| `gc.collect(1)` 调度进 GPU wait 期 | decode_ms −2.66ms，decision_flatten −4.69ms |

### 🔧 待讨论（按性价比排序）

| 优先级 | 目标 | 现耗时 | 可行性 | 依据 |
|---|---|---|---|---|
| **C** | materialize_selected_beams 结构共享 | 2.18ms | 中 | 瓶颈是 `parent.tokens + [token_id]` 每步全量拷贝 prompt（128×1024）；「共享前缀 + 增量」结构可省，但会改变 `tokens` 语义，下游 batch_decode/reconstruct 都要跟着改，重构面大 |
| **E** | loop glue 的二次方拼接 | 1.67ms | 中低 | `sum((beams...),[])` 是 O(n²) 拼接（ruff RUF017 也报），可改 `itertools.chain`/一次性 flatten；但里面混着 bookkeeping，纯收益有限 |
| **D** | reconstruct_beam_logprobs 只重建 n 条 | 2.26ms | 低 | 受限于 `BeamSearchParams` 无 `n` 字段（返回恒 = beam_width 128），改不动；且已用 gc.disable 压平双峰 |
| — | engine_decode（GPU） | ~30ms | 非本范畴 | 空闲等待已消除，剩余是纯 GPU 前向，只能靠算子/kernel 层 |

**根本方向**（已证伪向量化/零拷贝之后）：剩余 CPU 开销的本质是**对象分配**（materialize 全量拷贝、flatten 的 128 嵌套 tuple），而非 Python 计算。两条正路：① 减少 CPU 关键路径分配（共享前缀结构）；② 把不可避免的回收调度进 GPU wait 期（本轮已验证）。

---

## 5. 数据与复现

- **原始数据**（服务器）：`~/vllm-gr-worktrees/perf-worker-decision-flatten/results/analysis/opt-a/`
  
  - 正式 AB：`ab-{baseline,collect}-{1,2}.json`（每文件 50 prompts）
  - 历史归因：`raw-result-{baseline,gconly,gcprobe,loopgc,collect1,collect0}.json`
- **复现命令**（容器 `vllm-gr-opt-a`，worktree `perf-worker-decision-flatten`）：

  ```bash
  docker exec -w /opt/vllm-gr \
    -e PYTHONPATH=/opt/vllm-gr -e HF_ENDPOINT=https://hf-mirror.com \
    -e HF_HUB_DISABLE_XET=1 \
    -e LD_LIBRARY_PATH=/usr/local/cuda-13.0/compat:/usr/local/nvidia/lib64:/usr/local/cuda/lib64 \
    vllm-gr-opt-a python3 /opt/vllm-gr/tools/daily_benchmark/run_offline_benchmark.py \
    --output /opt/vllm-gr/results/analysis/opt-a/<name>.json \
    --data-dir /opt/vllm-gr/data \
    --num-prompts 50 --warmup-requests 4 --beam-width 128 --input-length 1024
  ```

- **代码改动**：`vllm_gr/entrypoints/gr.py` 仅 1 行 `gc.collect(1)` + 注释（gr.py:268）。详细 PR body 见 `docs/pr_body_gc_collect.md`。
