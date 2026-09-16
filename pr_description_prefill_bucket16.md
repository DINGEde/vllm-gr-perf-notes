# PR 描述:让 cache-hit prefill 走形状恒等的 bucket-16/1 图,并支持放宽 KV bound

> 状态:中文草稿,待作者审定后转英文。
> 代码改动:`vllm_gr/v1/worker/prefill_graph_common.py`(网格 + KV bound env)、
> `vllm_gr/v1/worker/gpu_prefill_graph_runner.py`(bound 应用)。
> 配套工具改动(可独立提交):`tools/AB_Tests/ab_runner.sh` + `benchmark.env.example`
> 的 `ENGINE_ENV` 注入钩子。
> 前情完整记录:`docs/prefill_graph_hit_replay.md` §1–§7。

---

## 1. 出发点与动机

### 1.1 问题:cache-hit 的 prefill 步拿不到图

OneRec beam-search 离线场景里,同一个 prompt 只会被完整 prefill 一次(miss),后续请求
全部命中 prefix cache(hit)。命中步只调度未缓存的尾部,长度由 block size 决定:

```
scheduled = ((N - 1) mod 16) + 1        # canonical N=1024 时恒为 16
```

而 prefill CUDA graph 的 bucket 由**本步调度 token 数**决定、attention 的
`max_seqlen_k` 在捕获期按 `2 × bucket` 烧死。于是命中步必然落进最小 bucket(128),
其 bound 为 256,而真实 KV 长度是整个上下文(≈1008)——**回放门控拒绝,回落 eager**。
这道门本身是正确的(防止 attention 读到烧死上界之外的 key 而被截断),但它把长上下文
的 cache-hit 全部挡在图外。

### 1.2 已知两条出路各有硬伤(先前实验已量化)

| 路线 | 结果 | 硬伤 |
|---|---|---|
| 放宽 bound + 现有 bucket-128 图 | 命中步回放跑 M=128(112 行填充),eager 只跑 M=16 | ① 多付 ~4 ms GEMM 算术(316 GFLOP),抵消图省的开销,中位数收益落噪声内;② 该模型的 `F.linear` 在本机不是 M 不变的(cuBLAS 按形状选核),M=128 与 M=16 输出低阶位不同,beam top-k 放大成不同序列——与 eager 基准不再逐位一致 |
| `VLLM_BATCH_INVARIANT=1` | 恢复逐位一致 | 实测约 +30~40% 时延,不可作默认 |

### 1.3 关键观察与上游印证

两个硬伤**同源于一个形状选择**:bucket-128 的图对 16-token 命中步宽了 8 倍。而
canonical 场景尾长恒为 16,存在"形状恒等"方案——补一张 **bucket-16 图**,让命中步
以 M=16 回放,与 eager 的 M=16 形状完全相同(与 miss 步两侧 M 同为 1024 而逐位一致
是同一机制,不依赖 cuBLAS 巧合)。

上游 vLLM 的默认 capture sizes 网格(`[1,2,4] + range(8,256,8) + range(256,max,16)`)
印证此方向:小尺寸密集覆盖是工业默认,16 精确在网格中。vllm-gr 现网格
`range(128, 8192, 128)` 恰好把高价值小 batch 区间漏成 128 一档,而对收益有限的大
prefill(miss 1024 步图化实测只值 0.6 ms)全覆盖——本 PR 把网格补齐到命中步真正
需要的形状。

## 2. 开发方案

### 2.1 改动一:默认网格补两个小 bucket(`prefill_graph_common.py`)

```python
SMALL_PREFILL_BUCKETS = (1, 16)
DEFAULT_PREFILL_BUCKETS = sorted(set(SMALL_PREFILL_BUCKETS) | set(range(128, 8192+1, 128)))

def get_default_buckets():
    ...
    small = {b for b in SMALL_PREFILL_BUCKETS if b <= max_bucket}
    return sorted(small | set(range(step, max_bucket + 1, step)))
```

设计决策:

- **为什么是 {1, 16} 这么少的两个**(而不是上游的 1,2,4,8,16,24,…):命中尾长
  分布是 `((N-1) mod 16)+1 ∈ [1,16]`。实验 ① 实证 `F.linear` 对 M∈[2,16] 全部与
  M=16 同核(同一 cuBLAS kernel),所以**一张 bucket-16 图即可覆盖尾长 2–16**;
  M=1 走独立 gemv 核,必须由 bucket-1 精确承接。每多一张图都是捕获时间与显存,
  最小集合就是 {1,16}。
