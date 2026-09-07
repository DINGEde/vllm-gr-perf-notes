# Beam Search CPU 后处理时延分析与优化

> 本文总结 vllm-gr 对 beam search 前端 CPU 后处理阶段（`decode_overhead`）的
> 时延归因与第一项落地优化（batch detokenize）。目标是：**用低开销、可闭合归因
> 的测量手段把 CPU 后处理的时间来源钉死，并据此消除掉最显著的一块时间**。
>
> 配套度量口径见 [`benchmark/beam_phase_metrics.md`](../benchmark/beam_phase_metrics.md)，
> 本文的分析粒度在其「decode 名义上已含 sort/reconstruct/detokenize」的基础上再
> 往下拆一层。

---

## 0. 概述（TL;DR）

- 用 `time.perf_counter_ns()` 互斥分桶 + ABBA A/B，把 beam search 前端 CPU 后处理
  `decode_overhead`（≈ 19 ms）分解到 11 个互斥子桶，**闭合残差仅 0.2 ms（1.1%）**。
- 定位到最大单项：末尾 128 束最终重构里的 `tokenizer.decode`（**6.34 ms**），
  以及决策阶段 `_worker_decision_flatten` 的 Python 层展开（**5.14 ms**）。
- 落地 **优化 A**：把 128 次 `tokenizer.decode` 合并为一次 batch decode，
  `detokenize` 6.34 → **1.31 ms**（5.1×）。过程中定位并修复了一个由 batch 化
  新引入的 **Python cyclic-GC 停顿**（~2.8 ms）。
- 最终 finalize 阶段 8.4 → **3.6 ms**（**净省 ~4.8 ms / −57%**），且比旧代码更稳定。

---

## 1. 背景与出发点

### 1.1 场景

vllm-gr 是给 vLLM 增加 catalog-constrained beam search 的 fork，面向推荐系统
（OneRec / OneRec-1.7B）。典型负载：

| 参数 | 值 |
|---|---|
| `beam_width` | 128 |
| prompt（SID 序列） | 1024 token |
| `max_tokens` | 5（短生成，`decode_steps` 实测仅 2） |

推荐场景的两个特点决定了 CPU 侧开销不可忽略：

1. **beam_width 大**：很多前端操作被 128 束放大；
2. **单请求时延敏感**：`decode_overhead` ≈ 19 ms，占 decode 的 ~39%、e2e 的 ~20%。

### 1.2 前序 A/B 结论与要解决的问题

前序工作做了 `beam_engine_driven`（engine-driven chaining）A/B，结论是：

- engine-driven 相比 frontend-driven 省 ~6 ms e2e；
- 但收益**几乎全部来自 `engine_decode` 的空闲等待消除**；
- CPU 侧后处理 `decode_overhead` 几乎不变（~18.5–19 ms），成为新的主要瓶颈。

此前对 CPU 开销的归因是**模糊且偏乐观**的：存在 ~10 ms 的「未归类残差」、
D2H 重复同步被高估。因此需要一种**低开销、可闭合归因**的测量，先把瓶颈钉死，
再谈优化。

---

## 2. 方法

### 2.1 低开销互斥分桶 instrumentation

不用重量级 PyTorch profiler，改用 `time.perf_counter_ns()` + `mock.patch.object`
对前端 `beam_search` 路径**非侵入式**打点，拆成**互斥、不可重叠**的子桶：

| 阶段 | 子桶 |
|---|---|
| finalize | `finalize_logprobs` / `finalize_detokenize` / `finalize_objects` |
| 循环内 | `cpu_prepare` / `cpu_decision` / `cpu_array_convert` / `cpu_eos` / `cpu_topk` / `cpu_materialize` / `cpu_cleanup` |
| 决策细分 | `decision_extract` / `decision_unpack` / `decision_flatten` |
| 入口细分 | `entry_preprocess` / `entry_deepcopy` / `entry_instance_init` / `entry_prompt_decode` |

