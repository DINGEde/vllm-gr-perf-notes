# vllm-gr 性能实验笔记

OneRec / vllm-gr decode 后 CPU 性能优化的实验记录与归因分析。

## 文档

- [Beam Search Decode 性能实验总览](beam_search_decode_perf_summary.md) —— 结果展示 + 后续优化方向（含正式 AB 结果）
- [decode 后 CPU 过程归因分析](beam_search_cpu_postprocess_optimization.md) —— `decode_overhead` 逐桶分解与 GC 归因
- [Prefill 缓存命中 vs 未命中 Profiling（Beam Search V1）](prefill_cache_hit_vs_miss_profiling_v1.md) —— 走 `beam_search_v1` 异步调度的 hit/miss Kineto trace 对比（prefill 省 ~24.65ms，端到端比 legacy 快 ~13ms）
- [Prefill 缓存命中 vs 未命中 Profiling（legacy）](prefill_cache_hit_vs_miss_profiling.md) —— 走 `beam_search` 旧接口的 hit/miss Kineto trace 对比（prefill 省 ~24ms，原始 trace 见 `traces/`）
- [CI / PR 门禁要求](ci_gate_requirements.md) —— 向 JiusiServe/vllm-gr 提 PR 的门禁与避坑

## 稳定性与长跑测试

`beam_search_v1` 会话生命周期（取消 / 暂停与恢复 / 编号跨次复用）的长时间稳定性测试，六个批次、
单次最长连续运行 5 小时，累计 352,916 次正常请求与 140.9 万次取消。结论：**未发现需要修复的缺陷**。

- [长跑稳定性测试报告](SOAK_REPORT.md) —— **正式版**，**结论前置**，11 张图（含五小时全样本时序、长尾构成归因）佐证「不泄漏 / 不阻塞 / 不漂移」，附精确统计表、测试流程与未解释项
- [长跑稳定性测试结果摘要](SOAK_SUMMARY.md) —— 一页结论：目的、设置（离线 / 单进程与多进程 / 并发 1）、结果与归因
- [长跑稳定性测试完整报告](SOAK_RESULTS.md) —— 逐批次的实现方式、逐项判据实测值与归因分析
- [长跑测试方案](SOAK_PLAN.md) —— 各批次的设计依据、判据定义与已知盲区

## 在线并发与合批

在线入口的并发请求为什么不加速、`--max-num-seqs` 为什么不生效，以及离线合批值多少。
同一 commit `d3bc50c`、同一张卡、同一 artifact digest 下的受控对照。

- [在线入口并发时延 vs 离线合批（受控对照）](ONLINE_CONCURRENCY_REPORT.md) —— 结论前置，3 张图（时延曲线 / 完成时刻阶梯 / 四做法对照）。
  结论：**在线入口恒为单执行 owner，N 个并发请求排队跑**，比离线串行还慢 7.8%；
  离线合批省 1.21×，**收益全部来自 decode（1.73×），prefill 合批反而贵 11%**；
  `--max-num-seqs` 两腿差 ≤ 2.9%，小于单腿自身轮间波动（2–23%）

## H20 离线 Beam 性能矩阵

单张 H20-3e 上的离线 beam 批延迟测量，覆盖 vllm-gr / SID-GR / SGLang / vLLM 四个框架。

- [四框架 168 点矩阵（2026-10-31 / 11-01）](h20-test-report/four-framework-20261031/README.md) —— **结论前置**：
  vllm-gr 中位最快；SGLang 慢 2.08× 且随 beam 放大到 3.96×；SID-GR 与 GR 的分界是二维的
  （短输入 + 宽 beam 时 SID-GR 反超到 0.90×）；vLLM 原生 beam search 慢 28–529×。
  含逐配置横向表、比值热图、四框架折线图与合并原始数据（`data/all.csv`，532 行）
- [H20 离线 Beam 性能实验报告（64 点，先前一轮）](h20-test-report/gr-report.md) —— 单框架、U/C 两种模式、含指标定义与计时边界

## 场景

`beam_width=128`、`input_length=1024`、`max_tokens=5`（`decode_steps=2`），模型
`OpenOneRec/OneRec-1.7B`，`attention_config={"backend": "CUSTOM"}` 走 ConstraintTable 的
worker-decision 路径。

## 核心结论速览

- **detokenize batch 化**：finalize 6.34ms → 1.31ms（5.1×）
- **`gc.collect(1)` 调度进 GPU wait 期**：decode wall time −2.66ms（−5.6%），
  decision_flatten 5.18 → 0.49ms
- 剩余 CPU 开销本质是**对象分配**（materialize 全量拷贝），优化方向见总览 §4