- **路由零新代码**:现有 `match_bucket`(最小可容纳 bucket)自然完成——尾长 1 →
  bucket 1,尾长 2–16 → bucket 16,17–128 维持现状(落 128),miss 步(bucket≥1024)
  完全不变。不需要 exact-match 门控:两种命中形状要么精确匹配(16、1),要么实证
  同核(2–15)。
- **env 覆盖语义保留**:`VLLM_GR_PREFILL_GRAPH_BUCKET_STEP/MAX_BUCKET` 仍生效;配置了
  刻意很小的 `max_bucket` 时,超出的 small bucket 自动滤除(`b <= max_bucket`),
  覆盖配置的原意。
- 兼容性:bucket=1/16 的 buffer 分配、捕获循环(`bucket >= num_reqs`)、LUT 均无
  特殊分支;NPU 与 GPU 共用该网格,NPU 侧小 bucket 捕获未经本 PR 验证,失败时按
  现有 per-bucket try/except 兜底回落 eager,不影响其余 bucket。

### 2.2 改动二:KV bound 放宽开关(`VLLM_GR_PREFILL_GRAPH_KV_BOUND`)

小 bucket 图的默认 bound(2×16=32 / 2×1=2)放不进真实上下文(kvlen≈1008),该开关
是改动一的前置依赖。语义:

```
bound = max(2 * bucket, VLLM_GR_PREFILL_GRAPH_KV_BOUND)     # 只增不减
```

- **捕获期一处计算、两处写入**:`meta.max_seq_len`(被 FA2 烧成 `max_seqlen_k`)
  与 `buf.max_seq_len_bounds[num_reqs]`(回放前门控读的账面值),二者始终一致,
  不会出现"门放行但图内截断"的裂缝;
- **只增不减**:`2×bucket ≥ B` 的现有图(如 bucket 1024 的 bound 2048)捕获参数
  不变,miss 路径字节一致;
- **安全门控保持不变**:回放前 `max_kvlen > bound` 依然回落 eager——本 PR 放宽的是
  捕获期上界,不是移除防护;超过 bound 的请求(如更长的上下文)照旧安全回落;
- **默认关闭**:不设 env 时行为与旧版完全一致(实验 ④ 轮 1 验证)。

### 2.3 不做什么(与备选方案的对比)

- **不迁移上游 piecewise/breakable cudagraph**:attention 不进图可以从结构上消掉
  bound 问题,但 `BeamAttentionImpl` cascade 与 beam GPU 原语深度定制,改造面大;
  且本 PR 的痛点(16-token 小 batch 的 Python/launch 固定开销)恰恰需要整个 forward
  进图,piecewise 保留 28 次 attention 段 eager 调用对小 batch 反而多付开销。
  列为长期方向,不在本 PR。
- **不改默认 bound 值**:bound 放宽后图内 attention 上界变大,对大图回放的性能影响
  目前只有 digest 证据、无重复计时数据;保守起见走显式 env,默认行为零变化。

### 2.4 decode 链路零影响

捕获顺序为 vLLM 原生图 → beam decode FULL 图 → prefill 图(`capture_model` patch
链),prefill 图后捕获且共享 graph pool(pool 共享只做内存块复用,不改写已捕获图);
运行期 `_model_forward` 拦截显式排除 beam-decode 与普通 decode batch
(`is_prefill_phase_batch`),decode 的 dispatch 不经过 prefill runner。

## 3. 实验过程与结果

### 3.0 统一环境与口径

