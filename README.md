# vllm-gr 性能实验笔记

OneRec / vllm-gr decode 后 CPU 性能优化的实验记录与归因分析。

## 文档

- [Beam Search Decode 性能实验总览](beam_search_decode_perf_summary.md) —— 结果展示 + 后续优化方向（含正式 AB 结果）
- [decode 后 CPU 过程归因分析](beam_search_cpu_postprocess_optimization.md) —— `decode_overhead` 逐桶分解与 GC 归因
- [CI / PR 门禁要求](ci_gate_requirements.md) —— 向 JiusiServe/vllm-gr 提 PR 的门禁与避坑

## 场景

`beam_width=128`、`input_length=1024`、`max_tokens=5`（`decode_steps=2`），模型
`OpenOneRec/OneRec-1.7B`，`attention_config={"backend": "CUSTOM"}` 走 ConstraintTable 的
worker-decision 路径。

## 核心结论速览

- **detokenize batch 化**：finalize 6.34ms → 1.31ms（5.1×）
- **`gc.collect(1)` 调度进 GPU wait 期**：decode wall time −2.66ms（−5.6%），
  decision_flatten 5.18 → 0.49ms
- 剩余 CPU 开销本质是**对象分配**（materialize 全量拷贝），优化方向见总览 §4