以 `decode_overhead` 为**父桶**做闭合校验：

```
decode_overhead ≈ Σ(所有互斥子桶) + cpu_glue（未归类胶水）
```

残差即为测量误差；残差小则分桶可信。

### 2.2 ABBA 实验设计

`true → false → false → true` 四轮、每轮 50 样本，pooled 对比 engine-driven 开关，
控制顺序/温度/负载混淆。基准：`beam_width=128, input_length=1024, max_tokens=5`。

---

## 3. 实验结果与分析（后处理阶段时间来源）

### 3.1 前序 A/B：engine-driven 收益在 engine_decode，不在 CPU 后处理

pooled true（2×50）vs false（2×50），p50：

| metric | true | false | delta |
|---|---|---|---|
| `e2e_ms` | 96.98 | 102.97 | **−5.99** |
| `engine_decode_ms` | 29.75 | 35.54 | **−5.80** |
| `decode_overhead_ms` | 19.15 | 19.58 | −0.43 |

**分析**：chaining 的收益集中在 `engine_decode`（空闲等待消除），CPU 后处理
`decode_overhead` 几乎不受益（−0.43 ms）。因此「engine-driven」本身不能解决
CPU 后处理开销，必须单独优化——这正是本文的目标。

### 3.2 `decode_overhead` 完整分解（ABBA true，pooled 100 样本，p50）

| 子桶 | p50 (ms) | 占比 | 归因 |
|---|---|---|---|
| `cpu_finalize_detokenize` | **6.337** | 33% | 128× `tokenizer.decode` |
| `cpu_decision` | **5.137** | 27% | 决策处理（见 3.3） |
| `cpu_materialize` | 2.177 | 11% | `parent.tokens + [tid]` 全量拷贝 prompt |
| `cpu_finalize_logprobs` | 2.057 | 11% | `reconstruct_beam_logprobs` |
| `cpu_glue` | 1.665 | 9% | 未归类循环胶水 |
| `cpu_prepare` | 0.986 | 5% | step request 准备 |
| 其余（array_convert/eos/cleanup/sort/objects/topk） | ~0.57 | 3% | 小项 |
| **Σ partition + glue** | **~18.9** | | **闭合残差 0.2 ms（1.1%）** |

**分析**：之前 ~10 ms 的「未归类残差」被完整钉死为**末尾 128-beam 最终重构**
（detokenize 6.34 + logprobs 2.06），**不是**循环胶水（glue 仅 1.67 ms）。这是
本次优化能精准落地的关键。

### 3.3 决策细分：瓶颈是 flatten 的 Python 层，不是 unpack

| 决策子桶 | p50 (ms) |
|---|---|
| `decision_extract`（取 buffer） | 0.010 |
| `decision_unpack`（`struct.unpack`） | **0.056** |
| `decision_flatten`（总） | **5.133** |
| └ flatten − unpack（Python 层） | **5.076** |

**分析**：`struct.unpack` 仅 0.056 ms，决策开销几乎全部在
`_worker_decision_flatten` 的 Python 层展开（3 次冗余 list comprehension +
128 嵌套 tuple cache 构建）。这修正了此前「D2H 重复同步 / 解包是瓶颈」的偏乐观
归因——解包不是瓶颈，Python 展开才是。

### 3.4 入口细分

| 入口子桶 | p50 (ms) |
|---|---|
| `entry_preprocess`（`_preprocess_cmpl`） | 4.453 |
| `entry_prompt_decode` | 1.203 |
| `entry_deepcopy` | 1.007 |
| `entry_other` | 0.293 |
| `entry_instance_init` | 0.006 |
| **beam_entry_overhead 合计** | **6.989** |

入口开销是一次性的（每请求一次），在短生成场景下占 e2e 比例可观；`_preprocess_cmpl`
（tokenization + prompt 处理）是其中大头。

### 3.5 tokenizer 微基准与正确性验证