| 项 | 值 |
|---|---|
| 硬件 | NVIDIA L20 46GB(sm_89),容器 `vllm-gr-benchmark-gpu1`,固定 GPU 1 |
| 软件 | torch 2.11.0+cu130、vllm 0.22.1、triton 3.6.0、bf16、`allow_bf16_reduced_precision_reduction` 保持引擎默认 True |
| 代码基线 | 实验 ①–③:decode_graph,改动经运行时猴补丁注入(`PROBE_EXTRA_BUCKETS`/`PROBE_KV_BOUND`),服务器源码保持 stock;实验 ④:真实 commit(见该节) |
| 负载 | canonical AB Test 配置:bw128-in1024、fcfs、`gpu_memory_utilization 0.90`、`max_num_seqs 1024`、`max_num_batched_tokens 16384`、`max_model_len 8192`、constraint_table、CUSTOM attention、`max_tokens=5`、单请求并发 |
| 采样 | 计时轮 `--num-prompts 100 --warmup-requests 10`;digest 轮 `10/2`(只比相等,不计时) |
| 干扰控制 | 共用机器:每次启动前连续 3 次轮询 GPU1 `memory.used < 200 MiB`;计时与 digest 分轮;AB 工具自带 A-B-B-A 与空闲门禁 |
| 每请求步构成 | 110 miss + 100 hit(每 prompt 恰好 1 次 miss prefill + 1 次 hit prefill,由探针计数器逐轮核实,非推算) |

实验 ①–③ 的臂定义:

| 臂 | 配置 | 命中步行为 |
|---|---|---|
| A | 图开,默认 bound(= stock) | 回落 eager |
| B1 | + `KV_BOUND=1024` | 走 bucket-128 图(M=128,112 行填充) |
| B2 | + `KV_BOUND=1024` + 网格 {1,16} | 走 bucket-16 图(M=16,零填充) |
| C | 图全关 | 纯 eager 基准 |

### 3.1 实验 ①:M∈[1,16] 同核矩阵(微基准,决定网格设计)

**目的**:bucket-16 图以 M=16 运行 GEMM,eager 命中步以 M=tail(1–15)运行。对每个
tail,各 GEMM 形状的输出是否逐位一致?这决定网格最小集合与是否需要门控。

**设计**(`test_profilling/prefill_gate_probe/micro_m_matrix16.py`):

- 形状:4 个 decoder 投影(qkv 2048→4096、o 2048→2048、gate_up 2048→12288、
  down 6144→2048)+ **lm_head 2048→176384**(其 M 直接决定 logits,beam top-k 对
  1 ulp 敏感,必须纳入);
- 矩阵:参考 = `F.linear(x[:tail])`(eager 行为,tail=1..15);测试 = `F.linear(x[:M])`
  截取前 tail 行,与参考逐位比较;M∈{16, 128},**M=128 是阳性对照**(先前实验已证其
  分叉,若本次意外一致则环境变化、旧结论需重审——环境一致性检查);
- 稳健性:2 个随机种子 × 每组合 3 次重复调用(先验证同形状位稳定,比较才有意义);
- 纪律:不动任何数值开关,复现引擎的 cuBLAS 环境。

**结果**(产物 `results/matrix_m16_0915.log`):

| 比较 | 结果 |
|---|---|
| M=16 vs 尾长 2–15 | **5 形状 × 15 尾长 × 2 种子全部逐位一致** |
| M=16 vs 尾长 1 | **全部形状分叉**(M=1 走 gemv 点积核,归约顺序不同) |
| M=128 阳性对照 | 4/5 形状分叉、o_proj 例外——与先前根因分析完全吻合,环境一致性通过 |
| 同形状重复调用 | 0 次不稳定 |

**分析与结论**:M∈[2,16] 共享同一 cuBLAS kernel、M=1 独立——所以**网格 {1,16}
即可覆盖全部 16 种命中尾长**,不需要 {1,2,4,8,16} 五张图,更不需要 exact-match
门控;唯一硬约束是尾长 1 必须路由到 bucket-1(实验 ② 端到端复证)。同核性是
"本机 torch 2.11.0+cu130 / L20 快照"的实证而非结构保证,见 §4 数值契约。

### 3.2 实验 ②:端到端 digest(数值验证)

**目的**:在真实引擎上验证 B2 的输出与纯 eager 逐位一致(核心判据),并端到端
复证尾长 1 的路由必要性。

**设计**:`PROBE_LIGHT=0`(完整探针:端到端输出 sha256 + 逐步 `_model_forward`
真实区域哈希 + KV 指纹),10 prompts/2 warmup,digest 与计时分轮(哈希开销会污染
计时)。N 取 1024 与 1025 两个长度,原因是**分叉源必须解耦**:

- N=1024:miss M=1024 = bucket 1024(**精确匹配**)→ 若 B2==C,分叉不可能来自 miss
  步,判据干净;
- N=1025:miss M=1025 vs bucket 1152(**有 padding**)→ miss 步本身可能与 eager 分叉
  (stock 固有行为)。此时 C 不是有效基准,改用 **A 臂做基准**(A 与 B2 的 miss 步
  路径完全相同:同一张 bucket-1152 图),唯一变量是命中步——A 回落 eager,
  B2 回放。另跑一个**网格只含 16 的 B2 变体**(B2{16}),让尾长 1 强行走 bucket-16,
  专门验证"尾长 1 走 bucket-16 必分叉"。

**结果**(产物 `results/matrix_e2e_0915/`、`matrix_e2e_0915_g16only/`):

| N | 比较 | 端到端 digest | 逐步比对(66 步 `_model_forward` 哈希) |
|---|---|---|---|
| 1024 | **B2 vs C** | `68ecbec4…` == `68ecbec4…` | **逐位一致 → 核心判据成立** |
| 1025 | B2{1,16} vs A | `d04a1ee7…` == `d04a1ee7…` | **66 步全同 → bucket-1 回放与 eager 恒等** |
| 1025 | B2{16} vs A | `fbe1e954…` ≠ | 分叉 → **尾长 1 走 bucket-16 确实分叉,双 bucket 设计必要** |
| 1025 | B2 vs C | 不同 | 逐步定位:**分叉全部发生在 miss 步**(eager M=1025 vs 图 M=1152 的 stock 固有 padding 分叉),canonical(1024=128 倍数)不存在此问题 |

**分析与结论**:形状恒等路线的数值安全性拿到三层证据——精确匹配的结构保证
(N=1024 全链一致)、bucket-1 恒等(66 步全同)、门控必要性实锤(B2{16} 分叉)。
同时如实记录:stock 现状下 N 非 128 倍数的 miss 步本就与 eager 存在同类 padding
分叉,非本 PR 引入(§4 声明)。

### 3.3 实验 ③:交错计时(猴补丁口径,行为与窗口收益)

**目的**:量化 B2 相对 stock 的端到端与分段收益,并与 B1 对比以分离"图化本身"
与"padding 算术"的贡献。

**设计**:A/B1/B2/C 四臂 × **3 次重复、臂间交错**(共用机器随时间漂移,配对差
必须同时测量);`PROBE_LIGHT=1`(零哈希);`--diagnostic-prompts 20` 后置采样拆出
命中步 prefill 窗口(beam token-loop 起 → token 0 完成,不含 miss 步);**miss 步
窗口作对照**(若 miss 窗口漂移,说明邻居占卡,该轮作废)。操作校验由探针计数器
逐臂核实(不成立则数字无效)。

**结果**(产物 `results/matrix_timing_0915/`):

操作校验(逐臂,来自 exec log):

```
A :  {bucket:128, scheduled:16, bound:256,  replayed:False}   ← 回落 eager
B1:  {bucket:128, scheduled:16, bound:1024, replayed:True}    ← M=128 回放
B2:  {bucket:16,  scheduled:16, bound:1024, replayed:True}    ← M=16 精确匹配回放
```

miss 窗口四臂一致(36.5–37.2 ms,C 38.0)→ miss 路径零影响、单变量成立。

端到端 hit 请求时延(配对差 B−A,ms,3 次重复的均值 / sd):

| 臂 | p50 | p90 | p99 | 命中步窗口 |
|---|---:|---:|---:|---|
| B1 | −3.19(sd 1.99) | −4.66(sd 1.17) | −5.95(sd 2.66) | 14.8 恒定(比 eager fast 态还贵) |
| **B2** | **−5.33(sd 1.62)** | **−6.91(sd 1.64)** | **−8.48(sd 3.18)** | **12.0–12.6,sd 0.5–0.7** |
| C(对照) | −1.04(sd 5.19,噪声内) | −0.23 | −0.00 | 双峰(与 A 同) |

**分析与结论**:

1. stock 的命中步是**双峰**(12.6/17.5 ms,sd≈2.4),C 臂与 A 齐平证明双峰是
   eager 回落路径固有、非图引入;B2 把它压成 12.0–12.6 ms 恒定线——p90/p99 收益
   来自消灭坏峰,p50 收益来自消除 B1 的 padding 算术(B1 回放多付 ~4 ms 抵消了
   图省的开销,B2 无 padding,p50 首次显著为负);