`self.get_tokenizer()` 返回的是 `Qwen2Tokenizer`（fast / Rust `TokenizersBackend`）。
在容器内微基准：

| 调用方式 | 耗时 |
|---|---|
| 128 次独立 `decode` | 7.642 ms（59.7 µs/次） |
| 1 次 `decode(list_of_lists)` | **1.487 ms**（11.6 µs/束） |

正确性：对同一批 token（含变长、空 beam），`decode(list_of_lists)` 与逐条 `decode`
产出**逐字节一致的字符串**，因此 `beam.text` 不变。

### 3.6 优化 A 落地与 GC 交互定位（实验时间线）

| 实验（样本数） | detokenize | logprobs | 结论 |
|---|---|---|---|
| OLD 基线（ABBA 100） | 6.34 | 2.06（双峰） | 基线 |
| NEW batch（10） | 1.34 | ~4.8（全慢） | ⚠️ logprobs 异常上涨 |
| OLD 回滚复测（10） | 6.31 | 2.05（双峰） | 确认是代码改动所致 |
| **NEW + `gc.disable()`（10）** | **1.31** | **2.26（稳定）** | ✅ 修复 |

**分析**：OLD 代码里 logprobs 是双峰分布 `[2.0, 2.15, 4.84, 2.04, 5.25, 1.98, 2.03,
2.04, 5.54, 2.05]`（大多 ~2 ms、偶发 ~5 ms）；batch 化后变成稳定 ~5 ms。定位为
**Python cyclic-GC 停顿**：batch 去掉了每束的 Rust `decode`（原本释放 GIL、摊薄
分配），128 次 `reconstruct_beam_logprobs` 背靠背分配 ~900 个对象，触发 gen0 GC。

---

## 4. 优化达成：具体消除了哪一块时间

### 4.1 batch detokenize 消除了 per-call Python 开销（~5 ms）

**改动前**（`vllm_gr/entrypoints/gr.py`，`beam_search` finalize 段）：

```python
for beam in best_beams:
    reconstruct_beam_logprobs(beam, initial_logprobs, sid_end_token_id)
    gen_tokens = beam.tokens[prompt_len:]
    beam.text = prompt_text + tokenizer.decode(gen_tokens)   # 128 次独立调用
```

每个 beam 都触发一次独立的 `tokenizer.decode`。虽然 `Qwen2Tokenizer` 是 Rust
backend，但每次调用仍要经过 transformers 的 `decode()` → `TokenizersBackend.decode()`
→ special-token 处理 → `clean_up_tokenization_spaces` 判断等 **Python 层封装**，
实测 **59.7 µs/次**。真正 Rust 解码 5 个 token 本身只需几 µs，剩余全是 per-call
wrapper 开销。128 次累计 = 6.34 ms。

**改动后**：

```python
decoded_gen_texts = tokenizer.decode(
    [beam.tokens[prompt_len:] for beam in best_beams]     # 1 次批量调用
)
for beam, gen_text in zip(best_beams, decoded_gen_texts):
    beam.text = prompt_text + gen_text
```

128 次 Python→Rust 往返压成 1 次，走 Rust 原生批量解码路径，**11.6 µs/束**。
**消除的时间 = 128 次独立调用的 per-call wrapper + 参数/特殊 token 处理开销**，
即 6.34 → 1.31 ms，省 ~5 ms。

> 说明：`BeamSearchParams` 没有 `n` 字段（返回 sequence 数恒等于 `beam_width`），
> 所以「只 detokenize 返回的 top-n 条」这个方案在本 fork 不适用；batch decode
> 是对任意 `beam_width` 都通用的方案。

### 4.2 `gc.disable()` 消除了 batch 化新引入的 GC 停顿（~2.8 ms）