2. 三次配对差方向全一致,B1 与 B2 的差(约 −2.1 ms p50)与 padding 算术的微基准
   估算(~3.5 ms GEMM + attention)量级自洽;
3. **口径注意**:本实验数字是探针 harness 口径(probe 包装使绝对值整体偏慢,
   stock 基线比 AB 口径慢 ~6 ms),夸大了端到端差值;对外引用以实验 ④ 的 AB
   工具口径为准,本实验的价值是**行为验证与机制分解**(双峰消除、padding 分离)。

### 3.4 实验 ④:AB Test(真实代码,A-B-B-A)

**目的**:用仓库标准 A/B 流程验证**本 PR 的真实 commit**(非猴补丁)——默认行为
无回归(轮 1)与显式开启 KV_BOUND 的收益(轮 2)。

**基线与 benchmark 设计**:

- base = `6bca38c`(decode_graph tip,含 #385);head = `cd819386`(本 PR 改动在
  base 之上的 commit,服务器分支 `ab/prefill-bucket16`)。二者只差本 PR 的两个
  文件,merge-base 即 base,无无关改动混入;
- 调度:**A-B-B-A**(base→head→head→base),每侧 2×100 样本取平均,消一阶顺序效应
  (cache 预热、GPU 温漂);阈值:mean/p50 ±2% / p90 ±3% / p99 ±5%,超阈值标 ⚠️;
- KV_BOUND 注入:配套的 `ENGINE_ENV` 钩子(benchmark.env 定义,ab_runner 注入)
  **两臂同设**——base 的旧代码根本不读该变量,零影响,因此注入本身不破坏单变量;
- 场景:bw128-in1024(canonical,AB 工具默认)。

**轮 1 结果(不设 KV_BOUND,验证默认行为)**:

结论 **✅ No regressions**:

| 指标 | mean | p50 | p90 | p99 |
|---|---:|---:|---:|---:|
| e2el_hit | −0.71% | −1.05% | +0.33% | −0.89% |

全部在噪声阈值内。分析:默认不设 env 时,小图的 bound(2/32)依然拒绝长上下文命中
步,行为与 stock 等价——报告里 prefill_hit −4% 与 decode −1.8% 同现,而 decode
路径与本改动无关,说明该轮机器漂移量级 ~2%,这些小幅负值不作收益声明。此轮证明:
**默认网格多两张小图,零回归、启动捕获无劣化。**

**轮 2 结果(`ENGINE_ENV="VLLM_GR_PREFILL_GRAPH_KV_BOUND=1024"`)**:

结论 **✅ No regressions**,4 项超阈值收益:

| 指标 | baseline→head(ms) | mean | p50 | p90 | p99 |
|---|---|---:|---:|---:|---:|
| **prefill_hit** | 13.32→12.66 | **−4.88%** | **−4.91%** | **−5.45%** | **−5.48%** |
| **e2el_hit** | 45.19→43.84 | **−2.99%** | **−2.67%** | **−3.98%** | **−3.29%** |
| decode_hit(无关对照) | 31.19→30.71 | −1.54%(噪声内) | −1.69% | −1.75% | −0.24% |

分析:

1. **prefill_hit 全分位 −4.9~−5.5%,绝对值 13.3→12.7 ms,与实验 ③ 的命中窗口
   (12.8→12.0–12.6 ms)跨口径吻合**——机制性收益(命中步 bucket-16 精确回放)在
   真实代码上复现,且这轮 decode 漂移仅 −1.7%、decode_hit 全分位噪声内,收益不能
   归因漂移;
2. e2el_hit(端到端,含 5 个 decode token)~−3%,方向与显著性和实验 ③ 一致,
   幅度上稀释于非 prefill 阶段(decode 占 e2e 约 2/3),符合结构预期;
3. 两轮合并的因果链:轮 1(同 head、无 env)无任何超阈值项 → 轮 2 的收益只能来自
   `VLLM_GR_PREFILL_GRAPH_KV_BOUND` 打开的 bucket-16/1 回放,单变量封闭。

**结论**:PR 级引用数字为 **prefill_hit −5% / e2el_hit −3%**(AB 工具口径),
叠加实验 ② 的逐位一致——性能与数值可复现性同时成立,这是放宽 bound 的旧方案
(bucket-128)做不到的组合。

## 4. 数值契约与风险声明

- **结构性保证**:尾长 16→bucket-16、尾长 1→bucket-1 是精确匹配(同 M 同核),
  逐位一致与 miss 步的机制相同,不依赖巧合;
- **实证性保证**:尾长 2–15 与 M=16 的同核性是本机快照(torch 2.11.0+cu130 / L20)
  的实测,cuBLAS/torch 升级后可能失效。对 digest 门禁等严格场景,应显式约束
  (后续可加 `EXACT_MATCH` 开关只放行精确匹配)或两臂同开 `VLLM_BATCH_INVARIANT=1`;
- **stock 既有分叉,非本 PR 引入**:N 非 128 倍数时 miss 步(M vs bucket padding)
  与 N≤256 的命中步在 stock 下本就与纯 eager 存在低阶位分叉(实验 ② 顺带证实
  1025→1152 的 miss 分叉);本 PR 在 canonical(128 倍数)下把这些路径全部拉回
  逐位一致;
- **捕获成本**:+2 张小图(输入 buffer 各 1/16 token 行),启动捕获时间与显存增量
  可忽略,捕获 summary 日志自动纳入新 bucket。

## 5. 复现

```bash
# ① 同核矩阵微基准(容器内,GPU <1 分钟)
docker exec -w /opt/vllm-gr -e LD_LIBRARY_PATH=... vllm-gr-benchmark-gpu1 python3 \
  test_profilling/prefill_gate_probe/micro_m_matrix16.py

# ② digest(与计时分开;N=1025 的门控变体:EXTRA_BUCKETS="16",独立 OUTDIR)
ARMS="C B2" INPUTS="1024 1025" LIGHT=0 NUM_PROMPTS=10 WARMUP=2 REPS=1 BOUND=2048 \
  OUTDIR=/opt/vllm-gr/results/matrix_e2e_0915 \
  bash test_profilling/prefill_gate_probe/run_bound_timing.sh

# ③ 交错计时(四臂 × 3 次,空闲门禁内置)
ARMS="A B1 B2 C" INPUT=1024 NUM_PROMPTS=100 WARMUP=10 DIAG=20 REPS=3 BOUND=1024 \
  OUTDIR=/opt/vllm-gr/results/matrix_timing_0915 \
  bash test_profilling/prefill_gate_probe/run_bound_timing.sh

# ④ AB Test(两轮:轮 2 前在服务器 benchmark.env 加
#    ENGINE_ENV="VLLM_GR_PREFILL_GRAPH_KV_BOUND=1024",跑完删除)
./tools/AB_Tests/ab_benchmark.sh --base <base-sha> --head <head-sha> \
  --server gr --remote-dir /home/d00991341/vllm-gr-decode-graph
```

每份产物 JSON 自带 argv / env / kv_bound / buckets(探针)或 head_sha / git subject
(AB),单份文件即可复现其参数,不依赖本文档。

## 6. 讨论:网格粒度——为什么上游用 8 步长,本 PR 只用 {1, 16}

上游 vLLM 的默认 capture sizes 是 `[1, 2, 4] + range(8, 256, 8) + range(256, max+1, 16)`
(封顶默认 512)。本 PR 只在 vllm-gr 网格上补了两个 bucket。两边差异不是对错,
而是**网格要服务的形状分布不同**:

### 6.1 上游为什么必须细

上游是通用推理引擎,prefill 的 token 数是**任意值**——客户端给什么就是什么。
8 步长网格下任意 total 的 padding 上界是 7 行,平均 ~3.5 行;没有细粒度网格,
小 batch 的相对 padding 比例会失控(比如 total=9 落到 16,浪费 44%)。且上游
`max_cudagraph_capture_size` 封顶 512,网格总数 ~66,细化的捕获成本可控。

### 6.2 vllm-gr 的命中形状分布是结构化确定的

OneRec beam-search 场景的命中尾长由 block size 决定:

```
tail = ((N - 1) mod 16) + 1 ∈ [1, 16]
```

只有 16 种形状,且实验 ① 的同核矩阵直接测出了这个区间的 kernel 结构:

- **M∈[2,16] 共享同一 cuBLAS kernel**(输出逐位一致是 kernel 选择确定性的直接
  证据)→ bucket-2/4/8 与 bucket-16 跑同一个 kernel,GEMM 时间相同且都踩在
  memory-bound 地板上(权重读取 ~3.3 ms 与 M 无关);