batch 化把 `reconstruct_beam_logprobs` 的 128 次调用变成背靠背执行，每束分配
`FlatLogprobs`（6 个 list）+ `steps` list ≈ 7 个对象，共 ~900 个对象，触发 Python
gen0 cyclic GC（阈值 ~700），每次 GC 停顿 ~1–2 ms，累计把 `finalize_logprobs`
从 2.05 ms 推到 ~5 ms。

该路径**没有引用环**（`BeamSearchSequence._lp_parent` 是单向子→父指针，
`FlatLogprobs` 是纯 list），对象本可由 refcount 即时回收，cyclic GC 是纯开销。
因此在 reconstruct 循环外包一层 `gc.disable()/gc.enable()` 消除停顿：

```python
gc_was_enabled = gc.isenabled()
gc.disable()
try:
    for beam in best_beams:
        reconstruct_beam_logprobs(beam, initial_logprobs, sid_end_token_id)
finally:
    if gc_was_enabled:
        gc.enable()
```

**消除的时间 = batch 化后新引入的 gen0 GC 停顿**（~2.8 ms），`finalize_logprobs`
回到 2.26 ms，且比旧代码的双峰分布（偶发 5 ms）更稳定。

---

## 5. 收益汇总

| 指标 | 优化前 | 优化后 | 收益 |
|---|---|---|---|
| `cpu_finalize_detokenize_ms` | 6.34 | **1.31** | **−5.0 ms（5.1×）** |
| `cpu_finalize_logprobs_ms` | 2.05（双峰，偶发 5ms） | **2.26（稳定）** | 消除偶发尖峰 |
| **finalize 合计** | **8.36** | **3.57** | **−4.8 ms（−57%）** |

正确性：`decode(list_of_lists)` 与逐条 `decode` 字符串逐字节一致，`beam.text`
与 `logprobs` 均不变；改动通过 offline benchmark（128 beams 正常返回）。

---

## 6. 可提交内容（PR）

**核心代码改动**：

- `vllm_gr/entrypoints/gr.py` — `beam_search` finalize 段 batch detokenize +
  GC 控制（净省 ~4.8 ms/请求）。

**实验基础设施**（可随附或单独 PR）：

- `tools/daily_benchmark/run_offline_benchmark.py` — 互斥分桶 instrumentation +
  ABBA 框架，是后续 B/C/D/E 优化的通用测量基座。

---

## 7. 后续方向（已用数据钉死优先级）

| 优先级 | 目标 | p50 (ms) | 说明 |
|---|---|---|---|
| B | `_worker_decision_flatten` 向量化/零拷贝 | 5.14 | 长生成长尾最大头 |
| C | `materialize_selected_beams` 结构共享 | 2.18 | 避免全量拷贝 prompt |
| D | `reconstruct_beam_logprobs` 只重建返回 beam | 2.06 | 短生成可再省 |
| E | 循环下沉 C/Rust | 1.67 | glue，工程量大 |

> 关键认知：本基准是**短生成（decode_steps=2）+ 全量返回（128 束）**，所以
> finalize（detokenize+logprobs，一次性）显得最大；真实部署里 finalize 随
> 返回 beam 数线性缩、per-step（decision/materialize/glue）随生成长度线性放大，
> 长生成场景下 B/C/E 才是长尾。

---

## 附录：实验数据

基准：`OpenOneRec/OneRec-1.7B`，`beam_width=128, input_length=1024, max_tokens=5`。
原始 JSON 在服务器 `~/vllm-gr-decode-graph/results/analysis/`：

| 目录 | 内容 |
|---|---|
| `abba-{1..4}-{true,false}/raw-result.json` | ABBA 四轮（每轮 50 样本） |
| `smoke-batchdecode10/` | NEW batch（10 样本） |
| `revert-old10b/` | OLD 回滚复测（10 样本） |
| `new-gc10/` | NEW + `gc.disable()`（10 样本） |

分析脚本：服务器 `/tmp/analyze_abba.py`（纯 stdlib，pooled true/false + bootstrap CI）。