- **M=1 是唯一例外**(gemv 路径,数值分叉且更便宜 ~1.3 ms)→ 必须精确承接。

所以在当前 workload 下,{2, 4, 8} 是纯成本(每张图 2 次 warmup + capture + 显存),
零增量收益——数值上不比 bucket-16 更安全(尾长 3,5,6,7,9–15 反正都靠实证同核,
与上游 8 步长下"pad 到 8"的性质同级),性能上同 kernel 无可省。**最小有效集就是
{1, 16}**。注意上游的 8 步长对尾长 3,5,6,7 同样不是精确匹配——两边在数值安全性的
性质上是同级的,区别只在上游没得选(分布任意)、我们有得选(分布确定)。

诚实标注一个未实测的假设:"同 kernel ⇒ 同时间"是推断——实验 ① 证明了输出一致,
但没有单独计时 M=2..15 的 GEMM。若要严谨可补测;若补测推翻,M≤8 确实更便宜,
则 {2,4,8} 应该加回来(下面的条件之一也随之提前)。

### 6.3 什么情况下 vllm-gr 也需要更细的粒度

三个具体场景,各带判据(满足即在网格中插入对应中间档):

| # | 场景 | 细粒度带来的东西 | 判据 | 验证实验 |
|---|---|---|---|---|
| 1 | **多请求合并的命中 batch**:`max_concurrency > 1` 时,同 prefix 的 k 个命中合并调度,total = k×tail 落点分散(如 2 个尾长 16 合并 = 32,现有网格落 128,padding 96 行) | 上游式 8/16 步长可精确或近似命中 | 线上并发形态:`max_concurrency` 与同 prefix 命中的合并频率 | B3 臂(上游式网格)+ 并发 >1 的 canonical 轮 |
| 2 | **N 非 128 倍数的 miss 步**:miss 的 total=N 落 `ceil(N/128)×128`,实测 N=1025 的 miss 窗口 40.6 ms(比 N=1024 的 36.5 ms 多付 ~4 ms,源于 1152 bucket 的 padding);上游 16 步长会选 1040 | 16 步长中间档把非对齐 N 的 miss padding 从 ≤127 压到 ≤15 | 线上 N 分布:非 128 倍数占比 | B3 臂 + `INPUTS="1024 1025"` 各一轮,看 miss 窗口差 |
| 3 | **cuBLAS/torch 升级后**:实验 ① 的同核结论失效(尾长 2–15 与 M=16 不再同核) | 更小的精确匹配 bucket({2,4,8})恢复逐位一致 | 升级后重跑同核矩阵,出现 exact=False 的组合 | 升级后复跑 `micro_m_matrix16.py` |

其中 #1、#2 的实际权重取决于线上负载形态(并发、N 分布),这也是本 PR 没有预先
铺设细网格的原因:**网格应该由实测的形状分布决定,而不是由上游的通用性决定**。
#3 是维护性触发器,同核矩阵脚本已在仓库,升级后一跑便知。

### 6.4 后续工作

- **B3 臂实验**(上游式网格 `{1,2,4} + range(8,256,8) + range(256,2048,16)`,封顶
  2048):canonical 预期与 B2 齐平(验证 {2,4,8} 冗余的推断);N=1025 轮预期 miss
  窗口 40.6→~37 ms(量化场景 #2)。与 N 分布调研绑定后再决定是否入网格;
- **M=2..15 的 GEMM 补测**(排除 §6.2 的假设漏洞);
- 顺带评估封顶下调(8192→2048,砍 48 张大图省捕获时间与显存;miss 1024 步的图
  化收益实测仅 0.6 ms,大图的价值密度低)——与上游 512 封顶哲学一致,可作为
  独立的网格重排 PR。

---

## 附:与 issue #390 的关系

本 PR 是该讨论的落地答案:bound 门控保持为正确性防护不变;通过"网格补小 bucket
+ 可放宽的捕获 bound"让 cache-hit 步在**形状恒等**的前提下回放,同时解决该讨论中
的尾延迟收益与数值可复现性两个矛盾诉求。讨论中"是否改默认 bound"的问题,本 PR 的
回答是:默认不改(轮 1 证明零回归),收益经显式 env 开启(轮 2 证明 −5%/-3%)。
