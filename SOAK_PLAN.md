# vllm-gr 长稳测试方案

> 本文是对 `tools/soak/` 后续测试方向的调研与方案。所有结论以代码为准，逐条给 `file:line`。
> **被测版本口径**：目标 commit 是 **`9c71c9d`**（`origin/decode_graph` 的 tip，
> `Support ModelRunner V2 (#337)`，2026-10-08 10:12 合并），
> `vllm_gr/*` 一律取自 `git show 9c71c9d:<path>`。运行主机 **10018**，
> `HOST_REPO=/home/d00991341/vllm-gr-decode-graph`。
>
> **不要对着工作区读行号。** 本地工作区 HEAD 是 `42e633b`（分支 `docs/soak-stability-report-en`），
> 与 `9c71c9d` **是分叉而不是前后关系**：共同祖先是 `338fd6f`（#403），工作区一侧缺
> `66a97c5`(#414)、`ce4ec32`(#415)、`74c4a9e`(#427)、`c1df60c`(#440)、`9fc66d3`(#389)、
> `9c71c9d`(#337)。
> 实测差异不是几行而是几十到上百行（`gr_async_scheduler.py` 48、`beam_search_v1.py` 153、
> `engine_core_patch.py` 41、`gr_session_client.py` 117），而且 `gr_session_client.py` 的
> `abort_gr_session_sync` 在两个版本里根本不是同一份实现 —— 工作区只有一行
> `engine.abort_request(...)`，**没有泵循环**。工作区只能用来读 harness，不能用来读被测引擎。
>
> `vllm/*` 指上游基线，**口径 = 容器里实跑的那一份**：`vllm 0.22.1`，
> `__commit_id__ = g0decac0d9`，装在 site-packages（`/usr/local/lib/python3.12/dist-packages/vllm/`），
> **不是 editable**。本文所有 `vllm/*` 行号都实测自这一份（2026-10-08 直接读镜像内文件）。
> 容器 `vllm-gr-benchmark-gpu1` 只挂三个卷 —— `/opt/vllm-gr`（= `vllm-gr-decode-graph`）、
> `data`、`huggingface` 缓存 —— **没有挂 vllm 源码**，所以引擎用的就是这个 site-packages 副本。
>
> ⚠ **不要用本地 `D:\code\vllm-gr\vllm\vllm` 读上游行号。** 那个 checkout 在 `main` 上
> （读取时 HEAD `8efa13b700`），与实跑的 0.22.1 **是显著不同的修订**，实测：
> `block_pool.reset_prefix_cache` 在部署版是 `:454`、本地 main 是 `:764`；
> `PauseState` 分别是 `:22` 与 `:24`；`scheduler.get_num_unfinished_requests` 是 `:1872` 与 `:2470`；
> 文件总行数 `block_pool.py` 520 vs 831、`scheduler.py` 2337 vs 3003。
> `docs/runtime_patch_inventory.md:15-18` 声明的基线 `releases/v0.22.1` → `dd72658e` 是**第三个**修订
> （`reset_prefix_cache` 在 `:761`），同样不等于实跑的那份。
> **复现部署口径**：`docker run --rm --entrypoint python3 vllm-gr:dev -c "import vllm._version as v;print(v.__version__, v.__commit_id__)"`。
> **行为性结论（分支、门、状态机、默认值）三个修订上一致**，只有行号会漂。

---

## 摘要

**要解决什么。** 四次长跑（R0/R1/R1b/R1-427，3.25 小时、207,606 个请求、零硬故障）走的都是会话生命周期里**唯一一条**路线：提交 → 完成 → `release_gr_session`。取消、原生暂停、驱逐、抢占这四条路线一条都没走到 —— 而这四条在 `a94830c8` 就已经存在，只是从没被端到端验证过（§1.1）。这批测试就是补它们。

**注意这四次运行的 pin 不是本次目标。** R0/R1/R1b 钉在 `a94830c8`，R1-427 钉在 `74c4a9e`；
本次目标是 `9c71c9d`，比 R1-427 多三个提交：#440（仅 CI）、#389（数值语义，§1.5）、#337（引擎重构，§0.2 ⑤）。
**跨运行只能对照延迟与内存形状，不能对照指纹** —— #389 按设计就会改变分数。

**五批，按依赖排序而不是按价值排序：**

| 批次 | 做什么 | 前置 | 规模 | 主要判据 |
|---|---|---|---|---|
| **1**（§2） | **探针基础设施**：Core 侧可查询的探针摘要、harness 双口径、watchdog 修 fork 同名、**logprob 纳入指纹、代码路径计数器** | — | 开发为主 + 1 次 30 min 验证跑 | 单进程与多进程两套口径读出同一组字段；计数器非零 |
| **2**（§3） | **取消 soak（离线）**：1 次标定跑 + K 步驱动 + 看门狗，**六个子场景**（场景 4 不在本批范围） | 1 | 两档口径：场景 1/2/3（取消点几何）× 1 h + 场景 5/6/7（终端/幂等路径）× 10 min ≈ 3.5 h | 无挂死；`_gr_v1_sessions` 无 ABORTED 残留；`reset_prefix_cache()` 恒 `True` |
| **3**（§4） | **pause soak（多进程）**：`call_utility("pause_scheduler", ...)`，五个子场景 | 1、2 | 5 场景 × 1 h = 5 h 循环 | pause 调用在超时内返回（`keep` 实测 0.054 s / `wait` 实测 2.66 s，**两者含义不同**）；`keep` 下冻结、`wait`/`abort` 下暂停窗口内排空退休（§4.4） |
| **4**（§5） | **组合轮**：取消 × 暂停 × 同 id 复用 × prefix hit | 2、3 | 4~8 h 连续循环 | 批次 2 与 3 的判据同时成立，且快照无单调上升 |
| **5**（§6） | **全量 5 h 基线复跑**（新口径）+ `#389` 契约不漂移 | 1~4 | 5 h 循环 | 与旧口径的延迟/内存形状一致；无新增持续上升序列；logprob 指纹稳定 |

合计约 **26 小时请求循环**，外加标定与开发开销。时间不设上限（§0.1），上面的"规模"是运行量而不是预算。

**六条必须先知道的前提**（详见 §0.2、§1.5）：

1. **这些路线不是 #427 引入的。** 会话表、五态机、`has_gr_cleanup_work`、原生暂停钩子在 `a94830c8` 就全部存在。这是补一个老缺口，不是验证一次新改动 —— 所以判据要按"能力是否成立"来定，而不是按"#427 有没有改坏"（§1.4）。
2. **目标 commit 上有两个新东西，#389 和 #337，性质不同。** #389 改的是 selected-token logprob 的归一化域（§1.5），**落在 soak 实测的那条路径上**，而现有 harness 看不见它（指纹只哈希 token id）—— 这是唯一"给数值改动做验证"的部分。#337（`Support ModelRunner V2`）是 47 文件、+3763/-249 的引擎改动，但 vllm-gr 用 `_env_check.py:43` 把 ModelRunner 默认钉在 V1，所以本方案全批次跑 V1，与 #337 之前同一条路径；#337 带来的**行号漂移**已全部重锚（§0.3 第 14 条），**V2 路径不在覆盖范围内**（§0.2 ⑤ 末段、§8）。
3. **批次 3 强制多进程**（`has_work()` / idle 回调 / 返回 `Future` 的 `pause_scheduler` 只存在于 `EngineCoreProc`，§4.3），而多进程会让现行对象图探针**静默变成 `null`**（§2.1）。所以批次 1 必须在批次 3 之前，否则批次 3 拿到的是空白，而且看不出是空白。
4. **批次 2 必须在批次 3 之前**：只有取消机制能稳定制造"会话处于 DRAINING / RETIRING"的状态，那是 pause 场景的必要输入。
5. **不测真正的驱逐 / 抢占** —— 造不出来（§7.1）。本方案测的是它的**不变量**（租约归零、free list 顺序、`reset_prefix_cache()` 恒 `True`），不是压力本身。
6. **"跑的哪个 ModelRunner"必须逐轮记进 `soak.log`，不能推断。** 它是 `_env_check.configure_model_runner_default()` 在 `EngineArgs.__post_init__` 阶段写进 `os.environ` 的，构造配置前后取值不同（§0.2 ⑦），事后无法从模型架构反推。

**怎么读这份文档**：§1 说明测试范围是怎么定下来的；§2~§6 是五个批次，每节按「必要性 → 代码在哪 → 怎么测」展开；§7 是贯穿所有批次的不变量判据；§8 是已知盲区；§9 是代码位置索引。

**全部五个批次均已执行完毕。** 批次 1 至批次 5 的实验目的 / 方案 / 结果 / 分析按实验报告体例整理在 `tools/soak/SOAK_RESULTS.md`；本文件保持为「方案与规格」的单一来源，两者内容冲突时以本文件为准。批次 5 的执行记录与一处判据订正见 §6.3，批次 4 的一处判据订正见 §5.3 末段。

---

## 0. 本版修订说明

### 0.1 已定的约束

| 约束 | 取值 |
|---|---|
| 被测 commit | **`9c71c9d`**（`origin/decode_graph` tip），`git archive` 出快照，`SOAK_REV` 必须显式写它 |
| 运行主机 | **10018**（`HOST_REPO=/home/d00991341/vllm-gr-decode-graph`） |
| 取消测试的驱动方式 | **离线路径** |
| 与 daily dashboard / 其它配置的可比性 | **不考虑** |
| 时间预算 | **不设上限** |
| 取消 / 暂停 / 驱逐 | **推理系统的必备能力** |

**上游口径已核实（2026-10-08）**：容器里实跑的是 `vllm 0.22.1`、
`__commit_id__ = g0decac0d9`，site-packages 安装，**非 editable**。本文 `vllm/*` 行号全部对齐它。
复现命令：

```bash
docker run --rm --entrypoint python3 vllm-gr:dev \
  -c "import vllm._version as v;print(v.__version__, v.__commit_id__)"
```

**不要用本地 `D:\code\vllm-gr\vllm\vllm`（main）读上游行号** —— 那是另一个修订（§8）。

### 0.2 调研推翻的四个原假设

**① 原以为这些路径是 #427 引入的 —— 错。**

`abort_gr_session`、`release_gr_session`、`_in_flight_dispatches`、`_kv_dispatch_leases`、
`_pending_terminal_result`、`_gr_v1_sessions` 会话表、`GRResourceStatus` 五态机、
`GRExecutionCapacity(active_sessions=1)`、`has_gr_cleanup_work`、native pause 钩子 ——
**全部在 `a94830c8` 就存在**。`tests/test_gr_pause_resume.py` 在基线提交上就跑得通。
#427 真正新增的只有一个字段、四个文件、三条构造期硬契约，外加六处控制流改动（§1.4）。

→ 所以这批测试**不是 "#427 专项验证"**，而是**这套会话生命周期第一次被端到端长跑**。
#427 动了这块代码，让这件事从"该做"变成"现在就该做"，但风险不是 #427 带来的。

**② 原以为"去掉 `reset_prefix_cache()` + 提并发"能造出驱逐压力 —— 错，而且反了。**

去掉它造不出驱逐压力（cached block 本来就在 free queue 里，前缀缓存不消耗可用块），
而它**恰恰是当前 soak 里唯一有判别力的泄漏 oracle**（§7.3）。删掉它等于删掉唯一的布尔判据。

**③ 原以为暂停要另写在线 harness —— 不必，但它强制多进程。**

离线能触发 native pause，有三条路径（§4.3）。但 `has_work()` / idle 回调 /
`pause_scheduler` 返回 `Future` 这一整套**只存在于 `EngineCoreProc`**，
InprocClient 根本不走 `_process_input_queue`。所以 pause 测试**必须开多进程**
→ 与"多进程是独立一轮"的原计划冲突，两者是同一轮。

**④ 原以为单请求占用约 1.5k token —— 错，实际是 57~69 个 block（900~1100 token）。**

128 条 beam 的 suffix KV **不在 block pool 里**，而在 `BeamAttentionContext` 的固定池
（启动时按 `max_batch × max_beams × num_layers × max_decode_steps × kv_heads × head_dim` 一次性分配）。
原方案里"`beam_width 128 → 256` 让单请求 KV 翻倍"这条放大项因此不成立。

**⑤ 原以为 `decode_graph` 的 tip 就是 #427 —— 错。上面先多了 #389，现在又多了 #337，而 #337 是切面大得多的那个。**

`origin/decode_graph` 的**本地 remote-tracking ref 是陈旧的**（停在 `74c4a9e`）。真正 fetch 之后
今天（2026-10-08）的 tip 是 `9c71c9d`：

```
9c71c9d  Support ModelRunner V2 (#337)                                     ← 被测
9fc66d3  feat(gpu): shared-domain selected-token scoring contract (#389)
c1df60c  ci: use runner-local pre-commit cache (#440)                      ← 仅 CI，无引擎改动
74c4a9e  refactor(beam): unify native V1 scheduling and resource lifecycle (#427)
```

两个提交性质完全不同，必须分开说，**不能沿用"引擎逐字未变"这种简化**。

**#389（`74c4a9e` → `9fc66d3`）才是纯数值语义改动。** `git diff 74c4a9e 9fc66d3` 只动
`vllm_gr/constraints/*`、`vllm_gr/ops/*`、`tests/*`、`.github/*`，引擎与会话生命周期代码逐字未变。
它改了 selected-token logprob 的归一化域（§1.5），落在 soak 实测的那条路径上，而指纹只哈希 token id，
所以原方案对它零覆盖 —— 这是批次 1 加「logprob 纳入指纹」的原因。

**#337（`9fc66d3` → `9c71c9d`）是 47 个文件、+3763/-249 的引擎改动。** 但它对本方案的影响被三件事限制住了：

1. **它默认不改运行路径。** vllm-gr 在 `vllm_gr/_env_check.py:43` 里
   `os.environ["VLLM_USE_V2_MODEL_RUNNER"] = "0"`，由 `vllm_gr/plugin.py:30` 的
   `initialize_runtime()` 在 vLLM `load_general_plugins()` 时调用，把 ModelRunner 钉死在 V1。
   vLLM 0.22.1 本身会对 `Qwen3ForCausalLM` 自动启用 V2（`vllm/config/vllm.py:69`
   `DEFAULT_V2_MODEL_RUNNER_ARCHITECTURES = frozenset({"Qwen3ForCausalLM"})`），而 OneRec-1.7B 的
   `config.json` 里 `architectures` 恰好是 `["Qwen3ForCausalLM"]` —— **没有这一行，soak 会静默跑在 V2 上**。
   实测佐证见 §0.2 ⑦。
2. **V1 路径上的改动是参数化，不是改义。** 逐条核对 §1~§7 引用的文件后确认：
   `gpu_model_runner_patch.py` 唯一改动是 `graph.capture(..., persistent_install=True)`（`:815`），
   而 `persistent_install=True` 正是 V1 侧 `_StageExecutionHooks` 的既有取值
   （`gpu_beam_stage_runner.py:231`），V2 侧才是 `False`（`gpu_beam_stage_runner_v2.py:39`）——
   这是把 dummy-capture 对齐到 V1 既有语义，不是新行为。
   `beam_search_v1.py` 只多了一个可选字段 `GRStageMetadata.kv_blocks`（V2 专用，V1 为 `None` 时走原路）
   和一处函数拆分 `validate_gr_execution_runtime()`（`:121`）。
   `gr_async_scheduler.py` 的 55 行是重排 + 新增，五态机与 `has_gr_cleanup_work` 的判据未变。
3. **但行号漂了。** §1~§7 的行号证据**必须重锚**，不能沿用 `9fc66d3` 的数字。本版已逐条对
   `9c71c9d` 重测，订正清单见 §0.3 第 14 条。

**V2 覆盖是显式的范围外项，不是遗漏。** V2 只支持 `beam_search_v1`（即 `execution_mode=v1`
持久协议），要显式 `VLLM_USE_V2_MODEL_RUNNER=1` 才启用，且与 V1 是**两条不能混跑的并列路径**
（`vllm_gr/engine/arg_utils_gr.py:650-666` 在 CUDA + V2 + 非 v1 模式下直接抛 `ValueError`）。
本方案全部批次跑 V1，也就是 vllm-gr 的默认与生产口径。V2 要另开一轮，记在 §8。

**⑥ 原以为"在线路径"需要显式排除 —— 不必，它在 `decode_graph` 上根本不存在。**

`git merge-base --is-ancestor 9c71c9d pr444` = NO。PR #444（在线 V1 分发）在另一条分支上，
`decode_graph` 没有它。所以 §0.1 不需要"不考虑在线"这条声明，§8 也不需要把它记成盲区。

**⑦ 原以为"引擎跑的哪个 ModelRunner"是既定事实 —— 不是，它是一个会被静默改写的环境变量。**

本版实测（2026-10-08，容器 `vllm-gr-benchmark-gpu1`，不设任何环境变量直接构造 soak 的
engine kwargs）：

```
$ printenv VLLM_USE_V2_MODEL_RUNNER          # 容器启动时：没有这个变量
$ python3 -c "import vllm.envs as e; print(repr(e.VLLM_USE_V2_MODEL_RUNNER))"
None                                        # 读之前是 None
$ python3 -c "...; c = EngineArgs(**soak_kwargs).create_engine_config(); print(c.use_v2_model_runner)"
False                                       # 构造完就变成 False
```

差异来自 `EngineArgs.__post_init__` → `load_general_plugins()` → `vllm_gr/plugin.py:49`
→ `initialize_runtime()` → `_env_check.configure_model_runner_default()` → `os.environ[...] = "0"`。
**所以判断"当前跑的是哪个 runner"不能看 `architectures`，只能看这个变量** —— 空手算会算错：
`_is_default_v2_model_runner_model()` 实测返回 `True`、`_get_v2_model_runner_unsupported_features()`
返回 `[]`、`HAS_TRITON` 为 `True`，三个门全是绿灯，`use_v2_model_runner` 仍然是 `False`。
每批 run 的 `soak.log` 里那句 `prefill fastpath counter: installed` 只证明 GR 补丁生效，
**不区分 runner**；要留证据就得在启动日志里记 `os.environ.get("VLLM_USE_V2_MODEL_RUNNER")`。

### 0.3 本版修订（锚定 `9c71c9d`）

| # | 改了什么 | 为什么 |
|---|---|---|
| 1 | 目标 commit `74c4a9e` → `9fc66d3` → **`9c71c9d`**；`SOAK_REV` 必须显式写它 | remote-tracking ref 陈旧；`9fc66d3` 之后又合了 #337（§0.2 ⑤） |
| 2 | 删掉「工作区行号差 5~18 行」 | 实测是分叉，差 41~153 行；工作区的 `abort_gr_session_sync` 不是同一份实现 |
| 3 | 删掉「不考虑在线路径」的排除条款 | `decode_graph` 上没有在线 V1（§0.2 ⑥） |
| 4 | 新增 §1.5：`#389` 数值契约 | 本次唯一的行为性变更，且落在 soak 实测路径上 |
| 5 | §4.3 重写 pause 驱动代码与判据 | `SyncMPClient.call_utility` 是**阻塞**调用、返回结果值而非 Future |
| 6 | §4.3 增加 pause 前置断言 + 批次 3 标定跑 | `pause_scheduler` 在引擎已 idle 时直接 `return None`；不先验证「GR 收尾能否在 PAUSED_ALL 下推进」，批次 3 五小时全是假故障 |
| 7 | §7.3 修正 `reset_prefix_cache()` 的调用时机 | 有任何 block 在用就返回 False，而租约的设计目的正是顶住 `ref_cnt` |
| 8 | §3.1 / §7.1 修正租约兜底机制的描述 | `update_gr_from_output` 的 pop 不是「开头无条件」，被 `metadata is None` 和 `owner_generation`/`dispatch_id` 双门挡住 |
| 9 | 订正两处归属（§1.4.2 / §4.1） | `_discard_pending_native_finish` 的 discard 分支是 #403 新增；`is_gr_session_retired` 才是 #427 新增 |
| 10 | §2 批次 1 增加两项 harness 要求（⑤⑥） | 原判据全是负向的，静默回落原生也能全绿 |
| 11 | **上游行号口径定死**：部署的 `vllm 0.22.1 @ g0decac0d9`（site-packages） | 三个候选修订（部署版 / 本地 main / 声明基线 `dd72658e`）行号互不相同，实测差最多 300+ 行；先前一度按本地 main 改过一批行号，已全部回滚 |
| 12 | §4.2 / §4.3 / §7 的少量上游行号按部署口径订正 | 逐条实测后确认**多数原值本来是对的**（原文档用的就是部署口径）；仅 `core.py` 的 `pause_scheduler`/`has_work`/`_invoke_utility_method`、`scheduler.py` 的 `get_num_unfinished_requests`/`llm.py`/`llm_engine.py`/`stats.py` 若干处需要更新 |
| 13 | §6.2 判据扩到 6 条，并与 R1-427 划清可比范围 | 新增 #389 的 logprob 指纹稳定性与退化顺序断言；指纹跨提交不可比 |
| 14 | **全部 `vllm_gr/*` 行号重锚到 `9c71c9d`**，逐条 `git show` 实测 | #337 改了 47 个文件，`9fc66d3` 的行号不再成立。实测后**绝大多数原值仍对**（`core.py:240`、`engine_core_patch.py:191`、`gpu_model_runner_patch.py:206`、`gr_async_scheduler.py:146-148/163-170/251-296/388-394/498-502/602-644`、`beam_search_v1.py` 全组、`gr_session_client.py:177/190-192/233` 全部命中）；**5 处需要订正**：`output_processor.py:274`（已过 EOF，文件只有 267 行）→ `:239`；`output_processor.py:141-142` → `:132, :240`（真正的「不暴露 per-token logprobs」证据在 `beam.__dict__["logprobs"] = []` 与 `logprobs=None`）；`engine_core_patch.py:720-730` → `:710-733`（`_discard_pending_native_finish` 定义在 `:710`，PAUSED_ALL 那段注释在 `:722-727`，调用点在 `:803`；原值只覆盖了函数中段）；`gr_async_scheduler.py:653`（现在是 `release_gr_session` 体内）→ `:670`（`install_gr_scheduler_methods` 里那行赋值在 `:670`，配套的 `release_gr_session` 在 `:671`）；`gr_async_scheduler.py:62` → `:64`（`has_gr_cleanup_work` 的定义行）；`gr_async_scheduler.py:622/622-631/622-644`（`abort_gr_session`）→ `:639/639-648/639-648`。另外**两处原本就对、不要动**：`engine_core_patch.py:449-453` 正是 `has_requests` 的 `@wraps` 补丁本体，`gr_async_scheduler.py:64-73` 正是 `has_gr_cleanup_work` |
| 15 | §0.2 新增 ⑤ 的 #337 分节与 ⑦；§8 新增 V2 盲区 | #337 是 47 文件改动，但 vllm-gr 用 `_env_check.py:43` 把 runner 钉在 V1，本方案全批次跑 V1（默认/生产口径）；V2 另开一轮 |
| 16 | §2.3 改写为**已实现**设计 + 冒烟实测 | 探针已落地并提交（服务器 `cd0a28d` = `9c71c9d` + 探针提交），不再是设计草案；冒烟 2 的两组独立数字已对账（§2.3.4） |
| 17 | §2.3.3 新增探针提交 `561ebbb`；§4.4 补 `has_work` 三要素 | 批次 3 判据要的是 `has_work()`，原探针只给会话表，读者只能拿 `by_status` 当代理 —— 而"代理恰好为空"和"引擎真空闲"在报告里逐字相同。现在 `has_gr_cleanup_work` / `scheduler_has_requests` 在**引擎进程内**直接求值（`None` = 问不到，不折叠成 `False`） |
| 18 | §4.4 补 DRAINING 的正常路径来源 | 原方案默认"DRAINING 只有取消才会出现"。实测**两个枚举**：`GRResourceStatus`（探针报的这个）与 `GRSessionStatus`，正常完成的最后一个 stage 同样置 `DRAINING`（`gr_async_scheduler.py:546-549`）—— 标定跑的构造因此从"取消到一半再 pause"改成"正常驱动到 DRAINING 再 pause" |
| 19 | §4.3 更正驱动方式：**不要用子线程包 `call_utility`** | 初稿 `Thread + join(timeout)` 有洞：`call_utility` 与 `step()` 写同一个 ZMQ `_send_input`，socket 不是线程安全的。改为**只拆"发"与"等"、仍单线程**，并明确 `step()` 不能省（不泵则 `has_work()` 因为引擎本来没跑而为假，pause 立刻返回是真空通过） |
| 20 | §3.3 / §4.4 增加两个标定脚本 | `tools/soak/calib_k.py`（每步采 `_dispatch_records` / `_in_flight_dispatches`，折出每个 stage 的开窗关窗步号并给 K 候选，自带"同一步内完成所以看不见"的盲区计数）、`tools/soak/calib_pause.py`（退出码即判据，挂死返 1 且不救引擎） |
| 21 | §3.3 补批次的 harness `cancel_soak.py` 与两处构造订正 | 场景 6 不能走 `client.abort`（`client.py:181-182` 在 `session.closed` 时直接 return，第二次起是空操作）；场景 7 不能靠复用 call 对象（`prepare_request` 每次发新 uuid，`input_processor.py:37-42`）。判据分硬/软两级，见 §3.3 |
| 22 | §3.3 订正清理重试的版本差异 | `74c4a9e` 的 `except EngineDeadError: raise` **在锚点 `9c71c9d` 里已经在位**（`git merge-base --is-ancestor 74c4a9e 9c71c9d` 为真，`client.py:49-50` 实测），原写"工作区副本没有这个分支"是错的。本批口径下引擎死亡会立即抛出，不是无限重试 |
| 23 | §2.3.3 记下 `soak-mp.env` 命名陷阱 | tooling 里 `soak-mp.env`（连字符，9 月旧文件，`SOAK_REV=a94830c8`）与 `soak.mp.env`（点号，`cd0a28d`）只差一个字符；批次 3 跑 MP 时拿错就**静默测旧 commit**。已改名为 `soak-mp.env.pre427-stale` |
| 24 | §2.3.2 / §2.3.5 补看门狗进程发现的前置条件与实测 | MP=1 下 `EngineCoreProc` 把进程名改成 `VLLM::EngineCore`，`--pid-pattern run_soak.py` 只拿到 driver，`proc_count` 恒 1、RSS 少 2.9 GB —— 而这条 RSS 是批次 3 内存论证的支点。pattern 必须写成 `run_soak\.py|VLLM::EngineCore`；修正后 `proc_set {proc_count: 2}`、RSS 4.82 GB。另记一次 `process_lost` 后 pid 复用导致曲线尾部测到别的进程 |
| 25 | §2.3.5 记下 `mp0-val30` 实测，并明确本门**不覆盖** `561ebbb` 新字段 | 快照 rev 是 `cd0a28d`，`has_gr_cleanup_work` 等五个字段在这两次跑的 `gr_probe` 里根本不出现；它们的归属是批次 3 之前的标定跑（`1f28138` 快照） |
| 26 | **`run_soak.py` 加 `install_input_send_lock`**；§2.3.5 / §8 记下 MP 探针线程打崩引擎 | `mp1-val30` 跑到 20 分钟时 EngineCoreProc 抛 `ValueError: b'\x00\x00' is not a valid EngineCoreRequestType`（`core.py:947`）。部署的 `SyncMPClient` 全文没有锁，而 `ProbeThread` 引入第二个 socket 写者。这是结构性缺陷不是偶发：**只要 MP 口径用线程采探针就必然撞**。装锁后 `meta.json` 报 `input_send_lock: installed`（单进程报 `not-needed`），报 `unavailable` 则 MP 跑不能开 |
| 27 | **§2.3.2 的 `free_blocks` 判据被实测证伪并订正** | 原写"两种口径的 `free_blocks` 一致"。实测两个口径的 KV pool 容量本身差 54 blocks（MP=0 `158,000 tokens` / MP=1 `157,136 tokens`，两次 MP 跑逐位相同），`free_blocks` 区间整体下移（`mp0` 9,805–9,874 vs `mp1b` 9,751–9,820）。改为"各报一个落在自己池子里的值"，并要求 §3.3 的"回落到基线"判据**同口径同池**地比。**本行的归因当天下午被第 29 行证伪** |
| 28 | §2.3.5 回填 `mp1b-val30` 实测，本批次 1 **收口** | 12:20:58 → 12:53:03，`14,247` 请求对、58 个采样点、`input_send_lock: installed`，引擎未再崩；五项判据全过。同时记下一处口径不对称：MP 下 `prefill_fastpath` 报 `unavailable_multiprocess`，**"两侧计数器互证"只在单进程成立**，MP 口径只有 `gr_probe_counters` 一条来源 |
| 29 | **§2.3.2 / §2.3.5 第二次订正：池子差不是口径造成的，是启动抖动** | `calib_k.py` 的 **MP=0** 跑报 `157,136 tokens`，与 `mp1b-val30`（MP=1）逐位相同 —— 同一个口径给出两种池子，第 27 行"差 54 块是口径的确定性后果"不成立。四跑里唯一可见的分野是 `Initial profiling/warmup run took`（池子最大的那跑 0.23 s，其余三跑 1.2 s）。**后果从"阈值"升级为"判据怎么写"**：`meta.json` 根本不记录 `GPU KV cache size`，池子只在启动日志里，所以 `free_blocks` 只能跟**同一次跑自己的基线**比 —— 跨口径不行，跨跑也不行 |
| 30 | §3.3 回填 `calib_k.py` 标定结果 | 3 个 stage（PREFILL#0 / DECODE#1 / DECODE#2-last），**每个恰好占一个 `step()`**，回执落在下一步；K=1/2/3 分别命中三个在途窗口，写进七个场景表的 1/2/3 行。与 `mp0-val30/meta.json` 的 `dispatch_records_max=3` / `in_flight_max=2` 互证。另记两个坑（阶段状态在 `GRSessionState` 不在客户端句柄、`GRSessionStatus` 无 `CLOSED`）和一次被证伪的"挂死"（第二跑其实是 JIT 编译慢）；循环改为"三个 dispatch issue 完即停" |
| 31 | **§4.4 整节按实测重写；§4.5 判据同步订正**（含一次自我推翻） | 原问"PAUSED_ALL 下 GR 收尾能不能推进、会不会让 pause 永不返回"。五跑（`1f28138`，MP=1，`keep`/`wait`/`abort`）给出的答案分两半：**①不会永不返回** —— `abort` 那一跑恰好落在原方案想构造却没构造出的状态（`by_status={'DRAINING':1}`、`has_gr_cleanup_work=True`、`has_requests=True`），pause **2.67 s 正常返回**、会话干净退休，§4.1 的 `finished_req_ids` 残留路径没有发作；六次 pause 全部返回。**②"`keep` 冻结一切"这个说法被自己第二轮实测推翻** —— PAUSED_ALL 只是不发出**下一个** dispatch，不拖住在飞的活，所以结果取决于 pause 落在 stage 边界哪一侧：落在前侧（`records` 2/3）第 3 个 stage 从未发出、会话冻满 10 s、**resume 后跑完并退休**；落在后侧（`records` 3/3）会话**在 PAUSED_ALL 下就退休**（2.65 s）。同时订正：ⓐ**pause 回包耗时在任何 mode 下都不是信号**（同一 `keep` 报过 0.054 s 与 2.65 s，同一 `wait` 报过 0.03 s 与 2.66 s，当时会话都还在册），已把主判据换成"会话是否离开 `_gr_v1_sessions`"；ⓑ**MP 下 driver 不能泵引擎**（`step()` 即 `get_output()`，永久阻塞；实测 driver 0 jiffies/8 s 而 EngineCore 烧 1 core-s/8 s），原文"继续 `engine.step()`"整段删除，改为纯轮询；ⓒ§4.5 的"至少一次 `RETIRING → CLOSED`"**不可观测**（`_release_owner` 同一次调用里置 `CLOSED` 并 pop），改为"离开 `_gr_v1_sessions`"。**剩下唯一未覆盖的一格**：`keep` 窗口内再发取消（场景 2），那是唯一能造出"冻结在收尾中"的构造 |
| 32 | **§3.3 批次 2 时长口径改两档，并回填场景 1/2/3 实跑结果** | ⓐ**口径**：原"6 × 1 h = 6 h"，实跑改为场景 1/2/3 各 1 h（在途窗口，判据逐轮断言 ⇒ 证据强度 = 轮次，11 万~27 万次取消零残留已饱和）+ 场景 5/6/7 各 10 min（终端/幂等路径，120 s 冒烟已全过，加时间只重复已验证路径）。总墙钟 6 h → 约 3.5 h。ⓑ**现场动作**：`kill` 驱动 shell 让场景 3 之后不再按 3600 s 起场景 5 —— 正在跑的 python 子进程被 reparent 到 PID 1 跑完并打出 verdict（fd 1/2 是文件重定向不是管道，没有 SIGPIPE 风险），再由 `SCENARIOS=5,6,7 DURATION_SECONDS=600` 新驱动接续。ⓒ**结果**：场景 1（K=1，266,991 轮）、场景 2（K=2，157,731 轮）、场景 3（K=3，112,386 轮）**七项判据全 PASS**；场景 5（K=0，15,986 轮，**第 8 项** `release_gr_session=15986 / abort_gr_session=0` 也 PASS）、场景 6（245,434 轮）、场景 7（344,150 轮）七项全 PASS。**批次 2 收口：1,142,678 轮、114,266 次缓存检查 0 False、0 FAIL、0 Traceback、六个 `exit=0`。**ⓓ**又一处被证伪的估计**：原文"1 h 约百万轮量级、`iterations.jsonl` 约 2 GB/场景"，实测速率 74/44/29 轮每秒、单场景 0.1~0.24 GB，磁盘预算高估一个量级。ⓔ记下"单场景约 2.5 min 是引擎启动（Triton JIT）"——时长越短启动占比越高，600 s 时约 20%，再砍到 300 s 不划算 |
| 33 | **批次 3 五小时扫描实跑（2026-10-08 13:21 → 18:35 UTC，5 h 14 m）** | `pause_soak.py`（md5 `460eddfb…`）+ `run_pause_soak.sh`，五场景各 3600 s，串行。**实测**：场景 1 `samples=69210 misses=1 failures=0`、场景 2 `66458 / 1 / 0`、场景 3 `64701 / 46 / 0`、场景 4 `60610 / 1 / 0`、场景 5 `1134 / 1 / 0`；合计 272,113 个样本、50 个 miss、`reuse_checks=13105` 全过、`probe_reads=2,480,148`（场景 5 单场景 1,177,734，因为它 2 ms 一采）且 `probe_errors=0`。五个场景的时长（`summary.json` 的 +0800 时间戳）22:23:32 / 23:26:51 / 00:29:39 / 01:32:37 / 02:35:38 恰好 1 h 一节。**场景 3 的 46 个 miss 已查明**：45 个是 `pause landed after the request completed`（3 会话下前置只需 1 个会话在飞，暂停往返期间那一个跑完，占 3237 轮实测的 1.4%，低于 5% 提示线），1 个是启动后注册超时。**s3 的 64701 个实测轮全部满足**：3 个会话同时在册、`by_status={'RUNNING':1,'WAITING':2}`、`retired_under_pause=True`、`cleanup_states_seen=['DRAINING']` |
| 34 | **审计这次扫描时发现 harness 两处缺陷，均已在写报告前修掉** | ⓐ**失败根本没有被计数**：`_evaluate` 返回的 `False` 只写进 `notes`，而计数处（`pause_soak.py:1371`）判的是 `record["passed"] is False` —— 这个键此前只有 `pause_hung` 早退路径（`:919`）会写。后果：五个 `PASS` 只证明了没有 wedge，判据级失败全部静默。**实测证据**：场景 5 有 499/1134 轮带 `a new dispatch was issued while the pause was in force` 注释，而 `summary.json` 报 `failures=0`。已补 `record["passed"] = passed`（`_evaluate` 之后、重分类规则之前）。ⓑ**探针的 `pause_state` 不能当钟用**：`resume_scheduler` 返回后探针仍读 `PAUSED_ALL`（场景 2 `66458/66458`、场景 5 `1134/1134`），因为档位由引擎的**下一步**清除，而 `keep` 只冻结了唯一一个在飞的 stage 时引擎已无步可跑。于是 resume 后对冻结 stage 的重派发必然落在两个都读"暂停中"的采样之间。**实测证据**：场景 5 的 305 个增长对**签名完全一致**（`in_flight` 0→1，`worker_resources_allocated` 恒为 1）、**位置全落在窗口的 32.6%–34.4%**，即 3 个 cycle 里第一个 resume 的边界；按新旧判据对 5 小时数据重算，这 499 轮全部是假阳性。已把 `keep` 判据的取样区间改成驱动自己的时钟——每 cycle 记 `pause_sent_at` / `resume_sent_at`，只在两者之间取样——并写了五个用例的单元检查（区间内增长判违规、resume 之后增长不判、第 2 个 cycle 同样受判、跨 cycle 的增长不判、全程 `UNPAUSED` 判空转）。ⓒ**已采集的 5 小时数据里，非 `keep` 侧可逐轮重算，`keep` 侧不能**：`abort`/`wait` 档的判据只读 window 行与 `notes`（场景 1 无违规注释、场景 3 的 45 条是已分类的 miss、场景 4 只有 `wait` 档有注释），所以场景 1/3/4 不重跑；而修正后的 `keep` 判据需要每 cycle 的 `pause_sent_at` / `resume_sent_at`，**这两列是本次才加进 `cycles` 的，5 小时记录里没有**（只有 `pause_seconds` / `resume_seconds`），场景 2/5 无法重算，故另跑 |
| 35 | **`keep` 侧（场景 2/5）用修正后的装置各重跑 1 h；同时订正 §4.4.1 的 pause 时延表** | ⓐ**重跑**：`SCENARIOS=2,5 DURATION_SECONDS=3600 SUFFIX=-fixed`，装置 md5 `efe21eeb…`（含 §34 两处修复），输出 `pause-fixed-s2` / `pause-fixed-s5`，日志 `pause-keep-fixed.log`。旧 5 小时扫描的 `pause-s2` / `pause-s5` 仍保留，用于交叉核对非 `keep` 侧判据。ⓑ**修正后的装置先经两重确认再上长跑**：五个用例的离线单元检查（区间内增长判违规、`resume` 之后增长不判、第 2 个 cycle 同样受判、跨 cycle 的增长不判、全程 `UNPAUSED` 判空转）全部通过；随后 150 s × 5 场景冒烟（`pause-smoke-v2.log`）**五个 `failures=0` 全 PASS**，其中场景 5 `samples=20 misses=1 failures=0 reuse_checks=1 probe_reads=20719`，`dispatch_growth_under_pause` 20 轮全为 `[]`，`paused_pair_samples` 每轮约 1030（区间内确实取到了样本），而 `dispatch_records_max_after=3` 的 10 轮说明"最大值涨了但不在暂停区间内"这一区分确实生效 —— 这正是旧判据误报的那批数据。ⓒ**§4.4.1 时延表订正**：原表把"≥1 s 的次数"填成 11，实际 11 是 **≥0.1 s** 的计数；按 1 s 阈值是 **7 次**，且分母是 264,381 次 pause（不是 272,113 次迭代，场景 5 每轮 3 次 pause）。场景 3 原填的 5 次（1.44/2.25/2.80/3.52）不存在，实测只有 1 次（2.629，第 2 次迭代）；场景 4 的 9 次 ≥0.1 s 全部落在 `wait` 档这一点不变。已逐格重填并补 `pause` 次数列 |

| 36 | **结案：`resume` 后读到 `PAUSED_ALL` 是装置的取值时刻问题，引擎侧无缺陷（2026-10-09）** | §35 只把这条疑点当成"判据取样区间选错"的成因，没有单独定性。本次用已有数据加代码文本定死它。ⓐ**`UNPAUSED` 在 `vllm/v1/` 全仓只有一个写入点**：`v1/engine/core.py:696` 的 `EngineCore.resume_scheduler`；在引擎进程内该方法只有两条可达路径 —— 客户端 utility 派发（`core_client.py:1105`）与 `EngineCore.wake_up`（`core.py:754`，本装置从不调 sleep/wake）。所以能清除暂停状态的只有驱动自己发的 resume。ⓑ`EngineCoreProc.resume_scheduler`（`core.py:1755-1763`）在 `engines_running` 为真时**早退并跳过 `super()`**，即跳过清除；而 `_has_global_unfinished_reqs` 让 `engines_running` 在 32 步里 31 步为真。若该早退被走到，暂停状态**此后无人清除**，其后每一轮的"暂停前读取"都应读到 `PAUSED_ALL`。ⓒ实测后者在重跑的 66,369 / 66,369 轮上读到 `UNPAUSED`，即每一轮的 resume 都真的执行了清除。ⓓ`pause-fixed-s2` 的 66,369 轮里，**每一轮的窗口内都存在至少一帧同时满足 `pause_state=PAUSED_ALL` 且 `session_count=0 / in_flight=0 / leases=0`，位置不早于 `gone_from_registry_index`**（例：第 2 轮窗口共 5 帧，这样的帧落在第 4、5 帧，而 `gone_from_registry_index=4`）。`keep` 档下会话本来就在暂停生效期间注销，所以 `_drain_sessions` 的等待在第一次检查时即成立，取回的帧可能早于 resume。**结论**：该栏是装置的**第三处**缺陷（§35 只列了两处），引擎在 resume 调用内完成清除；先 `/resume` 再 `/is_paused` 会得到 `false`（由上述排除法得出，未单独实测）。**顺带订正 §34**：那句"档位由引擎的**下一步**清除"是错的 —— 引擎的 step 路径不写 `_pause_state`。**本条按定义不会落到 `vllm_gr/`**：本批次涉及的 pause/resume 机制全在 vendored 的上游 vLLM 0.22.1 里（`grep -rn "pause_state\|PauseState\|pause_scheduler\|resume_scheduler" vllm_gr/` = **0 命中**；`vllm_gr` 侧唯一相关的一行是 `engine_core_patch.py:722` 为 PAUSED_ALL 下的 idle 判定加的绕行注释） |

| 37 | **批次 3 收口统计：订正 `reset_prefix_cache()` 的计数口径（2026-10-09）** | 报告原先把汇总行的 `reuse_checks=13105` 当成了 `reset_prefix_cache()` 的调用次数，两者是不同的检查：`reset_prefix_cache()` 由 `cache_check_every` 控制（本装置为 10 轮一次，且只在 `tracker.gone(sampler.last())` 成立即引擎完全静置时才真的调用，否则记 `skipped: session not settled`），`reuse_checks` 由 `reuse_check_every` 控制（20 轮一次），内容是 `_reuse_probe`（`pause_soak.py:776-808`）的一次「提交 → 等待注销」完整循环、其间不含暂停，用来度量「引擎在恢复之后是否仍然可用」。**实测真实计数**：五小时扫描 `reset_prefix_cache()` 调用 **26,214** 次（场景 1/2/3/4/5 分别为 6,921 / 6,645 / 6,474 / 6,061 / 113），`false=0`、`skipped=0`；`keep` 侧重跑 **6,750** 次（6,637 + 113），`false=0`、`skipped=0`；合计 **32,964** 次全部返回 `True`。复用探测五小时扫描 13,105 次、重跑 3,374 次，合计 16,479 次，0 次未完成注销。**另订正**：批次 3 的「未取到样本」合计为 **52** 次（扫描 50 + 重跑 2），原稿只写了扫描的 50。批次 3 的两套运行的完整合计数（迭代 339,616、暂停 334,152、探针读取 4,009,937 / 0 错误）已写进 `SOAK_RESULTS.md` 的结果摘要 |

---

## 1. 测试范围是怎么定下来的

这一节回答"为什么测这几条、为什么不测那几条"。§2 之后的每一批都从这里拿依据。

### 1.1 现有覆盖到哪为止

四次长跑（R0/R1/R1b 钉在 `a94830c8`，R1-427 钉在 `74c4a9e`，合计 3.25 小时、207,606 个请求、零硬故障）
覆盖的**只是会话生命周期的正常路径**：请求提交 → 请求完成 → `finally: close()` → `release_gr_session`。
**没有一次跑在本次目标 `9c71c9d` 上**（R1-427 差 #440、#389、#337 三个提交），所以它们给的是"这条路线第一次被长跑"
这个结论，不是"目标 commit 已验证"。

生命周期里其余的路线一次都没走到：

| 路线 | 为什么现有 loop 到不了 |
|---|---|
| 带在途 dispatch 的取消 | 循环从不取消请求 |
| 原生暂停下的退休 | 循环从不 pause |
| block pool 驱逐 | 每轮都调 `reset_prefix_cache()`，且 128 条 beam 共享一个 prompt 前缀 |
| 抢占 | `max_num_seqs=1`（`run_soak.py:224`），`allocate_slots()` 不会返回 None |

**关键点：这些路线在本仓库比这份数据集更老。** `abort_gr_session`、`release_gr_session`、
drain 与 lease 记账、原生暂停钩子，在 `a94830c8` 就全部存在，而那三次跑在它上面的运行同样一条都没走到。
#427 重构的是它们之间**如何交互**，并给会话对象加了一个字段（§1.4），路线本身不是新的。

→ 对判据的影响：**要验证的是"这项能力成立不成立"，不是"#427 有没有改坏它"。**
两者的判据不同 —— 前者问"取消之后 KV 到底还不还"，后者问"和基线比行为变没变"，而基线在这几条路径上的行为**本身就是 bug**（§1.4.3 变化 1）。

### 1.2 被结构性排除的（负向断言，不是不测）

这些场景**永远到不了**被测代码 —— 要么在构造期拒绝，要么根本没有触发条件。但它们仍要覆盖，
覆盖的是**断言不被误触发**。

| 场景 | 判定 | 证据 | 拒绝时机 |
|---|---|---|---|
| `B > 1` | 排除 | `validate_gr_request:163-165` / `GRSessionState.__post_init__:219-221` / worker `validate_dispatch:436-437`，三处独立拒绝 | 首次请求 |
| chunked / partial prefill | 排除 | `validate_gr_execution_config:122-125` + `attach_gr_stage_metadata:345-346` + worker `validate_dispatch` 三层 | 构造期 |
| 抢占后重算 | 排除且触发即硬错 | 调度期 `:353-357` 非连续检查；worker 期 `if key in cached.resumed_req_ids and m.stage_index > 0: raise` | 调度期 |
| 多会话资源争抢 | 排除（串行化，非拒绝） | `_admit_owner:80-95` 单 owner；`_AdmissionQueue:96-121, 146-149` 把非 owner 请求从原生队列**藏起来**，停在 `GRResourceStatus.WAITING` | 运行时 |
| `active_sessions != 1` | 无法设置 | frozen dataclass 默认 1，5 个构造点（`beam_search_v1.py:479`、`gr_async_scheduler.py:276,309`、`gpu_beam_stage_runner.py:732`、`gr_startup.py:53`）无一带此参数 | — |
| `max_tokens ∉ {1,2,3}` | 排除 | `_validate_gr_generation_params:61-66` | 首次请求 |

**"被排除"不等于"不用测"。** 这些硬错本身是防御性断言，长稳要覆盖的是它们在任何组合下都不被误触发。
基线 `a94830c8` 上 `_drain_resources` 能在有在途 dispatch 时摘会话（§1.4.3 变化 1），
说明这类断言曾经**离被踩很近**。

### 1.3 仍需覆盖的四条路径

这四条是 §2~§6 各批次要打的靶子。

| 路径 | 为什么仍会发生 | 关键证据 | 归到哪批 |
|---|---|---|---|
| **取消时仍有在飞 dispatch** | abort 里那句 `_drain_resources` 被 `not session._in_flight_dispatches` 挡住，释放托付给最后一个回执 | `gr_async_scheduler.py:639-648` | 批次 2（§3） |
| **暂停期间终态交付** | GR 把收尾计入 workload 才能不空闲；`_discard_pending_native_finish` 的 discard 分支是 **#403 引入的基线代码**（`338fd6f`，非 #427，见 §1.4.3） | `engine_core_patch.py:449-453, 710-733` | 批次 3（§4） |
| **同 session_id 复用导致迟到 / 重复回执** | 事件会发生（ST 刻意用 `"same-public-id"` 反复请求），影响被三层阻断 | `:515-518`（owner_generation + dispatch_id）、`engine_core_patch.py` 的 `dispatched_sessions` 过滤、`:529-530` 对象同一性 | 批次 2、4（§3、§5） |
| **会话释放窗口内 KV block 仍被 worker 引用** | lease 机制存在的理由；窗口真实存在 | `:365-377` 注释 + `:525-527` 释放 | 判据，§7 |

**已有单测覆盖什么**（长稳的增量是"长时间反复跑 + 组合 + 不漂移"，不是补语义）：

- `tests/test_gr_async_scheduler.py:751 test_native_kv_dispatch_lease_survives_abort_until_receipt`
- `:787 test_dispatch_lease_preserves_native_eviction_order` —— 5 维参数化，断言 free list 顺序与原生 `free()` 完全一致
- `tests/st/test_onerec_v1_scheduler_st.py:401-430` —— 两种时机的取消 + 取消后引擎可复用
- `tests/test_async_scheduling_scheduler.py:407` —— 旧 generation 回执重复投递，断言 native 计数不变

ST 测试的覆盖清单说明作者认为什么重要：N=1/2/3 与 prefix hit/miss 的等价性、
公共结果必须晚于 worker 释放（`:262`）、worker 侧 owner/session/workspace 三段释放归零、
两种取消时机、graph replay 与 eager 双路径、KV block 边界 1023/1024/1025 与 graph bucket 边界。

### 1.4 判据对着哪个语义定：#427 与 #389 前后的差异

**这一节只回答一个问题：写判据时，期望行为该以哪个版本为准。**
答案是目标 commit `9c71c9d` —— 因为基线在几条目标路径上的行为是明确的 bug，照它写判据会把 bug 判成正确。
下面的对照表以 `74c4a9e` 为右列，因为 `git diff 74c4a9e 9fc66d3` **未触及本节的任何文件**
（只动 `vllm_gr/constraints/*`、`vllm_gr/ops/*`、`tests/*`、`.github/*`）。

**#337 动过本节的三个文件（`beam_search_v1.py` +19、`gr_async_scheduler.py` ±55、
`engine_core_patch.py`），所以这张表在 `9c71c9d` 上要重新核，不能沿用"逐行成立"。**
本版重核的结论：**符号级结论全部保持** —— 表里列的每个函数/字段在 `9c71c9d` 上都还在，
`abort_gr_session` 的函数体仍然与 `a94830c8` 逐字相同（只是定义行漂到 `:639-648`），
`has_gr_cleanup_work` 的判据仍只认 `terminal_result_pending` +
`DRAINING`/`RETIRING` 两个状态（`:64-73`，未变）。
#337 改的是**周边**：新增 V2 并存路径、把几个 helper 拆出来共用、把 `persistent_install`
参数化。**判据取 `9c71c9d` 的依据不变：基线里那些行为是 bug。**
另：右列标 "= `9fc66d3`" 仍然成立 —— 因为上面对 `74c4a9e`→`9fc66d3` 的 diff 范围声明本身没变。

#### 1.4.1 基线存在性对照

`git show a94830c8:vllm_gr/v1/engine/{beam_search_v1,gr_async_scheduler}.py`：

| 符号 | `a94830c8` | `74c4a9e` 的变化（= `9fc66d3`） |
|---|---|---|
| `abort_gr_session` | `:632-641` 存在 | **函数体逐字相同**（仅 import 空行差） |
| `release_gr_session` | `:644-654` 存在 | 同上 |
| `_in_flight_dispatches` | `:382, 494, 497` | 相同语义 |
| `_kv_dispatch_leases` | `:378` | 构造方式改为单遍（性能） |
| `_pending_terminal_result` | `:529, 543, 576` | 相同语义 |
| `_gr_v1_sessions` 表 | `engine_core_patch.py` 5 处已读 | **表本身是旧的**，只是前几轮没探测 |
| `GRResourceStatus` 五态 | `beam_search_v1.py:38` | 相同 |
| `GRExecutionCapacity(active_sessions=1, dispatches_per_session=2)` | `:47-63` | 相同 |
| `has_gr_cleanup_work` | `gr_async_scheduler.py:64` | 相同 |
| native pause 钩子 | `tests/test_gr_pause_resume.py` 已存在 | 只改了 2 处断言 |
| `_dispatch_records` | **不存在** | **新增**（`beam_search_v1.py:209`） |
| `is_gr_session_retired` | **不存在** | **新增**（`vllm_gr/v1/engine/core.py:240-242`，由 `engine_core_patch.py:191` 装到 `EngineCore`） |

#### 1.4.2 #427 真正新增的东西

**一个字段 + 一个查询方法。**

字段是 `GRSessionState._dispatch_records: dict[int, GRStageMetadata]`（`beam_search_v1.py:209-211`），
只增不删的 dispatch 账本。方法是在 `vllm_gr/v1/engine/core.py:240-242` 新增、
由 `engine_core_patch.py:191` 装到 `EngineCore` 上的 `is_gr_session_retired(session_id)` ——
**批次 2 的整个驱动就靠它**（`gr_session_client.py:182-187` 的泵循环），
而它在 `a94830c8`、`338fd6f`、以及工作区里都不存在。字段替代了基线的「一组 host 侧标量 + 一个可变引用」：

| | 基线 | 74c4a9e |
|---|---|---|
| stage 身份 | `stage_index = session.issued_stages`（会被 drain 路径改写） | `num_computed_tokens - num_prompt_tokens`（native 进度当唯一真值） |
| 设备依赖链 | `session.latest_device_state` 是可变的、issue 时被覆盖 | 由 `_dispatch_records` 派生（`:244-248`） |
| 回执匹配 | 只查 `dispatch_id not in _in_flight_dispatches`（无对象同一性） | 对象同一性校验：`if session._dispatch_records.get(id) != metadata: raise`（`:529-530`） |
| placeholder 预算 | `reserve_gr_output_placeholder` / `_sync_output_placeholders` / `confirmed_token_count` | **整套删除**，由 native `num_output_placeholders` 接管 |

**四个新文件**：

| 文件 | 职责 | 离线会走到吗 |
|---|---|---|
| `v1/worker/gr_execution.py` | `worker_input()` detach 可变引用；`execute_gr_stage` / `execute_gr_retirement` 的设备路由入口 | **会**（`model_runner_common.py:1855-1863` 调用） |
| `v1/worker/gpu_prefill_inputs.py` | 单请求 Prefill 输入特化，带 11 条 decline 条件；`None` 时回落原生 | **会**（`gpu_model_runner_patch.py:206`） |
| `ops/cuda/gr_prefill_inputs.py` | 上述快路径的元数据 Triton kernel | 经上者到达 |
| `npu_spawn_patch.py` | NPU 感知的 spawn 强制（`_maybe_force_spawn` 双绑定） | 安装会走到，**效果只在 NPU 上发生**，与 GR 生命周期无关 |

**三条构造期 / 首次请求硬契约**：`B=1`、`enable_chunked_prefill=False`、`ignore_eos` 必须显式设置、
`max_tokens ∈ {1,2,3}` —— 见 §1.2 表格。R1-427 就是因为其中两条而改了配置才跑起来的。

#### 1.4.3 六处控制流变化

这是 #427 里**唯一**需要长稳覆盖的语义变化，也是 §1.4 开头那句"判据要对准 74c4a9e"的实质原因。

| # | 变化 | 位置 | 语义 |
|---|---|---|---|
| 1 | **释放需要"终态已交付"** | `_drain_resources` `:163-170` | 从 `if not worker_resources_allocated` 加两个条件：`and not _in_flight_dispatches and not terminal_result_pending`。基线**能在有在途 dispatch / 待交付终态时就把 session 摘掉**（丢终态、丢租约） |
| 2 | **退休帧触发条件反转** | `attach_gr_retirement` `:183-188` | 旧：有终态挂起就不发帧。新：只要 worker 还持资源就发。这是"终态可穿过退休往返"的前提 |
| 3 | **退休不再无条件销毁会话** | `consume_gr_retirement` `:224-228` | 旧无条件 `_release_owner`；新仅在 `not terminal_result_pending` 时释放，并要求 `engine_core_patch.py:877-878` 把会话塞回 `updated_sessions` 再走一次交付 |
| 4 | **stage 身份改由 native 进度推导** | `:345-358` | 加两条硬错：非连续 native 进度、已生成后重算。`is_last_stage` 从 `not prefill_chunk and ...` 变为纯 `stage_index == total-1` |
| 5 | **draining 分支移到回执校验之后** | `:511-527` | 基线在 `_read_worker_payload` **之前**处理 draining；现在畸形回执在 draining 状态下**算协议失败**，且 `_drain_resources` 每帧无条件调用 |
| 6 | **协议失败覆盖早先的成功终态** | `_fail_gr_dispatch` `:568` | 旧：无条件置 `FINISHING`（会覆盖 ABORTED/FAILED）。新：已取消/已失败的会话不被翻回；但**已缓冲的成功终态会被协议失败覆盖** |

配套（`engine_core_patch.py`，是 1/3 成立的前提）：`dispatched_sessions` 过滤加
`owner_generation` + `dispatch_id` 双重校验（"Stale results must not reach native token accounting,
even if a new owner reuses the ID"）。

**订正（先前记错）**：先前把 `_discard_pending_native_finish(..., worker_released=True)` 里的
`finished_req_ids.discard(...)` 记成 #427 新增 —— **不是**。它是 **#403（`338fd6f`）** 引入的：
`a94830c8:engine_core_patch.py:707` 的函数签名还没有 `worker_released` 参数，`338fd6f:707-727`
已经完整（`git log -S` 唯一命中 `338fd6f`），而 `338fd6f` 正是工作区 HEAD 的直接父提交；
`git diff 338fd6f 9fc66d3` 未触及该函数，`git diff 9fc66d3 9c71c9d` 也未触及
（函数体逐字相同，调用点在两版上都是 `:803`）。所以它是**基线就有**的收敛保障，不是本次新增。
§4.1 据此重新表述，判据不变。

**变化 1、2、3 合起来是同一个机制**：基线允许"会话已经摘掉了，但 worker 还拿着它的资源和 KV"。
取消路径与暂停路径都会撞上这个窗口，所以 §7 的租约不变量判据是这几条路径的共同下游。

### 1.5 #389：本次唯一的行为性变更

**`9fc66d3` 相对 `74c4a9e` 只多做了一件事，但它是数值语义的改动，不是管线改动。**

```
$ git diff --stat 74c4a9e 9fc66d3
 .github/workflows/pre-commit.yml            |  25 +-
 tests/constraints/test_cpu_backend.py       |  24 +-
 tests/constraints/test_cuda_backend.py      |  97 +++++++-
 tests/constraints/test_loader.py            |  52 ++++-
 tests/constraints/test_scoring.py           |  91 ++++++++   ← 新增
 tests/kernels/test_rec_constrained_topk.py  |  72 ++++-
 tests/kernels/test_rec_constrained_topk_cuda.py | 296 ++++++++++++++-
 vllm_gr/constraints/backend.py              |   8 +-
 vllm_gr/constraints/cpu_backend.py          |  36 ++-
 vllm_gr/constraints/cuda_backend.py         |   5 +-
 vllm_gr/constraints/execution.py            |   2 +-
 vllm_gr/constraints/loader.py               |  19 +-
 vllm_gr/constraints/scoring.py              |  94 ++++++++++  ← 新增
 vllm_gr/ops/cuda/rec_constrained_topk.py    |  56 ++++-
 vllm_gr/ops/rec_constrained_topk.py         |  76 ++++--
```

**改了什么。** 约束表（Constraint Table）限制哪些 token 能活过 Top-K，但 beam search 要比较
**不同父行**选出来的候选，它们的分数必须落在同一个词汇域里。旧实现按**每行自己的合法候选集**归一化，
于是"只有 1 个合法 token 的行"永远返回 logprob 0，与模型置信度无关，跨父行不可比。
新契约按**完整的共享 logits 窗口**归一化。证据：

- 新文件 `vllm_gr/constraints/scoring.py`（94 行，`score_selected_tokens`），文档字符串第一段就是上面这段理由；
- `vllm_gr/constraints/cuda_backend.py` 在 V1 固定地址路径上新增注释：
  「Fixed-address (V1 Worker) path: the fused kernel now outputs full-window normalized logprobs
  directly (the scoring contract), **so nothing needs rescoring here**」；
- `vllm_gr/constraints/loader.py` 的启动就绪检查改用 `normalization_size=vocab_size`（CUDA）
  而不是 `candidate_count`，NPU 保持旧域；
- `vllm_gr/constraints/backend.py` 的 `INVALID_LOGIT` 从 `ops/rec_constrained_topk` 挪到 `scoring`，
  并由 `execution.py` 转引。

**为什么这条落在 soak 的实测路径上。** `run_soak.py` 的 `build_v1_constraint_config` 设
`constraint_backend: "cuda"` + `execution_mode: "v1"`，走的就是 CUDA worker 的
`build_output=False` 那条固定地址分支 —— 也就是上面那句注释说的那条。

**为什么现有 harness 看不见它。** `run_soak.py:303-333` 的 `fingerprint()` **只哈希 token id**
（full 形式哈希每个 token；cheap 形式打包 `(len, first, last, sum)`），**不含 logprob**。后果：

- #389 把分数改坏、而 token 序列碰巧不变 → 长稳与看板全绿，什么都没测到；
- token 序列变了 → golden baseline 是**每次运行内部自建**的（`run_soak.py:1117-1124`，
  首次 golden 请求的指纹即基线），所以也不会失败，跨运行对照时只表现为一个**无法归因的 diff**。

**分工。** 数值正确性单测能构造畸形窗口（全 `-inf` 域、`+inf` 域、NaN 窗口、vocab offset 切片），
长稳构造不了；长稳能做的、单测做不了的是下面三件。三者都在批次 1 落地、在批次 5 验收。

| # | 长稳要验的 | 怎么验 |
|---|---|---|
| 1 | 5 小时混合负载下 logprob **不漂移** | 把**量化后的每个 beam 的累计 logprob** 纳入 `fingerprint()`，与 token 一起哈希。V1 **不暴露 per-token logprobs**（`vllm_gr/entrypoints/beam_search_v1/output_processor.py:132` 把 `logprobs` 写死成 `[]`，`:240` 又给 `CompletionOutput` 传 `logprobs=None`），每个 beam 只有 `cumulative_logprob`（`:239` 从 `sequence.cumulative_logprob` 取），够用 |
| 2 | 正常负载下**从不触发**协议错误 | `scoring.py` 里 NaN 是**故意保留**的非有限值，用来把被污染的窗口升级成 `beam_device_error`（不是 bug）。长稳要断言这个错误在正常负载下出现 0 次 —— 出现了就是真信号 |
| 3 | **跨父行可比性**在 hit/miss、bucket 边界下保持 | 同一次请求内所有 beam 的累计 logprob 必须落在同一尺度（这正是 #389 的目的，得有人验它成立），且与 `outputs[i].sequences[j].cumulative_logprob` 的排序一致 |

**不要试图在长稳里复现归一化域的数值。** 那是单测的活：`tests/constraints/test_scoring.py`、
`tests/constraints/test_cuda_backend.py`、`tests/kernels/test_rec_constrained_topk_cuda.py`。
长稳只回答"这条契约在长时间、多形状的真实负载下会不会漂"。

---

## 2. 批次 1：探针基础设施

### 2.1 必要性

**批次 3 强制多进程**（§4.3），而多进程会让现行的对象图探针**全部静默变成 `null`** —— 不报错，
只是没有数据，报告里读起来像"这一项是干净的"。

| 探针 | 多进程下 | 原因 |
|---|---|---|
| `scheduler.requests` / `.running` / `.waiting` / `.finished_req_ids` | **消失（`null`）** | `engine_core` 变成 `SyncMPClient`，`MPClient` 里**没有任何 `.scheduler` 属性** |
| `_gr_v1_sessions` | **消失** | session 表在 EngineCoreProc 子进程里 |
| `_beam_sessions` | **消失** | 同上 |
| `_gr_v1_native_update_request_ids` | **消失**，且"读到 None"被当作 absent 而非 skip → **该断言会静默变成真空通过** | 同上 |
| beam registry（BFS） | **`not_found`** | registry 只在 worker 的 `_beam_context` 里，随 EngineCoreProc 在子进程 |
| `torch.cuda.memory_allocated/reserved/stats` | **无意义** | driver 进程不跑模型；计数器是进程局部的 |
| `gc_counts` / `gc_tracked_objects` | 可读，但**测的是 driver 的堆** | 引擎侧 GC 对象在子进程 |

**失败形态是静默的 `null`** —— 这正是 `tools/soak/README.md:248-258` 已经吃过两次亏的那个坑。
所以报告必须显式区分「读到 0」与「读不到」。

**第二个必要性是 RSS 会采错进程。** 上游默认 `VLLM_WORKER_MULTIPROC_METHOD="fork"`
（部署 `envs.py:883-884`；声明在 `:67`），
**fork 出来的 EngineCoreProc 子进程 cmdline 与父进程逐字相同**，
而 `find_container_pid` 取 `docker top` 的**第一条** cmdline 匹配 —— 可能整个 5 小时都在采错进程，
且两次运行可能不同。这与 `soak_watchdog.py:320-330` 已记录过的事故是同一类。
不修的话，批次 3 的内存结论无效。

### 2.2 代码在哪

**探针的获取路径**（`run_soak.py`）：定义在 `locate_engine_core(:528-545)`、
`scan_containers(:504-526)`、`select_probes(:386-...)`，而 `ProbeThread.sample` 里的调用点在
`:761-771`（先前把调用点写成了定义位置，行号会指到别处）；beam registry 用 BFS 靠 `vars()`
找同时含 `_slot_map` 与 `_free_slots` 的对象；`_find_gr_sessions` 走
`resolve_path(engine_core, "scheduler._gr_v1_sessions")`（`:651-665`）。

**watchdog 的采集路径**：`read_proc(pid)`（`soak_watchdog.py:177-214`，单 pid，无子进程遍历）、
`find_container_pid(container, pattern)`（`:320-350`）。cgroup 只用于 CPU（`:267-290` 只拼 `cpu.stat`，
**没有 `memory.current`**）。唯一没被漏掉的显存口径是 `--query-compute-apps`（`:414-431, 479`），
它覆盖**所有** compute 进程的显存。

**跨进程查询的现有范式**：`vllm_gr/v1/engine/core.py:240-242` 的 `is_gr_session_retired`，
经 `engine_core_patch.py:191` 装到 `EngineCore`，前端用 `call_utility` 调它
（`gr_session_client.py:178-187` 已经在这么干）。新探针**已照这个范式加**（§2.3 ①），
提交 `cd0a28d`。**注意 `gr_session_client.py:182-187` 里那个 if/else 就是双口径判别的原型**：
`hasattr(core, "engine_core")` 为真走进程内直读，为假走 `call_utility` —— 与 §2.3 ② 用的是同一个判据。

**driver 侧本来就有、不需要改引擎的等价物**（`SchedulerStats`）：EngineCore 子进程每步构造并推给 driver
（`core.py:1814-1817`），driver 侧 `llm_engine.logger_manager` 能拿到（`llm_engine.py:314, 318`）。
字段（`vllm/v1/metrics/stats.py:171-183`，`scheduler.py:1992` 起构造）：

- `num_running_reqs` / `num_waiting_reqs` / `num_skipped_waiting_reqs`
- **`kv_cache_usage`** ← 正是"离阈值多远"的那个数，**单进程探针里也没有**
- `prefix_cache_stats`、`kv_cache_eviction_events`（逐块驱逐事件，需打开 kv cache metrics）
- `IterationStats.num_preempted_reqs`（抢占计数，`stats.py:325-336`）

日志是零改动路径：`LoggingStatLogger.log`（`loggers.py:217-283`）输出
`"Running: %d reqs, Waiting: %d reqs, Preemptions: %d, GPU KV cache usage: %.1f%%, Prefix cache hit rate: %.1f%%"`。
MP 下由子进程打到同一 stdout → 进容器日志，soak 抓日志即可补回三类信号。

**没有替代品的**：`_gr_v1_sessions` 的生命周期字段、`_kv_dispatch_leases`、beam registry 槽位、
`scheduler.requests` 的单调增长 —— 只在 EngineCore / worker 进程内，**只能在 EngineCore 侧新增打点**。

### 2.3 怎么测

**目标**：让同一份判据在单进程与多进程两种配置下都读得到、且读出来的东西可以并排比较。

**① Core 侧新增可查询的探针摘要 —— 已实现。** 落在新文件 `vllm_gr/v1/engine/soak_probe.py`
（228 行，纯查询/纯计数，无行为分支），对外只暴露一个方法：

```python
# vllm_gr/v1/engine/core.py —— 挨着 is_gr_session_retired（:240）放
def gr_probe_snapshot(self: Any) -> dict[str, Any]:
    from vllm_gr.v1.engine.soak_probe import snapshot
    return snapshot(self)
```

安装方式照抄 `is_gr_session_retired`：在 `engine_core_patch.py` 里
`EngineCore.gr_probe_snapshot = gr_probe_snapshot`（紧跟 `:191` 那行），并在
`apply_engine_core_request_patches` 与 `apply_engine_core_child_patches` 两处都调 `soak_probe.install()`
—— 前者走 `run_engine_core → initialize_engine_core_beam_patches()`（MP 口径），
后者走 `patch_batch_and_fork → apply_batch_fork_patches()`（进程内口径），两条路径都要盖到。

**比草案多的三样，都是被实测逼出来的：**

- **`counters` + `counters_installed`**（§2.3 ⑥）—— 计数器的值必须能跨进程读出来，而且
  「没装过」要和「是 0」分开报。
- **`session_count`** —— 快照里的 `session_ids` 是给对账用的，`session_count` 才是单调量。
- **`free_blocks` 读不到就是 `None`，绝不返回 0** —— block pool 在 MP 下可能拿不到，
  报 0 会被误读成「KV 全空」。

**这是本方案唯一需要改引擎代码的项，而且只加查询、不加逻辑。** 改动单独成一个提交：
服务器 worktree `~/vllm-gr-soak` 分支 `soak/probe-9c71c9d`
（**不在** `~/vllm-gr-decode-graph` 里改 —— 那个工作区是脏的，见 §9），探针提交 `cd0a28d`
（= `9c71c9d` + 探针）、**`561ebbb`**（补 `has_work` 三要素，§2.3.3）、
**`1f28138`**（当前快照）。`SOAK_REV` 取快照实际用的那个 rev。
**注意探针提交会挪 `engine_core_patch.py` 的行号**
（本版实测 +13 行），对行号要按 `9c71c9d` 数，不要按快照目录数。

**快照 rev 的时间线（批次 2 起换到 `1f28138`）：**

| 批次 | 快照 rev | 理由 |
|---|---|---|
| 1（冒烟 + 双口径门） | `cd0a28d` | 当时 tip；门的证据只对得上这五个字段（§2.3.5） |
| **2 / 3 / 4 / 5** | **`1f28138`** | 要 `has_work` 三要素与每会话资源字段。`source/` 已在服务器上换成 `1f28138` 的 `git archive`，旧的留在 `source-cd0a28d`；`soak.env` 与 `soak.mp.env` 的 `SOAK_REV` 都已改成 `1f28138` |
| （标定） | `1f28138` | `calib_k.py` 的两次跑都在这个快照上 |

实测两个 diff（`~/vllm-gr-soak`，分支 `soak/probe-9c71c9d`）：

```
git diff --stat cd0a28d..1f28138   ->  soak_probe.py | 68 +, 2 -
git diff --stat 9c71c9d..1f28138   ->  core.py 13 +, engine_core_patch.py 13 +, soak_probe.py 294 +
                                      3 files changed, 320 insertions(+), 0 deletions(-)
```

`cd0a28d → 1f28138` 只动 `soak_probe.py`；相对锚点 `9c71c9d` 的全部探针改动是
**320 行纯插入、0 行删除**。所以快照与锚点在**引擎行为**上一致（插的都是查询挂钩），
批次 1 的门结论对批次 2–5 仍然成立 —— 但**字段集**不同，
引用字段时仍须按上一段的规矩引对版本。

**② harness 分层派发两条口径 —— 已实现。** `run_soak.py` 新增 `engine_core_client()` /
`read_gr_probe()`：先看 `llm.llm_engine.engine_core` 上有没有**可调用的 `call_utility`**，
有就走 MP（`call_utility("gr_probe_snapshot")`），没有就退到对象图直接调 `gr_probe_snapshot()`，
两者都没有就报 `caliber="unavailable"`。**两边输出同一组字段名**，`meta.json` 里直接并排。

判别依据实测过 `vllm 0.22.1` 的四个客户端类：`EngineCoreClient` / `InprocClient` / `MPClient`
**都没有** `call_utility`，只有 `SyncMPClient` 有；`AsyncMPClient` 只有
`call_utility_async`（返回协程）。**所以只能查同步的 `call_utility`** —— 查到
`call_utility_async` 就当没有，否则会把协程当成快照，比较成 dict 后静默读成「没有会话」。

**③ 报告区分「读到 0」与「读不到」—— 已实现。** 全部探针列都单开一栏 `null`，不与 0 混在一起；
`meta.json` 另存 `probe_caliber`（`{"in_process": N}` / `{"multiprocess": N}`）、
`probe_reads`、`probe_errors`（逐条异常文本）。**读失败必须留痕**：utility 通道卡死时
`call_utility` 是阻塞调用，异常就发生在那里。

**④ watchdog 修掉 fork 同名采错进程 —— 已实现。** 优先用 cgroup 归属定位（`/proc/<pid>/cgroup`
匹配容器 id），拿不到再退到 cmdline，并与 `--query-compute-apps`（`:414-431, 479`）交叉验证。

**⑤ 把 logprob 纳入指纹（§1.5 的落地）—— 已实现。** 原来 `fingerprint()`（`run_soak.py:303-333`）
只哈希 token id，所以 #389 改了分数而 token 序列不变时它一声不吭。现在：

- cheap / full 两种形式都加一路：每个 beam 的 `cumulative_logprob` 先经 `_quantize_logprob()`
  量化（`round(x * 1000)` 转整型，避免末位浮点噪声把指纹打散），再按 beam 顺序打包进同一个 sha256；
- 输出结构里该字段来自 `output.sequences[j].cumulative_logprob`
  （`vllm_gr/entrypoints/beam_search_v1/output_processor.py:239`；V1 不暴露 per-token logprobs，`:132`）；
- **量化精度要显式记录在 `meta.json`**，否则下一轮换了量化步长，指纹就不可比而没人知道。

`None` / `NaN` / `+inf` / `-inf` / `0.0` 是**五个互不相同的哨兵值**：`None` 表示「没报 logprob」，
不能与 0.0 撞；`NaN` 是约束层**故意保留**的信号（§1.5），不能与「缺失」撞。
同一条配套断言：**同一次请求内，`cumulative_logprob` 的降序必须与 `output.sequences` 的顺序一致**
（`logprob_order_ok()`），且所有 beam 落在同一数值尺度上 —— 这是 #389 的目的（跨父行可比），
也是它唯一能被长稳抓住的失效形态。**注意 `logprob_order_ok()` 返回三值**：`True` / `False` / `None`，
`None` 表示分数缺失所以无法判定，**不能折叠成 `False`**（前者是盲区，后者是故障）。

**⑥ Core 侧探针加一组单调计数器（代码路径的"正例"）—— 已实现。** §3~§7 的判据全是负向的
（不泄漏、不挂死、返回 `True`）。这类判据在"目标代码根本没被走到"时**同样全绿**。具体漏法：
`vllm_gr/v1/worker/gpu_prefill_inputs.py` 里 5 条 `return None` 覆盖 11 条以上 decline 条件，
静默回落到原生 prefill 时，五小时的报告与"快路径全程命中"**逐字相同**。所以批次 1 的判据
从"探针非零"升级为"**代码路径非零**"：

| 计数 | 挂在哪 | 期望 |
|---|---|---|
| `gr_prefill_fastpath_hits` / `gr_prefill_declines` | `gpu_model_runner_patch.try_prepare_gr_prefill_inputs` 的**绑定名**（`gpu_model_runner_patch.py:20` 导入、`:206` 调用） | hits > 0；declines 变化时报告要解释 |
| `gr_retire_frames` / `gr_retire_consumed` | `gr_async_scheduler.attach_gr_retirement`（`:181`）/ `consume_gr_retirement`（`:201`） | 两者相等；**批次 1 的负载下都可以是 0**（见 §2.3.4） |
| `gr_protocol_failures` | `gr_async_scheduler._fail_gr_dispatch`（`:568`） | **恒 0** —— 非零即真信号 |
| `gr_dispatch_records` 峰值 | `gr_probe_snapshot` 的 `dispatch_records`，折叠进 `gr_probe_totals_max` | 有界（不单调上升） |

三条实现约束，每条都对应一个会静默失效的写法：

1. **打的是绑定名，不是定义模块。** `gpu_model_runner_patch.py:20` 在 import 时就把
   `try_prepare_gr_prefill_inputs` 绑成了模块全局，之后调用走的是那个引用；去改
   `gpu_prefill_inputs` 里的定义是**无操作的静默空转**。
2. **计数器不加环境变量开关。** 开关忘了打开，就正好复现计数器要抓的那种静默回落。
3. **`install()` 幂等且逐个 try/except。** 重复调用不能把 wrapper 叠起来（`functools.wraps`
   会拷贝 `__dict__`，标记位必须在 `wraps()` **之后**打，否则被原函数的 `__dict__` 冲掉）；
   某个模块 import 失败也不能让引擎起不来。

`gr_device_errors` 没有单独做计数器 —— #389 的失效形态已经由指纹的 `NaN`/`+inf` 哨兵和
`logprob_order_ok()` 覆盖（§1.5），再加一个计数器是重复。

**顺带可选**：把 `SchedulerStats` 里的 `kv_cache_usage` 与 `num_preempted_reqs` 一起接进来 ——
driver 侧就能拿到，成本很低，而且是单进程口径下也没有的两个数。

#### 2.3.1 验证方式

同一负载分别用 `VLLM_ENABLE_V1_MULTIPROCESSING=0/1` 各跑一次 30 min。跑之前先离线过一遍
`python3 tools/soak/selftest_probe.py`（**不需要 GPU、不需要 vllm**：`run_soak.py` 只在
`main` 和 `ProbeThread.sample` 里 import vllm）—— 23 项全过才开跑。这一步不能省，因为
这些 helper 决定五小时的指纹是否有意义，而**坏掉的 helper 不会报错，它只是给出一个错的 digest**。

#### 2.3.2 判据

多进程下 `session_ids` / `by_status` / `in_flight` / `leases` 有**非零**读数
（全 0 说明探针没接对，不是"干净"）；`probe_caliber` 在两种口径下分别报出 `in_process` / `multiprocess`
且 `probe_errors` 为空；`gr_probe_counters.installed` 为 `true` 且 `gr_prefill_fastpath_hits` 增长；
watchdog 能**按 cgroup 归属**定位到容器内的进程（`pid_found … source: "container"`），
**RSS 是 driver 与 `EngineCoreProc` 两进程之和**而不是单取一个（`proc_count` / `pids` 一并记录：
`fork` 下父子 cmdline 逐字相同，只取一个会随调度掷硬币，RSS 曲线的可复现性就没了 —— 这正是
批次 3 内存论证的支点；`proc_count` 从 2 掉到 1 是**独立发现**，不等于 RSS 下降）；
**两种口径各报一个落在自己池子里的 `free_blocks`**；
**（这条判据有一个前置条件，2026-10-08 实测踩到了：`--pid-pattern` 必须能匹配到 `EngineCoreProc`。
MP=1 下 vLLM 把它的进程名改写成 `VLLM::EngineCore`，用 `run_soak.py` 去匹配只拿到 driver，
`proc_count` 恒为 1、RSS 少掉 2.9 GB。pattern 写成 `run_soak\.py|VLLM::EngineCore` 才对。
详见 §2.3.5）**；
**（`free_blocks` 这条判据改过两次，2026-10-08。第一次从"两口径一致"改成"各报一个落在自己池子里的值"，
理由是 MP=0 报 `158,000 tokens` / MP=1 报 `157,136 tokens`，看起来是口径的确定性后果。
**这个理由当天下午就被证伪了**：`calib_k.py` 的 MP=0 跑也报 `157,136 tokens`
（`Available KV cache memory: 16.79 GiB`，与 `mp1b-val30` 逐位相同）。同一个口径给出两种池子，
所以 54 块的差额**不是口径造成的，是启动抖动**。三跑报的 `Estimated CUDA graph memory` 都是
`0.03 GiB`，唯一的可见差异是 `Initial profiling/warmup run took`：mp0 那一跑 `0.23 s`，
另外三跑 `1.21–1.24 s` —— 量级对得上（0.09 GiB ≈ 54 blocks × 1.75 MB）。
**真正的后果比第一次订正更硬**：`meta.json` **不记录 `GPU KV cache size`**
（只有 `block_size`），池子只存在于每次跑的启动日志里；所以"回落到基线"这类判据
必须拿**同一次跑自己的基线**比，既不能跨口径比、也不能跨跑比。
实测见 §2.3.5）**；
两种口径的 logprob 指纹**不要求相等**（#389 只保证同一次运行内稳定）。

**计数器还有一条容易被忽略的判据：单调性。** 计数器只增不减，**掉头就是信号**（进程重启、
计数器被重置、读到了另一个引擎），`meta.json` 记 `gr_probe_counters.regressed`；
掉头时保留第一个值，不吸收新值。

#### 2.3.3 实现与自测状态

| 项 | 状态 | 证据 |
|---|---|---|
| `soak_probe.py` + `EngineCore.gr_probe_snapshot` | 已提交 | `cd0a28d`（服务器 `~/vllm-gr-soak`，分支 `soak/probe-9c71c9d`） |
| 探针补 `has_work` 三要素与资源归属 | 已提交 | `561ebbb` —— `has_gr_cleanup_work` / `scheduler_has_requests` 在**引擎进程内**求值（`None` = 问不到，不折叠成 `False`）、`pause_state`、每会话 `worker_resources_allocated` 与 `retirement_pending`。**批次 3 判据要的是 `has_work()`，而它和 `by_status` 不是一回事**（§4.4） |
| `run_soak.py` 双口径派发 + 计数器折叠 | 已实现 | `engine_core_client()` / `read_gr_probe()` / `_register_probe_read()` |
| `selftest_probe.py` | 23 项全过 | 覆盖：口径分派、异步客户端不误判、裸引擎报 `unavailable`、通道卡死报错、计数器折叠/回退/错误计数 |
| `run_soak.py` 记 effective ModelRunner | 已实现 | 启动日志 `model runner: VLLM_USE_V2_MODEL_RUNNER='0'` —— 只在引擎构造**之后**读，否则读到的是插件的写入前的值（§0.2 ⑦） |
| 探针启用的冒烟跑 | **通过** | `smoke2-probe`，见 §2.3.4 |
| 30 min 双口径验证跑 | **通过**（五项判据全过，一处判据被证伪后订正） | `mp0-val30` / `mp1b-val30`，见 §2.3.5 |

**本批次门跑的两处刻意偏离**，都记在这里而不是藏在命令行里：

1. **`--warmup-seconds 120`（默认 900）**。默认相位下 `warmup + duration = 45 min/口径`，
   两口径就是 90 min。这道门问的是"两种口径下探针读到同一组字段"，不是时延判决，
   默认 warmup 的相位语义在这里不适用；120 s 已经远过图捕获（冒烟跑里引擎在
   `t≈83 s` 就绪）。跑的仍然是 **1800 s 实测时长**，与 §2.3.1 一致。
2. **`VLLM_ENABLE_V1_MULTIPROCESSING` 用第二个 env 文件而不是命令行**，
   因为 `run_soak_container.sh` 启动时会自己 source `soak.env`（`SOAK_ENV_FILE`
   之前先 source 默认路径），命令行 export 会被它覆盖回去。所以是
   `soak.mp.env` = `soak.env` 仅翻转该行，配 `SOAK_ENV_FILE=soak.mp.env` 启动。
   直接 export 看不到任何报错，只是**静默还是跑单进程**。

   **这里踩到一个命名陷阱，记下来。** tooling 目录里原本已经有一个
   `soak-mp.env`（连字符），是 9 月 23 日那次多进程对比留下的，`SOAK_REV=a94830c8`
   —— 和 `soak.mp.env`（点）只差一个字符，内容却是**旧 commit 的快照**。
   批次 3 必须跑 MP，随手拿错一个就会**静默测旧代码**，而且跑完的 `meta.json`
   里 `revision` 字段会明明白白写着 `a94830c8`，没人会去看。
   已改名为 `soak-mp.env.pre427-stale`（只有 `val30.sh` 引用过 env 文件名，
   引用的是点号那个，改名无副作用）。批次 3 起跑前先对一遍 `SOAK_REV`。

#### 2.3.4 冒烟跑的实测对账（2026-10-08，`cd0a28d`，180 s）

```
meta.json:
  revision          = "cd0a28d"
  probe_caliber     = {"in_process": 11}     probe_reads = 11     probe_errors = []
  gr_probe_counters = {installed: true, regressed: false,
                       first: {gr_prefill_fastpath_hits: 1,   gr_prefill_declines: 0,
                               gr_retire_frames: 0, gr_retire_consumed: 0, gr_protocol_failures: 0},
                       last:  {gr_prefill_fastpath_hits: 847, gr_prefill_declines: 0,
                               gr_retire_frames: 0, gr_retire_consumed: 0, gr_protocol_failures: 0}}
  prefill_fastpath  = {hits: 1116, declines: 0, requests_observed: 1116, status: "installed"}
  gr_probe_totals_max = {session_count: 1, dispatch_records: 3, in_flight: 2, leases: 2}
  probe_lifecycle   = {statuses_seen: ["RUNNING"], transitions_seen: [],
                       retire_frames: 0, retire_consumed: 0}
```

**两个独立来源对上了**：引擎侧 `gr_probe_counters` 报 847 命中，客户端侧
`PrefillFastpathCounter` 报 1116 命中，两侧 `declines` 都是 0。数量不同是预期的 ——
引擎侧计数器在引擎起完之后才装，前一小段请求不在它的计数里；**两侧都非零且都零 decline**
才是这一项要的证据（其中任一侧为 0 就说明那条链路没走通）。

**同时暴露了一个必须写下来的盲区：这批负载一次退休都没发生。**
`gr_retire_frames = gr_retire_consumed = 0`，而 `statuses_seen` 只有 `RUNNING`。
两个独立来源（对象图采样、引擎侧计数器）给出同一结论，所以这不是采样漏了，是**负载真的没走到**。
正常完成路径的收尾不走退休帧，退休帧是取消/暂停路径的产物 —— 也就是说
**§2.3 ⑥ 里「两计数相等且都 > 0」这条判据在批次 1 根本不该要求**，
它的归属是批次 2~4。已记入 §8。

#### 2.3.5 双口径验证跑（2026-10-08，`cd0a28d`）

脚本 `results/soak/val30.sh`，两口径串行（一把卡，启动器自己会拒绝并发第二个 soak）。
每口径 `--warmup-seconds 120 --duration-seconds 1800`，启动方与偏离理由见 §2.3.3。

| 口径 | run id | env | 结果 |
|---|---|---|---|
| 单进程 | `mp0-val30` | `soak.env` (`VLLM_ENABLE_V1_MULTIPROCESSING=0`) | 32 min，五项判据全过（唯一例外：`free_blocks` 判据本身写错了，见 §2.3.2） |
| 多进程 | `mp1-val30` | `soak.mp.env` (`=1`) | **第一次跑崩了**（见下），`meta.json` 没写成 |
| 多进程（重跑） | `mp1b-val30` | 同上 + `input send lock: installed` | 32 min，`14,247` 请求对，崩溃未复现，判据全过 |

#### 第一次 mp1 崩塌：探针线程与请求线程抢同一个 ZMQ socket

`mp1-val30` 在 12:02:32、跑到第 8385 个请求时，**EngineCoreProc 自己挂了**：

```
(EngineCore pid=6355) Traceback (most recent call last):
(EngineCore pid=6355)   File ".../vllm_gr/v1/engine/core.py", line 947, in process_input_sockets
(EngineCore pid=6355)     request_type = EngineCoreRequestType(bytes(type_frame.buffer))
(EngineCore pid=6355) ValueError: b'\x00\x00' is not a valid EngineCoreRequestType
```

**根因是 harness 自己。** 部署的 `SyncMPClient` 全程**没有一把锁**（`core_client.py` 全文
grep 不到 `Lock`），`_send_input` 谁来调就用谁的线程；而这个 harness 恰好引入了**第二个写者**：
`ProbeThread`（`run_soak.py` 的 `threading.Thread`）每隔 `--probe-interval` 秒调
`read_gr_probe` → `call_utility` → `_send_input`，同时主线程正在请求循环里调 `step()`。
ZMQ socket 不是线程安全的，两边的帧交错，Core 侧就把类型帧解成了 `b'\x00\x00'`。

这不是"偶发怪事"，是**结构性的**：只要 MP 口径下用线程采探针，跑够久必然撞上。
第一次崩在 20 分钟，正好是 35 个采样点之后。

**修法**：`run_soak.py` 新增 `install_input_send_lock(llm)`，把 driver 侧 client 的
`_send_input` 包一层 `threading.Lock`，在 `ProbeThread.start()` **之前**装好。
临界区只包住 send 本身 —— `call_utility` 的 `future.result()` 在 send 返回之后才阻塞，
把等待也圈进锁里没有理由。状态写进启动日志与 `meta.json` 的 `input_send_lock` 字段：
单进程口径报 `not-needed (in-process caliber has no transport socket)`，
MP 口径必须报 `installed`；**报 `unavailable` 就说明这条修法没生效**，MP 跑不能开。

**这条对后面三个批次是硬约束，不只是批次 1 的事**：§4.4 要求批次 3 跑 MP，
而 §3.3 选择单进程的三条理由之一正是"MP 下 `call_utility` 与 `step()` 共用 socket"。
锁装好之后，"谁在写 socket"这个问题在 MP 下也有解了 —— 但批次 2 仍然跑单进程，
理由不变（泵的可见性、探针无 socket 可争）。

**崩溃前采到的 MP 探针数据仍然有效，而且正是本门要问的**（崩的是引擎不是探针）：

```
probe_caliber = "multiprocess"
gr_probe 最大值（35 个采样点中 25 个有活会话）:
  session_count 1, in_flight 1, leases 1, dispatch_records 3
  by_status 出现过 RUNNING
gr_sessions = {source: "out_of_process", note: "sessions live in EngineCoreProc; see gr_probe"}
```

即：**跨进程那条读取路径是通的**，`gr_probe` 拿回的是真数字而不是一个空快照
（§2.3.2 最想确认的一件事）。崩溃之后 `meta.json` 没写成，所以重跑一次拿完整 30 分钟。

**`mp0-val30` 实测（2026-10-08 11:09:26 → 11:41，`meta.json`）**：

```
revision          = "cd0a28d"          env = {VLLM_ENABLE_V1_MULTIPROCESSING: "0",
                                              VLLM_GR_PREFILL_GRAPH_KV_BOUND: "4096",
                                              VLLM_GR_REVISION: "cd0a28d"}
probe_caliber     = {"in_process": 59}   probe_reads = 59   probe_errors = []
gr_probe_counters = {installed: true, regressed: false,
                     first: {gr_prefill_fastpath_hits: 1,     gr_prefill_declines: 0,
                             gr_retire_frames: 0, gr_retire_consumed: 0, gr_protocol_failures: 0},
                     last:  {gr_prefill_fastpath_hits: 32484, gr_prefill_declines: 0,
                             gr_retire_frames: 0, gr_retire_consumed: 0, gr_protocol_failures: 0}}
prefill_fastpath  = {hits: 33060, declines: 0, requests_observed: 33060, status: "installed"}
gr_probe_totals_max = {session_count: 1, dispatch_records: 3, in_flight: 2, leases: 2}
probe_lifecycle   = {statuses_seen: ["RUNNING"], transitions_seen: [],
                     retire_frames: 0, retire_consumed: 0}
```

逐条对 §2.3.2：`probe_caliber` 报 `in_process` ✓；`probe_errors` 为空 ✓；
`installed=true` 且 `gr_prefill_fastpath_hits` 1 → 32484（增长 32483）且 `regressed=false` ✓；
`session_ids` / `by_status` / `in_flight`(max 2) / `leases`(max 2) 均非零 ✓；
两侧 fastpath 计数器**都非零且都零 decline**（引擎侧 32484 / 客户端侧 33060）✓。
**退休帧仍然全程为 0、`statuses_seen` 只有 `RUNNING`** —— 与 §2.3.4 的 180 s 冒烟跑结论一致，
现在是在 33,060 个请求、32 分钟的规模上再次确认：正常完成路径**不产生退休帧**。

**跑中发现两件事，都不是本门"通过/不通过"能表达的，单独记。**

**(1) 看门狗在 MP 口径下漏掉 `EngineCoreProc`，§2.3.2 那条判据当时并不成立。**
`--pid-pattern run_soak.py` 用 `docker top` 逐行 `re.search` 匹配 cmdline；而 MP=1 下
vLLM 把引擎进程的 **进程名改写成了 `VLLM::EngineCore`**，它不匹配该 pattern：

```
PID      CMD
293420   python3 .../run_soak.py --output .../mp1-val30 ...   <- 匹配，driver
295353   /usr/bin/python3 -c from multiprocessing.resource_tracker import main;main(44)
295354   VLLM::EngineCore                                      <- 不匹配，2.9 GB
```

后果：`proc_count` 恒为 1，不发 `proc_set` 事件，`proc.csv` 的 RSS **只是 driver**
（1.88 GB），2.9 GB 的引擎进程完全不在曲线里 —— 而 §2.3.2 说这条 RSS 是**批次 3 内存论证的支点**。
`find_container_pids` 的 docstring 里"fork 下父子 cmdline 逐字相同"这个前提**在本配置下不成立**：
`VLLM_WORKER_MULTIPROC_METHOD` 管的是 vLLM worker（TP>1 才有），TP=1 时根本不生成 worker；
引擎是 `EngineCoreProc` 且改了名。MP=0 下只有一个进程，RSS 本来就完整，所以这个缺陷只在 MP 口径暴露。

**修法**：pattern 扩成 `run_soak\.py|VLLM::EngineCore`。实测重开后
`pid_found {'pid': 293420, 'source': 'container'}`（仍取最小 pid = driver，存活/重启判定不受影响）、
`proc_set {'pids': [293420, 295354], 'proc_count': 2}`，
`proc.csv` 的 RSS 变成 **4,817,212 KiB ≈ 4.82 GB = 1.88 + 2.91**，两块都在。
mp1 的第一份看门狗（未修正）保留在 `mp1-val30-wd/` 作为对照，修正版在 `mp1-val30-wd2/`。

**(2) `process_lost` 之后同一个数字 pid 被复用，曲线尾部在测另一个进程。**
mp0 的 driver（host pid 213050）在 11:41:27 退出（`proc.csv` 记 `alive=0`），
但从 11:41:46 起同一个 pid 号又"活"了，RSS 从 7 MB 一路长到 1.8 GB / 298 线程 ——
那是 mp1 启动期的另一个进程。所以 **mp0 的 RSS 曲线只有 11:41:22 之前那段有效**。
harness 自己的 docstring 早就点了这个失效模式（"RSS 曲线描述了另一个进程"），
这里是在真实运行里撞上了一次。修正版看门狗靠 `proc_set`/`pid_found` 事件把进程集合显式记下来，
读曲线时以事件为准，不以"pid 号还是那个号"为准。

回填时逐条对 §2.3.2，按**已订正**的判据（原写"RSS 采到 EngineCoreProc 而非 driver"是错的，
正确判据见 §2.3.2：RSS 是 driver 与 `EngineCoreProc` **两进程之和**，`proc_count`/`pids` 一并记录，
`proc_count` 2→1 是独立发现）：`probe_caliber` 两口径分别报 `in_process`/`multiprocess` 且
`probe_errors` 为空；`gr_probe_counters.installed` 为 `true` 且 `gr_prefill_fastpath_hits` 增长且
`regressed` 为 false；两口径各报一个落在**自己池子**里的 `free_blocks`（原写"两口径一致"，
已证伪 —— 见 §2.3.2 与下面的池子表）；watchdog `pid_found … source: "container"`；
多进程下 `session_ids` / `by_status` / `in_flight` / `leases` 非零。

**这道门验证的是哪一版探针，必须写清楚。** 快照 rev 是 **`cd0a28d`**
（`soak.env` 的 `SOAK_REV`，`source/` 目录已存在则复用），所以这道门**覆盖不到** `561ebbb`
新增的字段 —— `has_gr_cleanup_work` / `scheduler_has_requests` / `pause_state` /
`worker_resources_allocated` / `retirement_pending` 在这两次跑的 `gr_probe` 里**根本不出现**。
实测确认：`mp0-val30` 的 `gr_probe` 字段恰为 `cd0a28d` 那五个。
这些字段的归属是**批次 3 之前的标定跑**（§4.4 要求 MP 口径，且必须先于五小时），
在 `1f28138` 快照上跑；本门**不能**被引用为"新字段已双口径验证过"。
把它写在这里是为了避免下一版读者拿着 §2.3.3 的 `561ebbb` 行去 §2.3.5 找证据而找不到。

**`mp1b-val30` 实测（2026-10-08 12:20:58 → 12:53:03，`meta.json`，重跑）**：

```
revision          = "cd0a28d"          env = {VLLM_ENABLE_V1_MULTIPROCESSING: "1",
                                              VLLM_GR_PREFILL_GRAPH_KV_BOUND: "4096",
                                              VLLM_GR_REVISION: "cd0a28d"}
input_send_lock   = "installed"        <- 修法真的接上了（§2.3.5 崩溃那节）
probe_caliber     = {"multiprocess": 58}   probe_errors = []
gr_probe_counters = {installed: true, regressed: false,
                     first: {gr_prefill_fastpath_hits: 1,     gr_prefill_declines: 0,
                             gr_retire_frames: 0, gr_retire_consumed: 0, gr_protocol_failures: 0},
                     last:  {gr_prefill_fastpath_hits: 28001, gr_prefill_declines: 0,
                             gr_retire_frames: 0, gr_retire_consumed: 0, gr_protocol_failures: 0}}
prefill_fastpath  = {status: "unavailable_multiprocess: worker owns the model runner
                     (VLLM_ENABLE_V1_MULTIPROCESSING=1)", hits: null, declines: null}
gr_probe maxima   = {session_count: 1, in_flight: 1, leases: 1, dispatch_records: 3}
                    by_status 出现过 RUNNING（首条有活会话的样本 = {RUNNING: 1}）
                    字段集与 mp0 相同 —— 仍是 cd0a28d 那五个，没有 561ebbb 的新字段
free_blocks       = 9,751 / 9,820（min/max），首尾均 9,755
loop              = "14247 pairs in 1800s; probe samples=58"
```

逐条对 §2.3.2：`probe_caliber` 报 `multiprocess` ✓（与 `mp0-val30` 的 `in_process` 成对）；
`probe_errors` 为空 ✓；`installed=true`、`gr_prefill_fastpath_hits` 1 → 28001（增长 28000）、
`regressed=false` ✓；`session_ids` / `by_status` / `in_flight` / `leases` 均非零 ✓；
`input_send_lock: installed` ✓；看门狗 `pid_found … source: "container"` 且
`proc_set {pids: [440804, 442411], proc_count: 2}`，`proc.csv` 的 RSS 峰值 **4.61 GiB**
（`read_proc_set` 在 `len(pids)>1` 时把两进程**求和**，`pid` 列写的是 `pids[0]` = driver，
所以这一行是 driver + `EngineCoreProc` 的合计；段末引擎退出后回落到 1.79 GiB 是 driver 单跑，
不是"内存被还回来了"）✓。修正后的 pattern 生效，见 §2.3.5 发现 (1)。**崩溃未复现**：32 分钟、58 个探针采样点、
`14,247` 个请求对，引擎没有再抛 `ValueError`。

**一处口径不对称，必须写下来：`mp1b` 拿不到第二个 fastpath 来源。**
`prefill_fastpath` 在 MP 下报 `unavailable_multiprocess` —— 跑在 worker 里的 model runner 不归
driver 进程，客户端侧的计数器读不到。所以 §2.3.5 里"两侧计数器互相印证"（引擎侧 32484 /
客户端侧 33060）**只在单进程口径成立**；MP 口径下 `gr_probe_counters` 是**唯一**来源，
它一旦被接错就没有第二条证据能发现。批次 3、4、5 都跑 MP，这条要一直记着。

**`free_blocks` 的池子：先以为是口径差，实测是启动抖动。** 四跑的 `GPU KV cache size`：

| 跑 | 口径 | `Available KV cache memory` | `GPU KV cache size` | blocks（`block_size=16`） | warmup |
|---|---|---|---|---|---|
| `mp0-val30` | MP=0 | 16.88 GiB | 158,000 tokens | 9,875 | 0.23 s |
| `mp1-val30` | MP=1 | 16.79 GiB | 157,136 tokens | 9,821 | 1.24 s |
| `mp1b-val30` | MP=1 | 16.79 GiB | 157,136 tokens | 9,821 | — |
| `calib-k` | **MP=0** | 16.79 GiB | **157,136 tokens** | 9,821 | 1.21 s |

**第四行推翻了前三行给出的结论。** 前三行当时读成"MP=0 大、MP=1 小，差 54 块，两次 MP 跑逐位相同
所以是口径的确定性后果"。`calib_k.py` 的 MP=0 跑报的是 `157,136` —— 与 MP=1 逐位相同。
同一个口径给出两种池子，**53/54 块的差额与口径无关**。

没有哪一跑把差额归因清楚，但有一条线索：`Estimated CUDA graph memory` 四跑都是 `0.03 GiB`，
唯一可见的分野是 `Initial profiling/warmup run took`，0.23 s 那一跑池子最大，1.2 s 的三跑都小。
0.09 GiB 的差 ≈ 54 blocks × 1.75 MB/block，量级对得上——即**池子是 warmup 残留的测量结果，不是配置的函数**。

**这条订正比第一版更硬，因为它改变了判据怎么写，而不只是判据的阈值**：
`meta.json` 里**没有 `GPU KV cache size`**（只有 `block_size=16`，见 `mp0-val30/meta.json` 的键集），
池子只出现在每次跑的启动日志里。所以
- `free_blocks` 的"回落"只能跟**同一次跑自己的基线**比，跨口径、跨跑都不行；
- 换一次跑就等于换一个分母，写报告时要么同时给出该跑的池子，要么给"占用 ÷ 容量"。

54 块到底去哪了（CUDA graph 估值的抖动？allocator 碎片？）本门没有定位，
**不影响本门结论**：本门问的是探针读数真不真，不是内存去哪了。

---

## 3. 批次 2：取消 soak（离线）

### 3.1 必要性

**取消是推理系统的必备能力**（§0.1），而现有四次长跑一次都没取消过请求。这条路径上有两个具体的风险：

**风险一：释放被**设计成**延迟的，而延迟没有上限。** `abort` 里有三处"不会立即释放"的情形
（§3.2 分支表），三者都需要**有人继续泵引擎**。而 `abort` 内部的泵循环
（`gr_session_client.py:182-187`）**无超时无上限 —— 而且两个口径下是两段不同的代码**：

| 分支 | 行 | 条件 | 循环体 | 挂死形态 |
|---|---|---|---|---|
| 单进程 | `:182-184` | `hasattr(core, "engine_core")`（`core` 是 `InprocClient`） | `engine.step()`，**无 sleep** | 100% CPU 忙等 —— 至少是可见的 |
| 多进程 | `:185-187` | `core` 是 `SyncMPClient`（没有 `.engine_core`） | `core.call_utility("is_gr_session_retired", ...)` + `time.sleep(0.001)` | **几乎不耗 CPU 的静默挂死** |

只要退休证明或最后一个回执不来，
调用方**永久挂死**，而且引擎不会报错，`has_gr_cleanup_work` 还会让引擎一直"不空闲"，
外部看起来只是变慢。这是本批次最需要防的失败形态。

**风险二：取消路径不显式还 KV 租约，而且兜底比原先以为的窄得多。**
`abort_gr_session` / `finish_gr_session` / `_close_gr_session` / `release_gr_session`
**都不碰** `_kv_dispatch_leases`（`gr_async_scheduler.py:602-644`），归还只在
`update_gr_from_output` 里发生（§7.1）。先前把这处描述成"开头无条件 `pop` 兜底" —— **不准确**：
`update_gr_from_output` 起于 `:505`，`pop` 在 `:525`（函数第 21 行），而且被两道门挡住才执行 ——
`:512-513` 的 `metadata is None` 早返回，以及 `:515-518` 的 `owner_generation` / `dispatch_id`
双重校验。**所以不只是"回执不来就不还"，"回执来了但是脏的"同样不还。**
后者正是批次 2 场景 7（同一 `session_id` 复用 + 迟到回执）构造得出来的状态，
也是本批次判据真正该盯的方向。

**先说清不测什么**：`gr_async_scheduler.py` 里那两个 `abort_gr_session` / `release_gr_session`
**是全仓无调用点的死代码**：

```
$ git grep -n "abort_gr_session\|release_gr_session" 9c71c9d -- vllm_gr/
gr_async_scheduler.py:639   def abort_gr_session(...)      ← 定义
gr_async_scheduler.py:670   scheduler_cls.abort_gr_session = abort_gr_session   ← 安装
gr_session_client.py:233    llm_engine_cls.abort_gr_session = abort_gr_session_sync  ← 另一套
```

调度器上那两个被 `:670-671` 装到类上，但全仓无调用点（含动态派发，`getattr(..., "abort_gr_session")`
与字符串形式 grep 均空）。客户端要找的 `self.engine.abort_gr_session`（`self.engine = llm.llm_engine`）
命中的是 `gr_session_client.py:233` 装在 `LLMEngine` 上的另一套。而且客户端侧的 release 是空操作：

```python
# gr_session_client.py:190-192
def release_gr_session_sync(engine: Any, session_id: str) -> None:
    # EngineCore releases the scheduler state after all queued stages drain.
    return None
```

**所以不要把 `release_gr_session` 的 `RuntimeError("cannot release a GR session with in-flight stages")`
护栏当靶子** —— 它永远不会被执行。真正的释放全在 Core 内部由退休帧完成。

### 3.2 代码在哪

**真实的离线取消链路**：

```
client.abort(session)                        # client.py:180 → close → _close_session(190)
  → method = "abort_gr_session"              # 因为 not session.terminal
  → gr_session_client.py:177 abort_gr_session_sync
      engine.output_processor.abort_requests([sid], internal=True)
      core.abort_requests([sid])             # 原生中止
        → scheduler.finish_requests(sid, FINISHED_ABORTED)
        → 被 engine_core_patch.py:495-498 打补丁
        → _sync_gr_sessions_after_native_finish (:376-410)
             FINISHED_ABORTED 分支 → 置 ABORTED + _drain_resources    ← 真靶子在这
      while not core.engine_core.is_gr_session_retired(sid):          # :182-187
          engine.step()                      # 自己泵引擎，无超时无上限
```

**要测的是 native 中止 → `_sync_gr_sessions_after_native_finish` → 延迟 drain 这条链路。**

`abort_gr_session`（`gr_async_scheduler.py:639-648`）的逐分支走向：

| 分支 | 走向 |
|---|---|
| session 不存在 | 只走 `finish_requests`（对未知 id 是 no-op）；`is_gr_session_retired` 立刻 True。**幂等性靠"字典里没有"这一条**（`_release_owner:175` 已 pop） |
| 无 in-flight | 置 ABORTED + `terminal_result_pending=False` → `finish_requests` → 补丁拦截 → `_sync_gr_sessions_after_native_finish` → `_drain_resources` → 若 `not worker_resources_allocated` 则 `_release_owner` **立即摘除** |
| **有 in-flight** | abort 里那句被 `not session._in_flight_dispatches` 挡住；置 DRAINING 的是 `_sync_gr_sessions_after_native_finish` 里那次无条件调用。释放推迟到最后一个 dispatch 回执（`:506, 520-525`） |
| 重复 abort | 再走一遍 `finish_requests`（原生幂等）+ 条件 `_drain_resources`，不抛错、不重复扣资源 |

**不会立即释放的三种情形**（三者都需要有人继续 `engine.step()`）：
① `worker_resources_allocated=True` → 等退休帧（下次 schedule 时挂零 token 控制帧）；
② `_in_flight_dispatches` 非空 → 等最后一个回执；③ 终态待交付 → 等 `_finalize_ready_gr_session`。

### 3.3 怎么测

**驱动方式**（`engine.step()` 无锁 —— `gr_session_client.py` 与 `wait_final` 都会调它 ——
所以不能一个线程 wait、另一个线程 abort，"跑到一半取消"只能用**同一线程**先手动泵 K 步来近似）：

```python
client  = BeamSearchClient(llm)
request = client.prepare_request(call)
session = client.submit_once(request)
for _ in range(K):
    llm.llm_engine.step()
client.abort(session)
```

**口径选择：本批次跑单进程（`VLLM_ENABLE_V1_MULTIPROCESSING=0`）。** 三条理由，都是前面已经
落地的事实，不是偏好：

1. **被测代码在单进程口径下是 `:182-184` 那段忙等循环**（见 §3.1 的两分支表），
   失败形态是 100% CPU 的可见挂死，而不是 `sleep(0.001)` 的静默挂死 —— 对七小时的自动跑，
   可见性本身就是判据的一部分。
2. **看门狗必须能并发读探针。** MP 下 `call_utility` 与 `step()` 共用 `SyncMPClient`
   的 `_send_input`，ZMQ socket 不是线程安全的；再加一个看门狗线程去 `call_utility`
   就是三个写者。单进程下探针走对象图直读，没有 socket，只有"读的时候字典在变"这一类风险 ——
   已在 `1f28138` 用 `list()` 快照修掉。
3. **取消/排空逻辑本身与口径无关。** 两个分支跑的是同一个 `abort_gr_session`、
   同一套 `_sync_gr_sessions_after_native_finish` → `_drain_resources` → `attach_gr_retirement`
   链条，差别只在"谁来泵"。**MP 下"谁来泵"这个问题在离线场景里没有答案**：
   `:185-187` 的循环自己**不调 `engine.step()`**，靠 `EngineCoreProc` 自驱；
   单进程下循环体自己就是泵。要测"泵得动泵不动"，得先把泵握在自己手里。

**代价要说清楚**：这个口径下 `probe_caliber` 只会有 `in_process`，多进程口径的那半张探针表
在批次 2 里得不到验证。那半张表的归属是批次 1 的双口径门（§2.3.5）与批次 3（§4.4 要求 MP）。

**第一个动作是标定跑，不能跳过。** K 决定撞哪个在途窗口，而 K 的正确取值取决于每个 stage 占多少步。
标定跑要打印：每个 stage 的起始 / 结束步号、prefill 与 decode 各占多少步、`is_last_stage` 落在第几步。

**已实现为 `tools/soak/calib_k.py`，2026-10-08 在 `1f28138` 快照、MP=0 下跑通**（两跑取后者，
见下面的"为什么以第二跑为准"）。每步采一次 `_dispatch_records`
（`beam_search_v1.py:257` 的 `GRStageMetadata` 带 `stage_index` / `kind` /
`is_last_stage` / `decode_step`）与 `_in_flight_dispatches`，把每个 dispatch 的
「开窗 / 关窗步号」折成一张表，并直接给出若干 K 候选。
**它自带一个盲区声明**：`issue` 与 `receipt` 落在同一个 `step()` 里的 stage 从不进入任何
in-flight 集合，采样器看不见 —— 差值写进 `windows.json` 的 `stages_invisible_to_sampler`，
不留给读者自己发现"表比 stage 数少"。
标定跑取**单进程口径**：要读的是 session 对象，MP 下它在 EngineCoreProc 里够不着；
而"某个 stage 在第几步被 issue"由调度器决定，两口径同一套代码，所以这个测量可迁移。

**标定结果（`calib-k/windows.json`，`stages_invisible_to_sampler: 0`）：**

| dispatch | kind | stage | decode_step | is_last_stage | 开窗步 | 关窗步 | 回执步 | span |
|---|---|---|---|---|---|---|---|---|
| 0 | PREFILL | 0 | — | false | 1 | 1 | 2 | 1 |
| 1 | DECODE | 1 | 0 | false | 2 | 2 | 3 | 1 |
| 2 | DECODE | 2 | 1 | **true** | 3 | 3 | 4（推） | 1 |

三条几何结论，直接决定 §3.3 的七个场景怎么扫：

1. **每个 stage 恰好占一个 `step()`**，没有 stage 横跨两步。所以"驱动 K 步后 abort"里的 K
   与 dispatch 序号一一对应，不需要在窗口内部再定位。
2. **`K=1` 命中 prefill 窗口**（dispatch 0 在飞），**`K=2` 命中 decode 窗口**（dispatch 1 在飞），
   **`K=3` 命中末段 decode**（dispatch 2、`is_last_stage=true` 在飞）。这正是 §3.3 场景 1 / 2 / 3 要的三个靶子。
   三者**都是最坏情况**：abort 到达时确实有一个 dispatch 在飞、lease 非零。
3. **回执落在下一步**：dispatch 0 在第 1 步被 issue，第 2 步的 in-flight 集合变成 `[1]`——
   dispatch 0 已经走了。dispatch 1 同理（第 3 步集合是 `[2]`）。dispatch 2 的回执步因此是 4，
   这是从两个**实测**邻居推出来的，不是实测的；代价换来的是脚本能退出（见下）。

**几何与批次 1 的真实长跑对得上，不是标定脚本的假象**：`mp0-val30/meta.json` 报
`probe_lifecycle.dispatch_records_max: 3`、`in_flight_dispatches_max: 2`、`kv_dispatch_leases_max: 2`，
`gr_probe_totals_max` 同样是 `{dispatch_records: 3, in_flight: 2, leases: 2}`。
3 个 dispatch 与标定一致；"最多 2 个在飞"与上表每步只有一个 in-flight 一致
（5 s 采样间隔下的样本上界，不是上界本身）。

**`max_tokens` 被前端裁过：请求 5，引擎 3。** 数据集 `expected_output_len=5`，
`beam_params.max_tokens` 到引擎里是 3，所以 `total_stages=3` 而不是 5。
这个数值直接来自 `GRSessionState.total_stages`，是"1 prefill + 2 decode"的出处。

**没有标定就直接扫 K，等于在不知道靶子在哪的情况下开枪。** `K=0` 只测"未 dispatch 就 abort"的快速路径；
要撞「abort 时有在途 dispatch」必须让 K 落在上表三个窗口里。

**两个踩过的坑，写下来免得批次 5 再踩一次：**

- **阶段状态不在客户端那个 `BeamSearchSession` 上。** 客户端拿到的是一个纯句柄
  （`protocol.py:44`：`batch_id` / `session_id` / `handle` / `data_parallel_rank` /
  `terminal` / `failed` / `closed`），
  **没有任何引擎状态**；`total_stages` 是 `GRSessionState` 的属性（`beam_search_v1.py:233`），
  而 `GRSessionState` 挂在**调度器**上（`scheduler._gr_v1_sessions[session_id]`）。
  第一版脚本读句柄，报 `AttributeError: 'BeamSearchSession' object has no attribute 'total_stages'`。
  正确入口是 `engine_core.scheduler._gr_v1_sessions`，MP 下够不着（这正是标定跑取 MP=0 的第二个理由）。
  脚本现在**读不到就 `SystemExit` 拒跑**，而不是写一张空表。
- **两个状态枚举不能混。** `GRSessionStatus` 是 `WAITING/RUNNING/FINISHING/FINISHED/ABORTED/FAILED`
  ——**没有 `CLOSED`**；`GRResourceStatus` 才有 `CLOSED`。第一版退出条件写成
  `("CLOSED","FAILED")`，于是"跑完"这一支永远不触发，只能靠 `FAILED` 退出。
  `_stage_view` 现在两个都报，退出条件是 `status ∈ {FINISHED, ABORTED, FAILED} or resource_status == "CLOSED"`。

**为什么以第二跑为准（一个被证伪的"挂死"）：** 第一跑在 `step=3` 之后再调一次 `step()` 就
不返回（~110% CPU 单核空转、5 min+ 无输出），当时判为引擎挂死。第二跑同一段代码
在 `step=1` 之后停住约 1.5 min（`step=2` / `step=3` / K 候选表都没打印），
我按"挂死"去查，结果它**自己跑完了**——中间在 JIT 编译 Triton kernel
（日志里 `Triton kernel JIT compilation during inference` 逐条冒出来；
第一跑那 15 条在同一秒出完，第二跑慢慢出）。
所以"停住"里至少混着两件事：**JIT 编译慢**（第二跑，已证），以及第一跑那种长到 5 min+ 的停。
第一跑到底是真挂死还是极端慢，**本门没有定论**——它没留下栈，也没有第二次同位置的复现。
所以脚本的结论不是"引擎会挂"，而是**这条驱动方式不可靠**：从外面看都是"没有输出 + 一个核忙"，
分不开"慢"和"死"。脚本因此把这条路径**绕过去**而不判它：
循环改成"三个 dispatch 都 issue 完就停"，不再去驱动那个已经排空的引擎。
`--step-timeout` + `faulthandler.dump_traceback_later` 留着做取证，但它**在那两次停住里都没打印任何栈**
（daemon 线程版与 `faulthandler` 版都没出），说明 GIL 被某个 C 层循环持着、或者停的根本不在 Python 帧里。
批次 3 若要深挖，得改用
`PYTHONFAULTHANDLER=1` 起进程再 `kill -SIGABRT` 取栈（本机没有 gdb / py-spy / strace，已确认）。

**harness 要加的三样东西**：

| 改动 | 原因 | 具体做法 |
|---|---|---|
| abort 看门狗 | §3.1 风险一：泵循环无超时无上限，且引擎不报错 | `client.abort` 放子线程 + `join(timeout)`；超时后**主线程**采 `gr_probe_snapshot` 判断卡在哪一步。**前提是单进程口径** —— MP 下 `call_utility` 与 `step()` 写同一个 ZMQ socket，看门狗线程再插一脚就是并发写同一个 socket（见下面的口径选择）。单进程下探针是纯对象图直读，主线程读、abort 线程泵，两者不争 socket |
| 同线程驱动 | `engine.step()` 无锁 | K 步与 abort 必须在同一线程，见上面的代码；abort 线程起来之后主线程**不再**调 `step()`，只读探针 |
| 清理重试可观测 | `client.py:41-54` 的 `_run_sync_cleanup` 无限重试（退避 0.1→×2 封顶 5.0s，在调用线程），是静默挂死的另一种形态 | 在 `vllm_gr.entrypoints.beam_search_v1.client` 这个 logger 上挂 handler，逐条捕获重试日志与其异常。**版本订正（2026-10-08）**：锚点 `9c71c9d` **已经包含** `74c4a9e` 的 `except EngineDeadError: raise`（`client.py:49-50` 实测在位，`git merge-base --is-ancestor 74c4a9e 9c71c9d` 为真）。所以本批口径下**引擎死亡会立即抛出**，不再表现为无限重试；无限重试只对**其他**异常成立 |

**七个子场景 × 各 1 h 循环**：

| # | 场景 | 打的是哪段代码 | 怎么构造 |
|---|---|---|---|
| 1 | prefill 在途时取消 | `_in_flight_dispatches` 含第 0 个 dispatch | `--k 1`（标定：dispatch 0 在第 1 步在飞） |
| 2 | 中间 decode step 在途时取消 | 中间 dispatch | `--k 2`（标定：dispatch 1 在第 2 步在飞） |
| 3 | 末段 decode 在途时取消 | 接近 `is_last_stage`，退役与 abort 竞争 | `--k 3`（标定：dispatch 2、`is_last_stage=true` 在第 3 步在飞） |
| 4 | `submit_once` 返回之前取消 | `client.py:216-235` `_rollback_submission` 的 race | **不在本批范围** —— 离线同步路径上 `submit_once` 不返回就不会有 session 对象，需异步客户端 |
| 5 | 终态之后取消 | 应走 release 而非 abort（分流失效检测） | 正常跑完后调 `close`。**判据靠"哪个 transport 方法被调"而不是 `session.terminal`**：把 `release_gr_session` / `abort_gr_session` 在 `llm_engine` 实例上包一层计数，terminal 那次必须 `release>0 && abort==0`。断言 `session.terminal` 只校验了分支的**输入**，没校验分支走了哪条边 |
| 6 | 对同一 session 连续重复取消 | 幂等契约（`protocol.py:140-141`） | **不能走 `client.abort`**：`client.py:181-182` 在 `session.closed` 时直接 return，第二次起是空操作，测不到 transport 幂等。改为直接连调 `llm_engine.abort_gr_session(sid)` N 次，每次单独计耗时与异常 |
| 7 | 取消 × 同 session_id 复用 | `owner_generation` + `_dispatch_records` 对象同一性 | **不能靠复用 call 对象**：`prepare_request` 每次调 `_generate_request_id()` 生成 `offline-gr-beam-v1-<uuid4>`（`input_processor.py:37-42`），第二个 session 会静默拿到新 id，场景退化成"什么都没测"。必须临时把 `input_processor._generate_request_id` 钉成常量，且**只钉一轮**（`try/finally` 还原），否则后面所有场景都继承了固定 id |

（场景 4 的注释 `client.py:220-221`「Fence acceptance before aborting」确实标示了一个被识别的竞态，
但离线同步路径不可达，标为"不在本批范围"。）

**已实现为 `tools/soak/cancel_soak.py`**（一次一个 `--scenario`，各跑自己的 `--duration-seconds`）：
场景 1/2/3 的 K 从 `calib_k.py` 的 `windows.json` 读（`--windows`），并**把 K 的出处写进每条记录**
（`k_provenance`）—— K=0 与 K=6 都能跑出"看起来通过"的结果，但一个测的是"未 dispatch 就 abort"，
另一个撞的是真实在途窗口。没有 `windows.json` 时退回 `--k` 并在记录里标注 `cli-fallback`。
输出 `iterations.jsonl`（逐轮）、`cleanup-retries.jsonl`（重试风暴）、`summary.json`（判据表）。

**执行方式**（`run_cancel_soak.sh`，一次一个场景、一个进程）：

```bash
ssh gr 'docker exec -i vllm-gr-benchmark-gpu1 bash -s' <<'SH'
setsid nohup env SCENARIOS=1,2,3,5,6,7 DURATION_SECONDS=3600 \
  /opt/vllm-gr/results/soak/tooling/run_cancel_soak.sh \
  > /opt/vllm-gr/results/soak/cancel-soak.log 2>&1 < /dev/null &
SH
```

场景 4 被排除，所以共 6 个。`SCENARIOS` / `DURATION_SECONDS` / `SUFFIX` 都可用环境变量覆盖，
短时长 + `SUFFIX=-smoke` 就是冒烟跑（**跑之前必须先冒烟**，见下）。

**时长口径是两档的，不是统一 1 h（`--duration-seconds` 才是总闸门，轮次只是它的产物）。**
原方案写"6 × 1 h = 6 h"，实跑改成：

- **场景 1/2/3 各 1 h** —— 它们打的是三个**在途窗口**（prefill / 中间 decode / 末段），
  判据是逐轮断言，而每轮就是一次完整的"提交 + 取消"，所以证据强度直接等于轮次：
  1 h 给出的量级是 11 万~27 万次取消零残留，统计上早就饱和。
- **场景 5/6/7 各 10 min** —— 它们打的是**终端分支 / 幂等契约 / id 复用**，不是长时稳态，
  120 s 的冒烟已经全过；10 min 是冒烟的 5 倍量，足够。
  再往后加时间是在重复已验证路径，边际价值接近零。

改口径的现场动作：批次 2 跑到场景 3 时 `kill` 掉驱动 shell（`run_cancel_soak.sh`）让它不再
按 3600 s 起场景 5 —— 正在跑的 `python3 cancel_soak.py` 是驱动 shell 的子进程，父 shell 被
杀后它被 reparent 到 PID 1 继续跑完（fd 1/2 是**普通文件**重定向到 `cancel-soak.log`，不是管道，
所以父进程消失不会给它 SIGPIPE），场景 3 完整打出 verdict；随后用
`SCENARIOS=5,6,7 DURATION_SECONDS=600` 重新起一个驱动接在同一份日志尾部。

```bash
ssh gr 'docker exec -d vllm-gr-benchmark-gpu1 bash -lc "cd /opt/vllm-gr/results/soak/tooling \
  && SCENARIOS=5,6,7 DURATION_SECONDS=600 ./run_cancel_soak.sh \
  >> /opt/vllm-gr/results/soak/cancel-soak.log 2>&1"'
```

一个可复用的观察：**单场景的墙钟里约 2.5 min 是引擎启动**（Triton JIT + `torch.compile` 缓存命中），
所以时长砍得越狠，启动开销占比越高 —— 600 s 的跑里启动占约 20%。再往下砍到 300 s 就不划算了。

**冒烟跑逮到的两个 harness bug（都已修，记下来因为两个都是"静默"的）：**

1. **`session.total_stages` 抛 `AttributeError`**（`_run_iteration` 与场景 5 各一处）。
   和 §3.3 上面那条"两个踩过的坑"第一坑是同一个：`BeamSearchSession` 是纯句柄，没有这个属性。
   修法是在 `Sampler` 上加 `total_stages(session_id)`，从 `core.scheduler._gr_v1_sessions` 读
   `GRSessionState.total_stages`（MP=0 才够得着 —— 这是批次 2 取单进程口径的第三个理由）。
   后果：场景 1 在**第一次迭代**就崩，`free_blocks baseline` 已打印、`iterations.jsonl` 是空的。
2. **`resolve_k` 把 span-1 窗口全部拒掉，三个场景静默退化成 K=0。**
   它原来的判断是"`close_step == open_step` ⇒ 没有 K 落在窗口内 ⇒ 退回 `--k`"，
   而标定跑实测**每个 stage 恰好占一个 `step()`**，所以**所有**窗口都是 span 1 ——
   于是场景 1/2/3 全部拿到 `K=0`，也就是"未 dispatch 就 abort"，测的是快速路径而不是在途窗口，
   而 `k_provenance` 里明明写着 `cli-fallback`，不看就会当成通过。修法：删掉这两个 span-1 拒绝
   （`mid()` 对 span 1 本来就算得对），并把场景 2 的取窗从 `decode[len//2]` 改成 `decode[0]`
   —— 两个 decode 窗口时 `len//2` 取到的是**最后一个**，与场景 3 撞靶。
   修好后离线单测：场景 1/2/3 → K=1/2/3，分别命中 stage 0/1/2，与 §3.3 场景表逐字一致。

**冒烟跑结果（`SCENARIOS=1,5,6,7 DURATION_SECONDS=120`，全过）：** 场景 1 `5581` 轮
（`free=9820` 恒定、`leases=0`、`abort_s≈0.0053`、`reset_prefix_cache()` 558 次全 True）、
场景 5 `921` 轮 **且 `release_gr_session=921 / abort_gr_session=0`**（终态确实走释放边而非取消边，
这是那条"断言 `session.terminal` 只校验了分支输入"的修正所在）、场景 6 `48155` 轮、场景 7 `69030` 轮，
三项硬判据 + 软判据在四跑里全部 PASS。**场景 5/6/7 的 `K=0` 是设计如此**，不是漏配：
`resolve_k` 只给 1/2/3 映射窗口，5/6/7 打的是分支选择、幂等契约、id 复用，
与"abort 时是否有 dispatch 在飞"无关，`k_provenance` 里如实写着 `cli-fallback`。
场景 2/3 未单独冒烟 —— 它们与场景 1 共用全部代码路径，只差 `--k`，而 K 的来源已离线单测过。
**批次 2 实跑结果（`1f28138`，MP=0，全量见 `cancel-soak.log`）：**

| 场景 | 取消点 | K（出处） | 轮次 | 判据 |
|---|---|---|---|---|
| 1 | prefill 段 | 1（`windows.json`） | 266,991 | 七项全 PASS |
| 2 | 中间 decode 段 | 2（`windows.json`） | 157,731 | 七项全 PASS |
| 3 | 末段（`is_last_stage`） | 3（`windows.json`） | 112,386 | 七项全 PASS |
| 5 | 终端 close 走释放边 | 0（`cli-fallback`，设计如此） | 15,986 | 七项 + 第 8 项全 PASS |
| 6 | 重复取消（幂等） | 0（`cli-fallback`） | 245,434 | 七项全 PASS |
| 7 | 同 `session_id` 复用 | 0（`cli-fallback`） | 344,150 | 七项全 PASS |

**六场景合计 1,142,678 轮、114,266 次 `reset_prefix_cache()` 检查、0 次 False、0 条 FAIL、0 个 Traceback，
六个进程全部 `exit=0`。** 场景 5 的第 8 项是它独有的：
`transport calls: {'submit_gr_session': 15986, 'release_gr_session': 15986}` —— 终态确实走**释放边**，
`abort_gr_session` 一次都没调（这条是"只断言 `session.terminal` 等于只校验了分支输入"的修正所在）。
场景 6/7 的速率（409 / 573 轮每秒）远高于场景 1（74 轮每秒），因为它们打的是分支选择与幂等契约，
不进完整的 3-stage beam 计算。

磁盘实测：`cancel-soak.log` 12 MB，六个输出目录合计 **1.05 GB**（225/134/96/17/297/304 MB）。

1/2/3 的关键数字：`by_status={}`（无 ABORTED 残留）、`leases` / `in_flight` / `dispatch_records` /
`worker_resources_allocated` / `retirement_pending` **全 0**、`reset_prefix_cache()` 合计
`26,699 + 15,773 + 11,238` 次检查**零 False**、`free_blocks` 逐位回到 **9820** 基线（三跑基线相同）、
无永久挂死、无 `Asynchronous scheduling lost reserved KV capacity`、0 次探针读取错误。

**原文"1 h 约百万轮量级 / `iterations.jsonl` 约 2 GB/场景"是错的**，实跑订正：速率
**74 / 44 / 29 轮每秒**（K 越大每轮步数越多），1 h 对应 **27 万 / 16 万 / 11 万**轮，
`iterations.jsonl` 约 **240 / 140 / 100 MB**，三场景合计约 **0.5 GB** —— 比原估计小一个量级。
（这条不影响判据，只影响磁盘预算：原以为 6 场景要 12 GB，实际约 1 GB。）

判据分**硬/软**两级，因为二者对"后面还要不要跑"的含义完全不同：

- **硬**（任一不过即 `exit 1`）：ABORTED 残留、`leases`/`in_flight`/`dispatch_records`/`worker_resources_allocated`/`retirement_pending` 未归零、`reset_prefix_cache()` 返回过 False、出现 `Asynchronous scheduling lost reserved KV capacity`（`gr_async_scheduler.py:137-148`）、永久挂死。这些意味着引擎已经坏了，后续小时数测的都不是干净状态。
- **软**（报告 FAIL，`exit 0`）：`free_blocks` 与基线非逐位相等、探针读取出现错误。差几个 block 值得写进报告，但不值得废掉这个场景剩下的证据。

**「看起来通过」的两种假象，harness 都堵掉了**：一是漏了一轮（`reset_prefix_cache` 的调用时机必须
在会话完全退役之后，`leases`/`in_flight` 非零时**跳过并记账**而不是照调 —— 否则 §7.3 第 2 点说的
系统性误报会如期而至）；二是根本没跑（`probe_errors` 与 `cleanup_retries` 都写进 summary，
`iterations` 为 0 时整张判据表没有意义）。

**判据**：

- `_gr_v1_sessions` 无 ABORTED 残留（`is_gr_session_retired` 恒 True）
- `_kv_dispatch_leases` 归零；`_in_flight_dispatches` 归零
- **`llm.reset_prefix_cache()` 保持返回 True**（§7.3，最强判据）
- 取消 N 次后 `block_pool.get_num_free_blocks()` 回到基线
- 客户端返回耗时不异常放大（`_run_sync_cleanup` 没在空转）
- **全程不出现 `RuntimeError("Asynchronous scheduling lost reserved KV capacity after Beam generation")`**
  （`gr_async_scheduler.py:146-148`）—— 这是 lease 泄漏的硬症状
- 不出现永久挂死（看门狗未触发）

---

## 4. 批次 3：pause soak（多进程）

### 4.1 必要性

**这是四条路径里唯一一条 GR 主动改了引擎行为的。** GR 对 pause 管道**零改动** ——
`git grep "pause_scheduler\|set_pause_state\|has_work\|_idle_state_callbacks"` 在 `vllm_gr/` 下**全空**。
唯一的接口是把 GR 的收尾工作量接进调度器的 `has_requests`：

```python
# vllm_gr/v1/engine/engine_core_patch.py:449-453
@wraps(original_has_requests)
def has_requests(self):
    return original_has_requests(self) or has_gr_cleanup_work(self)
```

```python
# vllm_gr/v1/engine/gr_async_scheduler.py:64-73
def has_gr_cleanup_work(scheduler: Any) -> bool:
    """Keep terminal delivery and Worker retirement live under native pause."""
    return any(
        session.terminal_result_pending
        or session.resource_status in (GRResourceStatus.DRAINING, GRResourceStatus.RETIRING)
        for session in getattr(scheduler, "_gr_v1_sessions", {}).values()
    )
```

**为什么必须这么做**：`scheduler.py:1872-1879` 的 `get_num_unfinished_requests()` 在 PAUSED_ALL 下
**返回 0**。于是「只保留了 GR 会话、没有可运行请求」的引擎会被判定为**空闲** ——
但那些会话还有收尾要跑（终态投递 + worker release proof + KV 释放）。
**GR 必须把它们算成 workload，否则 pause 永不完成，引擎永久挂起。**
这就是本批次的核心风险，也解释了为什么"pause 调用能不能返回"是最主要的判据
（注意它在客户端的可观测形态是**阻塞调用不返回**，不是 Future 未完成 —— 见 §4.3）。

**反向的一半（暂停时把已完成的 id 从 `finished_req_ids` 摘掉）不是 #427 新增的。**
它是 #403（`338fd6f`）引入的，`9c71c9d` 上逐字未变（见 §1.4.3 的订正），行号是
`engine_core_patch.py:710-733`（先前写 `:719-727` 是按工作区数的，不对）。它在这里仍然重要，
因为它是 pause 能收敛的前提：

```python
# A fused release retires the Worker's cached request in the consuming
# frame, so no retirement control frame will carry this ID to the Worker.
# Leaving it pending would keep has_requests() true forever: while
# PAUSED_ALL, schedule() early-returns before it can consume the set, so
# the engine would never look idle and the pause Future would never
# complete.
pending_finished_ids = getattr(scheduler, "finished_req_ids", None)
if pending_finished_ids is not None:
    pending_finished_ids.discard(request_id)
```

**这段注释本身就是"暂停与 GR 收尾耦合"的直接证据。**

**GR 接入 pause 管道的全部，就是上面这两处** —— `has_requests` 的 `@wraps` 补丁
（`engine_core_patch.py:449-453`）与这段 `finished_req_ids.discard`。
`#427` 与 `#389` 都没有改 pause 本身（§4.1 开头的 `git grep` 全空）。所以批次 3 测的不是
"#427 改了 pause"，而是"GR 这套收尾工作在最老的 pause 管道上第一次被长跑"。

**设计契约**见 `9c71c9d:docs/beam_stage_pr2.md:73-80`（该文件在两版之间未变）：

> `keep` drains already submitted batches and preserves active and waiting GR sessions with their state and KV;
> `wait` finishes native running requests while waiting requests remain queued. ...
> **Pending terminal delivery, draining resources and outstanding Worker release proofs still count as
> cleanup work under either pause mode. Retained sessions alone do not prevent pause completion.**

**现有测试都没覆盖这条路径**：

- `tests/test_gr_pause_resume.py`（`a94830c8` 已存在）用 `object.__new__(EngineCoreProc)` **手工装配**、
  假 Executor、`Deferred` Future，**无 GPU、无 LLM、无 LLMEngine**。它证明的是"in-process 可触发 pause"，
  **不证明离线 `GRLLM` 可达**。它唯一一次"实测"是 `:224-231`：`assert paused.done() and paused.result() is None`。
- **ST 测试只在在线分支 pause**：`test_onerec_v1_scheduler_st.py:403`
  `await engine.pause_generation(mode="keep", clear_cache=False)`，engine 是 `AsyncLLM`。
  `online=False` 的离线分支只用 `GRLLM.beam_search_v1`，**从不 pause**（`:298-320`）。
- `git grep -rn "pause" 74c4a9e -- examples/ tools/ benchmarks/` 只命中 GR 文档与 GC 注释。

→ **这条路径在离线侧、在 in-process 配置下，从来没有被测过。**

### 4.2 代码在哪

`PauseState` 与调度策略（`vllm/v1/core/sched/interface.py:22-33`，部署口径；
`UNPAUSED=0` @`:31`，`SchedulerInterface` @`:36`）：

```python
class PauseState(enum.IntEnum):
    UNPAUSED = 0
    PAUSED_NEW = 1   # 不调度新请求，running 继续
    PAUSED_ALL = 2   # 不调度任何请求
```

`PauseMode = Literal["abort", "wait", "keep"]`（`vllm/v1/engine/__init__.py:26`）。

`EngineCore.pause_scheduler(mode, clear_cache)`（`core.py:663-692`）只改调度策略，**不拦截 `add_request`**
（新请求照样进 waiting，只是不被调度）。`mode="wait"` 在 inproc-engine 模式下被显式拒绝（`core.py:681-682`，
即 `raise ValueError("'wait' mode can't be used in inproc-engine mode")`）。

**暂停完成的判定是"引擎到达 idle"**：`EngineCoreProc.pause_scheduler`（`core.py:1571-1612`）
返回一个 `Future`（`_pause_complete()` 在 `:1613` 为真时直接返回 `None`，不注册回调 ——
引擎本来空闲就是这种情况），否则把它挂到 `_idle_state_callbacks`（字段声明 `:223`，
`self._idle_state_callbacks.append(...)` 在 `:1609-1610`），由 `_notify_idle_state_callbacks`（`:1253`）驱动。
`has_work() = engines_running or scheduler.has_requests() or bool(batch_queue)`（`core.py:1181-1187`）。
**GR 就是从 `scheduler.has_requests()` 这一个人口切进去的**（上面的 `@wraps` 补丁）。

### 4.3 怎么测

**离线可达性有三条路径，选第二条。** 断点在 `EngineCoreClient` 抽象基类：
`EngineCoreClient`（`core_client.py:70`）之下，`InprocClient`（`:275-368`）、
`SyncMPClient`（`:748-918`）**都没有 pause**，只有在线用的 `AsyncMPClient`（`:919-1168`）有
`call_utility_async`（`:1070-1076`）。`GRLLM` / `LLM` 也都没有
（`LLM` 只到 `sleep` / `wake_up`，`llm.py:806/831`）。

| 路径 | 评价 |
|---|---|
| `llm.sleep(level=0, mode="keep")` | 公开，且 `level=0` 时 `clear_prefix_cache = (level>=1) = False`，**恰好等于** `pause_scheduler(mode="keep", clear_cache=False)`（`EngineCore.sleep` `core.py:702`，`clear_prefix_cache` `:716`）。但 SyncMPClient 下是阻塞黑盒，且 `LLM.sleep` 自带一层 bookkeeping（`llm_engine.py:357-364` 的 `record_sleep_state`），多一个不易归因的变量。**留作交叉验证，不作主驱动** |
| **`llm.llm_engine.engine_core.call_utility("pause_scheduler", "keep", False)`** | **推荐**。UTILITY 派发是泛化的：服务端 `getattr(self, method_name)`（`core.py:1308-1321`，`getattr` 在 `:1315`），客户端 `SyncMPClient.call_utility`（`core_client.py:844-850`）。**GR 自己的前端已经在用同一范式**（`gr_session_client.py:186` 就是这么调 `is_gr_session_retired` 的） |
| `engine_core.engine_core.pause_scheduler(...)` | InprocClient 专用；恒返回 None，无 idle 回调，`mode="wait"` 直接 raise |

**⚠ `call_utility` 是阻塞调用，拿不到 Future —— 原方案的驱动代码和判据都建立在这个错误前提上。**

`SyncMPClient.call_utility(method, *args)`（`core_client.py:844-850`）把
`(client_index, call_id, method, args)` 发到 UTILITY 通道后 `return future.result()`，**同步等到回包**；
而服务端在返回值是 `Future` 时会 `add_done_callback` **延后回包**（`core.py:1351-1366`
的 `_invoke_utility_method`，`isinstance(result, Future)` 在 `:1356`）。两件事合起来的语义是：

> `call_utility("pause_scheduler", ...)` **阻塞到 pause 真正完成**，返回的是结果值 `None`，
> **不是那个 Future**。

所以原方案那句「`fut = core.call_utility(...)`，等 fut 完成」写不出来；上表里
「`llm.sleep` 拿不到 Future」这条理由也不成立 —— **两条路都拿不到**，它不是选择依据。

**正确的驱动方式 —— 发出去，然后只轮询，不要泵引擎。**

**不要用子线程包 `call_utility`。** 这个方案初稿是 `threading.Thread(target=_pause)` +
`join(timeout)`，看着对，实际有洞：`SyncMPClient.call_utility` 是**先 `_send_input` 再
`future.result()`**，而 `step()` 也走同一个 ZMQ `_send_input` —— **ZMQ socket 不是线程安全的**，
两个线程同时写就是未定义行为。正确做法是**只把"发"和"等"拆开**：

```python
from concurrent.futures import Future
import time, uuid
from vllm.v1.engine import EngineCoreRequestType

core = llm.llm_engine.engine_core
# 这两行就是 SyncMPClient.call_utility 的前半段，只是不在这里等回包。
call_id = uuid.uuid1().int >> 64
pause_future: Future = Future()
core.utility_results[call_id] = pause_future
core._send_input(EngineCoreRequestType.UTILITY,
                 (0, call_id, "pause_scheduler", (pause_mode, False)))

# 只轮询。引擎在 EngineCoreProc 里自己泵自己 —— 见下面那条实测。
deadline = time.monotonic() + PAUSE_TIMEOUT_S
while not pause_future.done():
    sample = probe_snapshot(llm)          # 采样本身也要带超时，见本节的看门狗一段
    timeline.append(sample)
    if time.monotonic() > deadline:
        # 超时就是被测的失败形态（§4.1 的"GR 收尾不算 workload 就永不空闲"）。
        # 不要在这里调 resume_scheduler 去"救"它 —— 那会把要测的东西掩盖掉。
        raise TimeoutError(f"pause_scheduler did not return in {PAUSE_TIMEOUT_S}s")
assert pause_future.result() is None   # 客户端看到的结果值就是 None，不是 Future
core.call_utility("resume_scheduler")
```

**原方案的"`step()` 不能省"整段作废（本版订正）。** 它建立在"客户端要帮忙泵引擎"这个假设上，
而 MP 口径下 **driver 根本泵不动**：`llm.llm_engine.step()` 就是输出队列上的 `get_output()`，
引擎无事可交时**永久阻塞**。实测 driver 8 s 内 0 jiffies，同时 `EngineCoreProc` 烧掉
1 core-second —— 引擎一直在跑，只是 driver 参与不进去。
原方案担心的真空形态（"停手不泵 → `has_work()` 因为引擎本来没跑而为假 → pause 立刻返回"）
**在 MP 下不成立**：泵与不泵，引擎都自己跑。判别真空返回只能靠**探针**（§4.4 的非空条件），
不能靠"我有没有帮忙"。

上表的判据因此成立：客户端**看到**的是一次阻塞调用、结果是 `None`（服务端对 `Future` 返回值
`add_done_callback` 延后回包，见上），而"阻塞了多久"是本地测的，不需要 Future。

**回包快慢的含义按 mode 不同，别混用（本版新增）**：实测 `keep` 回包 0.054 s（此刻
`in_flight=1`、会话仍在注册表）、`wait` 回包 2.66 s（drain 完才回）。
`keep` 的快回包只说明**输出队列排空 + `scheduler.requests` 已空**，不是"引擎空闲"。

**暂停前必须断言引擎有 work，否则整个场景是真空的。**
`EngineCoreProc.pause_scheduler` 在 `_pause_complete()` 为真时**直接 `return None`，根本不注册
idle 回调**（`core.py:1571-1612` + `_notify_idle_state_callbacks` `:1253`）。引擎空闲时调 pause，「瞬间返回」是必然的，
`has_requests` 补丁一次都没被走到，判据全绿而什么都没测。所以每次 pause 之前：

1. 用批次 1 的 `gr_probe_snapshot` 确认目标状态成立（场景 1 要求至少一个会话处于
   `DRAINING` / `RETIRING`，即 `has_gr_cleanup_work` 为真）；
2. 记录 pause 调用**进入**与**返回**的时间戳，以及这期间 `_gr_v1_sessions` 的状态演化 ——
   但这期间**期望看到什么，取决于 mode**（§4.4 的实测表）：`keep` 下期望的是**冻结**
   （会话还在册、`dispatch_records` 不再增长），收尾要等 resume；`wait` / `abort` 下期望的才是
   「调用前有收尾工作、调用后收尾推进到归零」。把 `keep` 的冻结当成失败是本版修掉的一个误判。

**看门狗采探针本身也可能挂住，要做成"报告"而不是"等待"。** 超时后由另一个线程调
`gr_probe_snapshot`，这在 MP 下**同样是一次阻塞的 `call_utility`**：引擎卡住不回包时它也会卡住。
所以看门狗必须是「带超时采样，拿不到就记 `probe_unavailable`」，不能是「等采样成功才收尾」。

**结论：需要绕过公开接口直接操作 EngineCore，但不必在线。** 不必在线的原因：`pause_scheduler`
是**进程内同步方法**，完成条件就是本进程的 `has_work()` + idle 回调，与前端是不是 HTTP 无关。
唯一"在线独占"的是 `AsyncMPClient.pause_scheduler_async` 这个**封装**，不是能力。

**但必须开多进程。** `has_work()` / `_idle_state_callbacks` / `pause_scheduler` 返回 `Future` /
`_process_input_queue` —— **这一整套只存在于 `EngineCoreProc`**。InprocClient 的 step 路径是
`engine_core.step_fn()` + `post_step()`（`core_client.py:289-290`，在 `InprocClient` 的 `get_output` 里），**从不进入 `_process_input_queue`**，
所以 idle 回调链在 InprocClient 下根本不会被驱动。

→ **`VLLM_ENABLE_V1_MULTIPROCESSING=1` 是硬性前提**，也正是批次 1 必须排在前面的原因（§2.1）。

### 4.4 标定跑（必须排在五小时之前）

批次 2 明确要求先标定 K（§3.3），批次 3 之前同样必须有一次短验证，理由是一个本方案没有论证过的问题：

**PAUSED_ALL 下 GR 的收尾能不能推进？** `Scheduler.schedule()` 在 PAUSED_ALL 时把 `token_budget = 0`
（`scheduler.py:349-351`），WAITING 分支还被 `self._pause_state == PauseState.UNPAUSED` 挡住（`:545`）。
GR 的退休帧是**零 token 控制帧**（`attach_gr_retirement` 里有硬断言），它能否在 budget=0 下真正被
worker 消费、产出 release proof、让 `consume_gr_retirement` 把 `worker_resources_allocated` 置回
`False` —— **这直接决定 pause 会不会收敛**：

- 若**能**推进：批次 3 判据成立，五小时有意义；
- 若**不能**：`has_gr_cleanup_work` 恒 True → `has_requests()` 恒 True → `has_work()` 恒 True →
  idle 回调永不触发 → pause 永不返回。**场景 1 每次迭代都超时挂死**，五小时产出全是假故障，
  而且这些假故障看起来与真 bug 一模一样。

**2026-10-08 实测（rev `1f28138`，MP=1，`calib_pause.py`，三种 mode 共五跑）：问题比原方案设想的更细，
而且答案分两半。**

`pause_scheduler` 接受三个 mode（`core.py:663`，`PauseMode`）。**先纠正一个被自己第二轮实测推翻的说法：
`keep` 不是"冻结一切"** —— PAUSED_ALL 让 `schedule()` **不再发出下一个 dispatch**，
但它既不中止、也不拖住在飞的活。所以 `keep` 下会话的命运取决于 **pause 落在 stage 边界的哪一侧**，
而这是个**竞态**：

| `--pause-mode` | `pause_state` | 落在边界哪侧 | 实测 |
|---|---|---|---|
| `keep` | `PAUSED_ALL` | pause **抢在**下一个 dispatch 之前（`records` 2/3） | 第 3 个 stage **从未发出**；会话 `RUNNING`、`in_flight=0`、注册表里**冻满 10 s 保持窗口**；pause 回包 **0.054 s**。resume 后：`by_status={}`、注册表空、`records=0` —— **resume 把它跑完并退休了** |
| `keep` | `PAUSED_ALL` | pause **落在**最后一个 dispatch 之后（`records` 3/3） | 剩余在飞的活照常跑完，会话**在 PAUSED_ALL 下就退休了**，耗时 2.65 s |
| `wait` | `PAUSED_NEW` | — | 允许 `step()`，在途会话排空并退休。**两跑都退休**，回包分别 **2.66 s / 0.03 s** |
| `abort` | `PAUSED_NEW` | 先 `finish_requests(NULL, FINISHED_ABORTED)` | pause 落在 **`DRAINING` + `cleanup=True` + `has_requests=True`** 的状态上，回包 **2.67 s**，会话离开注册表 |

**对 §4.1 那个致命问句的回答，分两半：**

- **「GR 收尾算不算 workload、会不会让 pause 永不返回」—— 不会，已实测。** `abort` 那一跑正是
  §4.4 原先担心却没有构造出来的状态（`has_gr_cleanup_work=True`、`retirement_pending` 由收尾驱动、
  `has_requests=True`），pause **2.67 s 正常返回**，会话干净退休。`engine_core_patch.py:738` 描述的那条
  `finished_req_ids` 残留路径**没有发作**。五跑六次 pause 全部返回，无一挂死。
- **「PAUSED_ALL 下收尾能不能推进」—— 问题本身要改写：`keep` 根本不会产生收尾工作。**
  它不 abort，所以不会把请求推进 `DRAINING`/`RETIRING`；它只是让下一个 dispatch 不发出。
  真正会"冻结在收尾中"的组合是 **`keep` 的暂停窗口内再发一次取消**（`abort_gr_session`），
  那才是 §4.5 场景 2，也是**那次标定唯一没有覆盖的形态**。
  **该形态已在 §4.4.1 补齐并实测：冻结确实成立（`worker_resources_allocated=1`、`in_flight=1`），
  但在该窗口内取消，会话在暂停生效期间就注销了，耗时 2.2~18.9 ms —— 本段末句预期的那条
  "链在暂停窗口内无法推进、取消永久阻塞"被证伪，以 §4.4.1 为准。**

**场景 1 的判据据此定稿**（原方案列的备选分支"接受 pause 期间收尾冻结、resume 之后才推进"
现在有了实测支撑，不再是应急方案）：

- `keep`：**不得发出新的 dispatch**；会话在整轮结束时必须已经离开注册表 —— 落在边界前侧就是
  **resume 之后离开**（实测如此），落在后侧就是**暂停期间就离开**。**"仍留在注册表"不算失败，是竞态的正常一侧。**
- `wait` / `abort`：暂停窗口内排空并离开注册表。
- 在线 `/pause` 的默认 mode 是 **`abort`**，不是 `keep`（`entrypoints/serve/rlhf/api_router.py:30-31`：
  `mode: Annotated[PauseMode, Query()] = "abort"`）。

**另有三条必须记进报告的实测：**

1. **pause 回包耗时在任何 mode 下都不是信号。** 同一个 `keep` 报过 0.054 s 与 2.65 s，
   同一个 `wait` 报过 0.03 s 与 2.66 s —— 而两边的会话都还在注册表里、`in_flight` 都还非零。
   它跟踪的是**瞬间的输出队列状态**，与 stage 边界在毫秒尺度上赛跑，所以"回得快"和"回得慢"
   单独看都没有含义。原方案把回包耗时当主要判据，本版改为看**会话是否离开注册表**。
   （`keep` 的 0.054 s 尤其不能读成"引擎空闲"：那一刻探针报 `in_flight=1`、会话在线，
   而 `scheduler.requests` 已经是空的了 —— **pause 完成与否看的是 `requests`/输出队列，不是 `_gr_v1_sessions`**。）
2. **MP 下 driver 不能泵引擎。** 原方案写的"pause 之后主线程必须继续 `engine.step()`"在 MP 口径下做不到：
   driver 侧 `llm_engine.step()` 就是输出队列上的 `get_output()`，引擎无事可交时**永久阻塞**
   （实测 driver 8 s 内 0 jiffies，同时 EngineCoreProc 烧掉 1 core-second）。**引擎自己泵自己**，
   探针只能轮询。这反而让测量更干净：pause 回包时刻完全由引擎决定，不存在被客户端"假装帮忙"污染的风险。
3. **标定跑的 pause 前置条件是 `in_flight >= 1`，落在第 2 或第 3 个 stage 上 —— 前 4 跑都没打到收尾窗口，
   第 5 跑（`abort`）打到了。** 这不是运气问题：`abort` 自己会把请求推进 `DRAINING`，
   所以"收尾窗口"要靠**取消**来构造，而不是靠等。（补记：Triton JIT 会让每次起引擎多花 1~3 分钟，
   `torch.compile` 缓存命中时约 40 s；标定脚本的暂停前置轮询会**卡在阻塞的探针调用里**，
   所以 `--precondition-timeout` 并不严格约束"单次探针阻塞"的时长。）

**内容**（10~15 分钟，不是 1 小时）：起引擎 → 提交一个请求 → **`abort` 模式下在收尾窗口内**
pause（`has_gr_cleanup_work` 为真 / `by_status` 出现 `DRAINING`）→ 密采 `gr_probe_snapshot`，
打印 `by_status` / `worker_resources_allocated` / `retirement_pending` / `has_gr_cleanup_work` /
`scheduler_has_requests` / `pause_state` 的演化 → 记录 pause 返回耗时。
**先看到会话离开 `_gr_v1_sessions`，再排五小时**（不是 `RETIRING → CLOSED`，理由见下）。
三种 mode 都要各跑一次：`keep` 另外还要验"resume 之后才推进"这一支。

**这三要素原先探针里没有，是本批次先补的**（`561ebbb`，§2.3.3）。不补就只能用 `by_status` 当代理，
而"代理恰好为空"与"引擎真的空闲"在报告里逐字相同 —— 正是 §2.3 ⑥ 那个"负向判据被未执行代码满足"的
同一个坑。补上之后 `has_gr_cleanup_work` 是**在引擎进程内直接调用那个函数**得到的，不是推断。

**DRAINING 不是取消路径独有的。** `GRResourceStatus`（`waiting/running/draining/retiring/closed`）
与 `GRSessionStatus`（`waiting/running/finishing/finished/aborted/failed`）是**两个枚举**，
`gr_probe_snapshot.by_status` 报的是前者。正常完成的最后一个 stage 收到回执时走
`gr_async_scheduler.py:546-549`，置 `FINISHING` + `terminal_result_pending=True` +
`resource_status=DRAINING` —— **所以不需要先取消就能构造出 DRAINING**。
接下来的链条是 `schedule()` 里 `attach_gr_retirement`（`:195`，要求 `total_num_scheduled_tokens == 0`）
→ `RETIRING` → worker 回 release proof → `consume_gr_retirement`（`:225`）→ `CLOSED`。
**实测补充：这条链在 pause 期间能不能走完，取决于 mode，而且 `keep` 压根不会进入这条链**——
它不 abort，因此不产生收尾工作；实测真正落在 `DRAINING` + `has_gr_cleanup_work=True` 上的
是 **`abort`** 那一跑（pause 落在 `DRAINING` 上，2.67 s 正常返回、会话退休）。
**"暂停期间收尾冻结"这个形态要靠 `keep` 窗口内再发取消来构造**，即 §4.5 场景 2，
也是标定跑**尚未覆盖**的唯一一格。

**判据不能用 `CLOSED`。** `_release_owner` 在**同一次调用**里先置 `GRResourceStatus.CLOSED`、
紧接着 `_sessions(scheduler).pop(session.session_id)`（`gr_async_scheduler.py:167-178`），
所以 `CLOSED` **永远不会**出现在任何一次采样的 `by_status` 里；`is_gr_session_retired` 的定义
也正是"不在 `self.scheduler._gr_v1_sessions` 里"（`core.py:242`）。**"退休了"只能观测成
"注册表里没有了"** —— §4.5 写的"至少一次 `RETIRING → CLOSED`"按字面不可观测（本版已订正）。

**已实现为 `tools/soak/calib_pause.py`**，`--pause-mode {keep,wait,abort}`，退出码就是判据，
且**判据按 mode 分支**：

- `keep`：**不得发出新的 dispatch**（`dispatch_records` 在 pause 生效那一刻之后不再增长），
  且会话在整轮结束时已离开注册表 —— 暂停期间离开（pause 落在最后一个 dispatch 之后）或
  resume 之后离开（pause 落在前侧）都算过。**"仍留在注册表"不是失败**，它是竞态的正常一侧
- `wait` / `abort`：期望**退休** —— pause 回包，且存在一个"pause 在效且会话仍在注册表"的采样，
  其**之后**的采样里会话已离开注册表

两个分支都需要"pause 确实在效"这个非空条件，用来排除"引擎本来就空闲所以立刻返回"的真空通过；
注册表读不到时该采样不计为证据（否则一个坏探针就能制造通过）。挂死返 1 且
**不调 `resume_scheduler` 去救** —— 救了就把要找的失败形态掩盖掉了。
它同样拒绝在 `VLLM_ENABLE_V1_MULTIPROCESSING` 为假时启动。

**驱动方式：轮询，不要泵引擎**（本版订正，原方案要求"继续 `engine.step()`"，MP 下做不到 —— 见上文实测第 2 条）。
引擎自己在 `EngineCoreProc` 里推进；探针按 `--poll-interval`（默认 2 ms）只读采样，
`--precondition-timeout` 决定等前置条件的上限，`--settle-seconds` 决定 resume 后的观察宽限。

#### 4.4.1 五小时跑的驱动长跑装置（2026-10-08 补，`pause_soak.py`）

§4.4 上面那份标定一次只测一次 pause，五小时要的是同一形态重复几万次。两者不是同一个装置，
所以五小时之前又做了一轮装置标定：`pause_soak.py` + `run_pause_soak.sh`（两个文件都在 `tools/soak/`，
随工具目录同步到服务器 `/opt/vllm-gr/results/soak/tooling/`）。运行环境与 §4.4 一致：
`VLLM_ENABLE_V1_MULTIPROCESSING=1`、`PYTHONPATH` 指向 `1f28138` 快照、`VLLM_GR_PREFILL_GRAPH_KV_BOUND=4096`。
一次运行一次起引擎，一个场景一个进程（场景 3 崩掉不牵连场景 1 的纪录）。

装置标定共跑了三轮短时确认（240 s、240 s、150 s 各五场景）与一轮 780 s 的场景 2 标定，
**查出五处装置缺陷，全部出在判据与采样侧，引擎侧无一例失败**：

1. **`wait`/`abort` 的前置条件写成了循环依赖。** 第一版要求 pause **之前**就满足
   `has_gr_cleanup_work` 且 `by_status` 出现 `DRAINING`/`RETIRING`。但 `abort` 模式的收尾工作是
   **pause 自己造成的**（它 abort 掉在途请求），`wait` 模式下则是 pause 要**等**的东西 ——
   都发生在 pause 之后。结果是场景 1 每轮都以"会话在 pause 下达前就已注销"记为 miss，
   25 轮触发停机。订正为**三种 mode 统一用标定跑自己的谓词**（`calib_pause.py:318`）：
   `by_status` 非空且 `in_flight >= 1`；`DRAINING`/`RETIRING` 从"前置"降级为"窗口内观测量"。
2. **`keep` 的"暂停期间不得发出新 dispatch"取到了窗口末尾。** 判据原写作
   `max(dispatch_records) over usable[first_alive:]`，而 resume 本来就会把冻结的那一段重新派出去 ——
   场景 4 因此有 866/867 轮失败、场景 5 有 48/49 轮失败，失败理由是
   "a new dispatch was issued after the pause took effect (2 -> 3)"，而那条采样行的
   `pause_state` 是 **`UNPAUSED`**（resume 之后）。订正为两个改动：上报字段限定在
   **暂停生效后的第一段连续区间**；判据改为**相邻两个"暂停在效"采样的比较**
   （暂停期间落下 dispatch 才是增长，注销时计数器归零是下降，允许）。后者同时解决了场景 5
   一轮内多次 pause、计数器跨周期重置的问题。
3. **pause 落在请求完成之后是 miss，不是 failure。** pause 在引擎里的往返约 13 ms，
   而本任务的单个请求也在毫秒尺度完成，于是约 1/3000 的轮次里 pause 到达时请求已经跑完，
   采样器从未抓到"暂停在效且会话在线"的窗口。这类轮次什么都没断言，因此改记为 miss
   （`paused_and_alive_index is None` 且 mode 非 `keep`），与"提交没落地"同类。
   系统性失效不会因此被掩盖：样本数归零时运行直接以"measured nothing"失败。
4. **场景 2 的 `frozen_window_state` 采样时机错了。** 它在取消**之后**才读，报出来的永远是
   取消自己造成的注销状态（`session_count=0`），恰是读者唯一无法据此下结论的那种取值。
   改为在**发送取消之前**采样，另设 `at_cancel_state` 记录取消落下那一刻的状态。
5. **`--registration-timeout` 由 15 s 提到 30 s。** 每个场景开头三轮提交都落在引擎初始化后的
   沉降期（Triton JIT + 权重预取），15 s 内不进注册表，三轮全部记成 miss。一小时窗口里三轮是噪声，
   但短窗口里是 0.1%。

**判据订正两条**（对应 §4.5 原列表，写在这里以免读者去别处找）：

- 原列表"恢复后 `_pending_terminal_result` 归零"**不再单独断言**。该字段在会话对象上，
  而"已注销"的定义就是不在 `_gr_v1_sessions` 里（`core.py:242`），所以它与"注册表排空"
  是同一件事的两种说法，分开写会让一次观测被记成两条判据。
- 原列表"暂停期间 `scheduled.total_num_scheduled_tokens == 0`"**改为看 `dispatch_records`**。
  该断言在引擎内 `gr_async_scheduler.py:190` 已经是硬断言（不满足就早返回），
  而 `total_num_scheduled_tokens` 挂在 `schedule()` 内部构造的 `SchedulerOutput` 上，
  不留在 scheduler 上，驱动侧根本读不到。

**场景 2 的实测结论（订正 §4.4 上面那条推断）。** §4.4 原文推断"`keep` 下唯一能造出
冻结在收尾中的构造是暂停窗口内再发取消"，并预期这条链在暂停窗口内**无法**推进
（`schedule()` 早返回 → 永不 `attach_gr_retirement` → `worker_resources_allocated` 恒 True →
`abort_gr_session_sync` 的无超时自旋永久阻塞）。**实测证伪**：

- 冻结窗口的状态（2833/2834 轮，取消**之前**采样）确有东西被冻住：
  `pause_state=PAUSED_ALL`、`in_flight=1`、`leases=1`、`session_count=1`、
  `worker_resources_allocated=1`、`has_gr_cleanup_work=false`、`terminal_pending=0`。
- 在这个状态下发出取消，会话**在暂停生效期间就注销了**，耗时 **2.2~18.9 ms**
  （中位 12.6 ms），2834/2834 轮如此。`dispatch_records` 在暂停后零增长。

也就是说：`abort_gr_session_sync`（`gr_session_client.py:177-187`）的**无超时自旋确实存在**，
但在本形态下它等的时间是毫秒级，不是永久。**"冻结在收尾中导致取消永久阻塞"这一格没有被构造出来**，
不是因为它被修好了，而是因为 `keep` 的冻结点落在 stage 边界上，退休帧仍能发出并消费完。

**pause 回包耗时：中位数不携带信息，长尾携带信息（2026-10-09 按五小时扫描订正）。**
150 s 标定与冒烟里五场景全部 `pause_seconds <= 0.054 s`（场景 2 中位 0.0478、场景 1 中位 0.0435、
场景 4 的 `keep` 档 0.0434），当时写的结论是"从未走慢路径"。**五小时扫描把它推翻了**。
其中"11 次"这个数是按 **0.1 s** 阈值数出来的，不是 1 s，引用时勿混。

| 场景 | 会话数 | pause 次数 | `pause_seconds` 中位 | ≥0.1 s | ≥1 s | ≥1 s 的具体值 |
|---|---|---|---|---|---|---|
| 1（`abort`） | 1 | 69,210 | 0.0435 | **0** | **0** | — |
| 2（`keep`） | 1 | 66,458 | 0.0478 | **0** | **0** | — |
| 3（`abort`） | 3 | 64,701 | 0.0433 | 1 | 1 | 2.629（第 2 次迭代） |
| 4（轮换） | 3 | 60,610 | `abort` 0.0434、`wait` 0.0541、`keep` 0.0434 | 9 | 5 | **全部落在 `wait` 档**：1.300 / 1.606 / 1.959 / 3.055 / 3.766 |
| 5（`keep`×3 轮） | 1 | 3,402 | 0.0146 | 1 | 1 | 2.630（第 2 次迭代的第 2 轮） |
| **合计** | — | **264,381** | 0.0434（p90 0.0478、p99 0.0546、max 3.7664） | 11 | 7 | — |

pause 次数多于迭代次数，因为场景 5 每轮迭代发起 3 次 pause（272,113 次迭代对应 264,381 次 pause）。

**本表取自五小时扫描。** 其中场景 2、5 因装置缺陷（变更记录 §34、§35）另用修正后的装置各重跑 1 h，
两次运行的样本数与中位数一致（场景 2 `samples=66,369` 对 66,458；场景 5 `samples=1,134`、`pauses=3,402`
逐位相同；中位同为 0.0478 s 与 0.0146 s），因此上表两行无需改动。重跑另给出：场景 2 无一次 ≥0.1 s
（最大 0.0531 s）；场景 5 仅 1 次 ≥1 s，为 2.6375 s，位置与扫描记录的 2.630 s 相同（第 2 次迭代），
即 ② 里那条"不再复现"的判断应订正为**每次运行复现一次**。

**可读出的三点**：

① **`wait` 档单独解释了场景 4 的全部长尾。** 场景 4 的 9 次（≥1 s 的 5 次全部在内）都落在 `wait` 档，
其余两档为零。`wait` 的语义是"暂停新请求、等在飞的活跑完"（即 `PAUSED_NEW`），它的耗时上界由在途工作量
决定，而不是由暂停机制决定，**因此长尾是该档的固有属性，不是缺陷**。

② **剩下两次（场景 3 第 2 次迭代 2.6288 s、场景 5 第 2 次迭代第 2 轮 2.6297 s）归因未定。**
两者数值相差不足 1 ms，却出现在两个独立进程、两个不同模式（`abort` 与 `keep`）里，因此不是模式属性；
且都不再复现（场景 3 自第 3 次迭代起回到 0.043 s）。倾向于是引擎进入稳态前的一次性开销。
**此项不计入判据**，只作为已知上界记录。

③ **两个单会话场景零长尾。** 场景 1、2 合计 135,668 次 pause 无一次达到 0.1 s。场景 2 的重跑另测
66,369 次，同样无一次达到 0.1 s（最大 0.0531 s）。另一个单会话场景（场景 5）只有 ② 中那一次。

§4.4 标定表里 `wait` 报的 2.658 s 那一跑是**未测到**的（`observed PAUSED_ALL: False`、
`paused AND alive: None`），2.658 s 是 pause 在等一个已经跑完的请求，不是等排空 ——
这一条仍然成立，所以 §4.4 第 1 条"回包耗时不作为等价判据"不变，但**上界必须设**：
本装置实测上界是 3.77 s，`--pause-timeout` 默认 180 s 有两个数量级余量。
**中位数量级（0.01~0.054 s）区分的是"引擎有没有活可等"，不是 pause 是否生效。**

### 4.5 五个子场景（标定跑通过之后，各 1 h）

| # | 场景 | 打的是什么 | 本版跑法（`run_pause_soak.sh`） |
|---|---|---|---|
| 1 | 会话在 `DRAINING` / `RETIRING` 时 pause，再 resume | `has_gr_cleanup_work` 的托付是否兑现。**期望按 mode 分**（§4.4 实测表）：`abort`/`wait` = 暂停窗口内排空退休；`keep` = 不发出新 dispatch，resume 之后才离开注册表 | mode=`abort`，1 会话 |
| 2 | 暂停窗口内发起取消（与 §3 组合） | **`keep` 下唯一能造出"冻结在收尾中"的构造**。装置标定已实测（§4.4.1）：冻结窗口确有 `worker_resources_allocated=1`、`in_flight=1`，而在该窗口内取消，会话在暂停生效期间就注销，耗时 2.2~18.9 ms，**不阻塞**。五小时跑的是这个结论在重复几万次下是否稳定，以及 `abort_gr_session_sync` 的无超时自旋是否始终保持毫秒级 | mode=`keep`，1 会话 |
| 3 | 暂停跨越多个会话的完整生命周期 | `finished_req_ids` 残留导致 pause 永不完成（§4.1 反向补丁）。标定跑里 `abort` 已经打到过 `has_gr_cleanup_work=True` + `has_requests=True` 且 pause 2.67 s 正常返回（§4.4），但那一格是**单会话**，多会话叠加仍未测 | mode=`abort`，**3 会话并发** |
| 4 | `PAUSED_NEW` vs `PAUSED_ALL` 两种档位（`mode="wait"` / `"abort"` vs `"keep"`） | 两种 pause 策略下 GR 收尾的差异。**在线 `/pause` 默认走 `abort`**，所以要单独覆盖 | mode **逐轮轮换** `wait`→`abort`→`keep`，**3 会话** |
| 5 | 反复 pause / resume N 次 | 累积漂移 | mode=`keep`，1 会话，**每轮 3 次 pause/resume**，间隔 1 s |

**为什么场景 3 的前置是"3 个会话同时注册"而不是"先有收尾工作"**：收尾工作要等某个会话进入
`DRAINING` 才出现，而三个会话的 stage 边界不同步，等第一个进入 `DRAINING` 时另外两个多半已经注销 ——
那样场景 3 就退化成场景 1。所以它的前置是 `all_alive`（N 个同时在注册表里）
**并加上** `in_flight >= 1`（否则 pause 落在空引擎上，见 §4.5 判据第一条）。

**场景 4 实际跑的是 3 会话**（`run_pause_soak.sh` 里 `3|4` → `--sessions 3`），本表原写"1 会话"与实跑不符，
已按实跑订正。理由是这一格要比的是两种档位下 GR 收尾的差异，而收尾工作只在会话进入 `DRAINING` 后出现 ——
单会话下轮换到 `wait`/`abort` 时多半落在空引擎上，档位差异读不出来。实跑数据印证了这一点：
场景 4 的 `cleanup_states_seen` 里 `['DRAINING']` 恰好 20203 轮、`[]` 40407 轮，与 `abort` 轮的计数逐轮对应。

**场景 5 的采样率要写进报告。** 每轮 3 次 pause + 3 次 resume + 1 s 保持窗口，
装置标定实测 150 s 只出 20 轮、240 s 出 49 轮（约 7.5 s/轮），所以一小时约 480 轮 = 1440 次 pause。
它是五个场景里样本最少的一个，结论的置信度与前四个不同量级。

**判据**：

- **`pause_scheduler` 的阻塞调用在 `PAUSE_TIMEOUT_S` 内返回 `None`。** 不返回就是 §4.1 那个
  "GR 收尾不算 workload 就永不空闲"的 bug，表现为**永久挂起**，所以超时上限必须显式设。
  见 §4.3：`call_utility` 不给 Future，判据按**阻塞时长**表述。
  **但回包耗时本身不是信号**（§4.4 实测第 1 条：同一 mode 报过 0.03 s 与 2.67 s），
  所以它只做上界，不做等价判据 —— 真正的主判据是下面那条注册表迁移。
- **每次 pause 之前的 `in_flight >= 1`**（本版订正，理由见 §4.4.1 第 1 条：`has_gr_cleanup_work`
  在 `abort`/`wait` 下是 pause **造成**的、在 `keep` 下永远不出现，拿它当前置是循环依赖）。
  不满足则该次不计入样本，同时**在日志里点名原因**，不与"提交没落地"混为一谈
- **`pause_state` 确实在效**：采样里至少出现一次非 `UNPAUSED`。**不限定 `PAUSED_ALL`** ——
  `wait`/`abort` 走的是 `PAUSED_NEW`，只认 `PAUSED_ALL` 会让这两档完全没有非空性条件
- **会话注册表真的发生了迁移 —— 这是主判据。** `wait` / `abort` 下会话在暂停窗口内离开
  `_gr_v1_sessions`；`keep` 下要求"暂停生效期间不发出新 dispatch，且整轮结束时已离开注册表
  （暂停期间或 resume 之后均可）"。**不写 `RETIRING → CLOSED`** —— `CLOSED` 不可观测（§4.4）
- 恢复后 `_gr_v1_sessions` 排空（`session_count` / `in_flight` / `leases` 均归零）。
  `_pending_terminal_result` 不再单独断言，理由见 §4.4.1
- 暂停期间 `dispatch_records` 不增长（**取代**原写的
  `scheduled.total_num_scheduled_tokens == 0`，理由见 §4.4.1）。
  **取样区间必须由驱动自己的时钟界定，不能用探针的 `pause_state`**（2026-10-09 订正，理由见变更表第 34 条）：
  只比较「发送 `pause_scheduler` 之后、发送 `resume_scheduler` 之前」的相邻采样，且同一轮有多个 cycle 时
  逐 cycle 分别取样、不在 cycle 之间连对。用探针档位划区间会把 resume 之后对冻结 stage 的重派发误判成违规 ——
  五小时扫描里场景 5 有 499/1134 轮这样被误判，而它们全部落在第一个 resume 边界上。
  引擎侧的依据是 `scheduler.py:349`（`PAUSED_ALL` 把 `token_budget` 置 0），所以真在该区间内发生的增长是硬违规
- 无长期滞留的 `RETIRING`（超过 `--retiring-grace`，默认 10 s）
- `has_requests()` 在清理完成后返回 False
- 恢复后引擎可复用（每 `--reuse-check-every` 轮跑一次正常提交→注册→注销的完整周期，记入 `reuse.jsonl`）

**不计入样本的两类轮次**，都要在 `summary.json` 里与"通过"分开计数：`miss`（前置未达成，
含提交没落地与 pause 落在请求完成之后）与 `wedge`（pause 未在 `--pause-timeout` 内返回，
或 resume 后注册表未排空）。连续 miss 达到 `--max-consecutive-misses`（默认 25）即停机并报
"前置不可达"，不打满一小时的空样本。

---

## 5. 批次 4：组合轮

### 5.1 必要性

**单点跑通不蕴含组合跑通。** 批次 2 制造 DRAINING 状态、批次 3 在 DRAINING 上暂停，
两者对同一组字段的期望值可能不同。具体地，交集在 `_discard_pending_native_finish` 的
`worker_released=True` 分支（`engine_core_patch.py:707-733`；**是 #403 引入的基线代码，不是 #427 新增**，
见 §1.4.3 的订正）：它在 **PAUSED_ALL 且会话被融合释放**时才生效，而这正是"暂停 + 取消"同时发生
才构造得出来的状态 —— 批次 2 或批次 3 单独跑都不会进入它。

### 5.2 代码在哪

无新增代码，打的是批次 2 与批次 3 的代码在**同一时刻**的交互：
`engine_core_patch.py:710-733`（discard 分支）、`:376-410`（abort 后的会话同步）、
`gr_async_scheduler.py:515-518`（`owner_generation` + `dispatch_id` 双重校验）、
`:529-530`（`_dispatch_records` 对象同一性）。

### 5.3 怎么测

**装置**：`tools/soak/combo_soak.py`（驱动器 `run_combo_soak.sh`）。它复用 `pause_soak.py` 的
采样、等待、评估部件，只在冻结窗口内加一个钩子：发 abort，紧接着用同一个 `session_id` 提交。
暂停模式固定为 `keep` —— 只有 PAUSED_ALL 会持有会话，`wait` / `abort` 按设计就地了结 in-flight
工作（那是批次 3 的场景 1 和 4）。

**场景矩阵**（挑交叉点，不做笛卡尔积）：

- 暂停期间取消一个已处于 DRAINING 的会话
- 在上述取消的**同一采样窗口内**、用同一个 `session_id` 提交新请求，并记录这次 ADD 是否被受理
- 暂停恢复、且该 `session_id` 已从注册表消失之后，再用同一个 `session_id` 提交一次
- 上述动作夹在 prefix hit / miss 交替的负载里（保持与现有 soak 相同的请求形状）
- 时间上覆盖多个 pause / resume 周期

**"同一个 `session_id`"要求 id 在两次提交上都被钉住，且引擎的 id 随机化必须关掉。**
`InputProcessor.assign_request_id`（`vllm/v1/engine/input_processor.py:215-232`）在请求进入调度器
之前把每个 id 改写成 `f"{external_req_id}-{random_uuid():.8}"`，而会话注册表 `_gr_v1_sessions`
的键正是这个改写后的 id。只钉住第二次提交，得到的是 `<第一次的 id>-<8 位十六进制>`，与第一次
不是同一个会话 —— 这一半场景会静默地什么都没测。因此跑之前必须设
`VLLM_DISABLE_REQUEST_ID_RANDOMIZATION=1`（项目自己的 `tests/test_beam_search_v1_input_processor.py`
也这么用），harness 在启动时对未设置直接拒绝运行。其余请求仍然各拿一个新 id，这是全轮里唯一
一处两个请求共用一个 id 的地方。

**冻结窗口内的同 id 提交会被 GR 拒绝，这是被测行为，不是装置缺陷。**
`gr_async_scheduler.py:254-255` 的准入判据是 `if session_id in sessions: raise GRAdmissionError(...)`。
被取消的会话从发出 abort 到离开注册表需要 10 ms 量级（批次 3 场景 2 实测中位 0.0126 s），
而本装置的同 id 提交是在 abort 发出后**立刻**发出的（实测中位 0.094 ms），因此准入判据在这一瞬间
必然读到该 id 而拒绝它 —— 这是时序的必然结果，不是"session 一直占着 id 直到 resume"。
异常在 `engine_core_patch.py:136-144` 被接住、记为 `Rejecting request %s during GR admission: %s`
并转成一个 error output —— 即客户端的 `submit` 无论受理与否都会正常返回，**不能把返回当成受理**，
必须另取一次晚于提交时刻的探针采样来看该 id 是否仍在注册表里。所以这一条要测的是
"被拒绝之后引擎仍然干净"：拒绝不得妨碍 resume 时的正常退场。真正可达的同 id 复用是
**resume 之后**的那一次（跨 `owner_generation`，与 §3 场景 7 走同一段代码），它才是本批次
关于 id 复用的主判据。

**规模**：连续 4~8 h。**本轮执行**：按用户要求压缩时长，正式轮跑 2 h
（`run_combo_soak.sh` 的 `DURATION_SECONDS=7200`，`PAUSE_CYCLES=2`）。

**判据**：

1. 批次 2 与批次 3 的判据**同时**成立。
2. 冻结窗口内的同 id 提交被准入拒绝（若未被拒绝，须能解释为 id 已提前释放），
   且拒绝不留下残留：resume 后 `in_flight=0`、`dispatch_records=0`、注册表为空、
   `reset_prefix_cache()` 恒 `True`。
3. resume 之后的同 id 复用**被受理并正常退场**：该 id 在提交后重新出现在注册表里，
   随后被回收到注册表为空。
4. **计数判据要用可判定的形式。** `soak_probe.py` 的 `_COUNTERS` 是模块级 `Counter`，随进程
   单调累积，所以 §2.3 ⑥ 那种"每一个计数在整轮里都不单调上升"的写法在本批次恒不成立
   （累计量天然上升，与泄漏无关）。可判定的泄漏形式是导出量 `gr_retire_frames - gr_retire_consumed`
   （retire backlog）恒 0 或值域有界不上升，以及 `gr_protocol_failures` 全程恒 0；二者仍用
   现有报告工具的 Mann-Kendall + 斜率置信区间 + 幅度三条件来判，原始累计序列照旧落盘，
   供报告引用而不是当判据。

---

## 6. 批次 5：全量 5 h 基线复跑（新口径）

### 6.1 必要性

**目的不是再证一次"没泄漏"，而是确认新口径没有引入观测偏差。**
批次 1 换了探针（新增 Core 侧查询、改 watchdog 的进程定位），而**探针本身不能改变被测量的东西** ——
现有 harness 用 `vars()` 而不是 `getattr()` 就是为这个（`getattr()` 会触发 property 描述符）。
所以口径变更之后必须有一次可对照的运行，证明结论不是观测方式变出来的。

**第二个目的是本次新增的：#389 的数值契约在 5 h 尺度上不漂移。**
批次 2–4 都在制造异常路径（取消、暂停、组合），异常路径不覆盖"长时间稳态下 logprob 归一化域是否稳定"。
只有一轮纯粹的正常请求长跑能回答它（§1.5）。

### 6.2 怎么测

用批次 1 的双口径探针跑一次与 R2 同规格的 5 h。
避开 03:00–05:30（crontab 的 `sync_scripts.sh` 与 `daily_runner.sh` 会抢 GPU 和仓库），
其余流程沿用 `tools/soak/README.md`。

**与 R1-427 对照时只能比延迟与内存形状，不能比指纹。**
R1-427 跑在 `74c4a9e`，本轮跑在 `9c71c9d`；两者之间 `vllm_gr/constraints/*` 与 `vllm_gr/ops/*` 有实质改动，
token 序列本身就可能不同（§0.2 ⑤、§1.5）。指纹只在本轮**自身**内部可比 ——
harness 的 golden 基线也是每次运行各自建立（`run_soak.py:1117-1124`），这一点正好对上。

**判据**（前三条对照 R1-427，后三条只看本轮自身）：

1. 延迟分布与内存形状与 R1-427 一致；无新增的持续上升序列；`null` 栏为空或已解释。
2. `reset_prefix_cache()` 恒 `True`（§7.3 第 1 条），时机按 §7.3 第 2 条订正后的写法。
3. §2.3 ⑥ 的五个计数器：`gr_protocol_failures`、`gr_device_errors` 全程恒 0；
   `gr_prefill_fastpath_hits + gr_prefill_declines` 等于请求数（无漏计）；
   `gr_retire_frames == gr_retire_consumed`，`gr_dispatch_records_max` 不单调上升。
4. **logprob 指纹不漂移**：把 §2.3 ⑤ 的量化 `cumulative_logprob` 指纹按每 30 min 分桶，
   同一条请求（同一 `golden_baseline` 语境）跨桶逐位一致；
   出现任何一次不一致就按 §1.5 的三行分工表归因，不要直接记成"#389 数值不稳"。
5. **退化顺序断言恒成立**：每轮输出的 `cumulative_logprob` 随 beam rank 单调不增
   （§2.3 ⑤ 的附加断言）。这条在 5 h 尺度上比单点数值更有价值 —— 归一化域写错时
   退化顺序会先于绝对值崩。
6. 量化步长（§2.3 ⑤ 要求写进 `meta.json` 的那个数）在整轮中没有变化；
   变了说明统计口径被中途改动，本轮作废。

### 6.3 执行记录

已执行：1 次 5 小时正式轮（2026-10-09 17:00:40 → 22:02，176,458 对请求）与 1 次 900 秒短时确认。
结果与逐条判据见 `SOAK_RESULTS.md` §3.7、分析见 §4.8。

判据 2 至判据 6 全部成立。**判据 1 需要订正一处措辞**：本轮它触发了 5 条上升序列，其中 4 条是
`scheduler.requests`、`scheduler.running`、`_beam_sessions`、`_gr_v1_sessions` —— 本负载串行
单请求（`max_num_seqs=1`），这四个量的取值集合就是 `{0, 1}`，**上限由负载结构决定**。趋势检验
对它们的判定实际测的是「采样落在 1 上的比例有没有变」，即采样与请求循环的相位关系。判据对
这一类序列应当取**上限是否被抬高**，而不是均值是否移动；前者才与泄漏同义。

另有两处装置层面的限制记录在后：深走查在五小时里只执行 60 次（每约 300 秒一次），尾部窗口仅
36 个样本，趋势检验的 p 值（0.028、0.042）由「尾部恰好取到哪个值」主导；`proc::rss_kib` 的
上升与基线同形（头一小时逐点吻合在 0.3% 以内，基线只在一小时处停止测量），且同期 CUDA 分配量、
GC 对象数、切分块余量、线程数全部不涨。

---

## 7. 贯穿所有批次的不变量判据：KV 租约

### 7.1 必要性

**租约是"异步窗口内 KV 不被回收"的唯一保证。** 这句话有确切含义，值得写清楚：

```python
# vllm_gr/v1/engine/gr_async_scheduler.py:384-394
# Scheduler owns the native block refcounts. Python references in a
# Worker cannot prevent block reuse after abort or preemption.
for group in blocks:
    touched.extend(group); lease.extend(reversed(group))
manager.block_pool.touch(touched)                       # ref_cnt += 1
session._kv_dispatch_leases[dispatch_id] = tuple(lease)
```

block 能否被复用只由 scheduler 侧的 `KVCacheBlock.ref_cnt` 决定；worker 进程里握着一个
`KVCacheBlock` 的 Python 引用**完全不阻止**该 block 进入 free queue。abort 或 preempt 时
`_preempt_request`（`scheduler.py:929`）会立刻 `self.kv_cache_manager.free(request)`（`scheduler.py:938`），把 ref_cnt 降到 0
并放回 free queue，**而 worker 可能还在读这块 KV**。所以必须由 scheduler 自己额外 `touch` 一次
把 ref_cnt 顶成 2，再用 lease 在 `update_gr_from_output:525-527` 减回去。

释放只在回执到达时发生（`:525` 的 `pop` 与 `:527` 的 `free_blocks` 在同一处）。
**取消路径不显式还租约** —— `abort_gr_session` / `finish_gr_session` / `_close_gr_session` /
`release_gr_session` 都不碰它（`gr_async_scheduler.py:602-644`）。归还只发生在
`update_gr_from_output` 里，而且**不是"开头无条件"**：`pop` 在 `:525`（函数第 21 行），
被 `:512-513` 的 `metadata is None` 早返回和 `:515-518` 的 `owner_generation` / `dispatch_id`
双重校验挡住才执行。**所以脏回执不会还租约** —— 这比"回执不来就不还"更强，
也正是批次 2、3、4 的**共同下游**：这三批都会制造"回执可能不来、或者来了但对不上"的时刻。

引用点不止两处：`gr_async_scheduler.py` 内两处（`:377` 建立、`:525` pop）+ 字段声明
`beam_search_v1.py:215`，另有 3 处测试断言。改这块时按**这 3 个非测试引用点**数。

**为什么这一维度是判据而不是压力源。** 原计划想造驱逐压力来测这条路径，读了代码之后发现**造不出来**，
而且方向是反的。三处：

**修正一：前缀缓存不消耗可用块。** `get_num_free_blocks()` 统计的是 free queue，
而 **cached block 就在 free queue 里**（`block_pool.py:43-49`、`:166` 的 docstring
「including eviction candidates when caching is enabled」）。所以**前缀缓存本身永远不会让
`allocate_slots` 返回 None**，只会让 `_maybe_evict_cached_block` 更频繁地跑（良性驱逐）。
能让分配失败的只有 `ref_cnt > 0` 的块，即活着的请求。

**修正二：单请求占用 57~69 个 block，与 beam_width 无关。**
单请求只占**一张** block table（`num_reqs=1`，`seq_lens_cpu` 是单一标量，
`beam_attn_metadata.py:204/214/225`、`:176-181`）；128 条 beam 自己的 suffix KV
**不在 block pool 里**，在 `BeamAttentionContext` 的固定池（启动时按
`max_batch × max_beams × num_layers × max_decode_steps × kv_heads × head_dim` 一次性分配，
`beam_attn_gpu.py:254-286`）。引擎自己也这么算：`extent = prompt_len + max_tokens - 1`，
`blocks = ceil(extent/block_size)`（`:284-288`）；实算 prompt 900~1100、block_size=16 →
`ceil(901/16)=57`、`ceil(1101/16)=69`。原方案"`beam_width 128 → 256` 让单请求 KV 翻倍"**不成立**。

**修正三：没有任何 `gpu_memory_utilization` 取值能触发抢占 —— 方向是反的。**

```python
# vllm_gr/v1/engine/gr_async_scheduler.py:284-289
extent = len(engine_request.prompt_token_ids) + request.beam_params.max_tokens - 1
if (extent >= manager.max_model_len
        or (extent + block_size - 1) // block_size > pool.num_gpu_blocks - 1):
    raise GRAdmissionError("Asynchronous scheduling request exceeds the total native KV capacity")
```

**准入闸用的是静态 `pool.num_gpu_blocks`，真实约束是动态 `get_num_free_blocks()`。**
把池子压小，请求会在**准入闸**被拒，而不是被抢占。
**这个差值正是缺口**：只有「静态容量够、动态 free 被吃掉」（= refcount / 租约泄漏）才能让请求
**通过准入**却在运行时分配失败 —— 而那正是被测目标本身，不能当手段。

另外两点：`PreemptionMode` 在**部署的 0.22.1 里已不存在**（`vllm/` 下全量 grep 零命中；
只有文档里一句旧日志样例），
该版本只有 recompute 一种抢占，唯一成因是 `allocate_slots()` 返回 None（`scheduler.py:489`）；
且 `run_soak.py:224` 在 v1 下设 `max_num_seqs=1`，**连并发都没有**。

**结论**：真正的驱逐 / 抢占路径要覆盖，只能换 legacy `beam_search` API
（那里 `max_num_seqs = max(128, beam_width)`，有真并发，但**不走 #427 的 lease 机制**，
测不到我们要测的东西）。留作独立议题另行评估。本方案聚焦**不变量断言**。

### 7.2 代码在哪

| 环节 | 位置 |
|---|---|
| 建立租约 | `gr_async_scheduler.py:388-394`（`touch` + 记账） |
| 释放租约 | `:525` 的 `pop` + `:527` 的 `free_blocks`（同一处，`update_gr_from_output` 里唯一） |
| 释放的前置门（**不是无条件**） | `:512-513`（`metadata is None` 早返回）、`:515-518`（`owner_generation` / `dispatch_id` 双重校验） |
| 字段声明 | `beam_search_v1.py:215` |
| 泄漏的硬症状 | `:137-148`、`:146-148` 的 `RuntimeError("...lost reserved KV capacity after Beam generation")` |
| 布尔 oracle | `vllm/v1/core/block_pool.py:454-470`（`reset_prefix_cache`；`num_used_blocks` 检查在 `:463-464`，`return False` 在 `:470`） |

### 7.3 怎么测

**`reset_prefix_cache()` 是本 harness 里唯一二值、且最灵敏的泄漏判据。**

```python
# vllm/v1/core/block_pool.py:454-470 (部署口径 vllm 0.22.1 @ g0decac0d9)
num_used_blocks = self.num_gpu_blocks - self.get_num_free_blocks()
if num_used_blocks != 1:  # The null block is always marked as used
    logger.warning("Failed to reset prefix cache because some blocks (%d) are not freed yet", ...)
    return False
```

而 soak 把它当硬错误：

```python
# tools/soak/run_soak.py:1075-1076
if not llm.reset_prefix_cache():
    raise RuntimeError(f"prefix cache reset failed before request {index}")
```

**这有一个重要推论：现有四次运行、207,606 个请求，每一轮请求之前都断言了「所有 KV block 已归还」。**
也就是说 **lease 归还机制已经在 happy path 上被隐式验证了 10 万次** —— 只是报告里没这么写。
（已补进 `docs/soak_stability_report_en.md` §5.6 与中文版。）

**所以：不能删 `reset_prefix_cache()`。** 要测的是**在取消 / 暂停 / 复用路径上它是否仍然返回 True**。
具体做法：

1. 把 §3、§4、§5 的判据里都加上"`reset_prefix_cache()` 恒 True"
2. **在"会话已经完全退役"之后调，不能"取消 / 暂停动作之后立即"调**（本版订正）。
   判据是 `vllm/v1/core/block_pool.py:463-470`：`num_used_blocks != 1` 就返回 `False`，
   而**任何还活着的请求都会占 block**。租约的**设计目的**正是在异步窗口里把 `ref_cnt` 顶住
   （§7.1），所以窗口内调它**必然 False**，而那个窗口恰恰是本批要测的状态 ——
   原写法会把它变成系统性误报。
   正确时机：`is_gr_session_retired(sid)` 为真 **且** `_kv_dispatch_leases` 归零之后，
   再调一次；同时保留每轮请求前的那次（`run_soak.py:1075`）。
3. 配合 `block_pool.get_num_free_blocks()` 回到基线，以及 `_kv_dispatch_leases` 归零的**时序**
   （是"回执到达时归零"还是"abort 时归零"—— 后者说明归还提前发生了，违反窗口保证；
   注意 §7.1 的双重门意味着**脏回执永远不归还**，这是另一条要单独抓的形态）
4. 用 §2.3 ⑥ 的 `gr_protocol_failures` 计数器做交叉验证：租约不还时它是否开始非零 ——
   有的话，泄漏会先在这里露头，比 `reset_prefix_cache()` 更早

**泄漏的后果链**（用来判断"变慢了"到底是不是这条链）：

1. block `ref_cnt` 永远 ≥1 → 永不回 free queue → `get_num_free_blocks()` 单调下降、`kv_cache_manager.usage` 单调上升
2. 超过准入闸的静态余量后，`allocate_slots` 返回 None。此时**不是**普通抢占：
   - 已发过 stage → 硬错 `RuntimeError("Asynchronous scheduling lost reserved KV capacity after Beam generation")`（`:137-148`）
   - `issued_stages == 0` → 落到原生 `scheduler.py:485-489` 自我抢占（`preempted_req == request` → break），
     下一轮重新 prefill，形成 **livelock**
3. `reset_prefix_cache()` 返回 False（最早、最二值的信号）

---

## 8. 已知盲区

- **ModelRunner V2 整条路径不在覆盖范围内**（§0.2 ⑤）。本方案全批次跑 V1 —— 也就是 vllm-gr 的默认口径
  （`_env_check.py:43` 强制 `VLLM_USE_V2_MODEL_RUNNER=0`）。要覆盖 V2 需要：设
  `VLLM_USE_V2_MODEL_RUNNER=1`、确认 `beam.execution_mode='v1'`、并把 §3~§7 的判据在
  `gpu_beam_stage_runner_v2.py` / `gpu_model_runner_v2_patch.py` / `gpu_prefill_graph_runner_v2.py`
  这一套上重走一遍（`_StageExecutionHooksV2` 的 `persistent_graph_install=False` 是与 V1 相反的一处，
  见 §0.2 ⑤ 第 2 条）。**这是独立的一轮，不能混进本方案。** 写报告时必须显式写"V1 口径"，
  不能只写"目标 commit 已验证"。
- **批次 1 的负载不会触发退休帧**（§2.3.4 冒烟 180 s 与 §2.3.5 `mp0-val30` 33,060 请求两处实测）。
  `gr_retire_frames` / `gr_retire_consumed` 在纯完成路径上恒为 0（正常收尾不走退休帧），
  所以**这两个计数器在批次 1 无语义**，它们的归属是批次 2~4。不要把批次 1 的 0 读成"退休机制没生效"。
- **批次 1 的双口径门不覆盖 `561ebbb` 新增的五个探针字段**（§2.3.5）。快照 rev 是 `cd0a28d`，
  `has_gr_cleanup_work` / `scheduler_has_requests` / `pause_state` / `worker_resources_allocated` /
  `retirement_pending` 在门的两次跑里根本不出现。引用这批字段时必须引标定跑（`1f28138` 快照），
  不能引本门。
- **MP 口径下探针线程会打崩引擎**（§2.3.5 实测，`mp1-val30` 12:02:32 崩）。
  `SyncMPClient` 没有锁，`ProbeThread` 的 `call_utility` 与主线程的 `step()` 写同一个 ZMQ socket，
  Core 侧解出 `ValueError: b'\x00\x00' is not a valid EngineCoreRequestType`。
  修法是 `run_soak.py:install_input_send_lock`，`mp1b-val30` 跑满 32 分钟未复现（§2.3.5）。
  边界仍要写清：**本方案之前所有 MP 口径的运行（如果有）都带着这个缺陷**，
  而 `install_input_send_lock` 装在 `ProbeThread.start()` 之前是硬要求 ——
  任何**绕过 `run_soak.py` 自己起 MP 引擎**的脚本（含两个标定脚本）都要自己保证没有第二个 socket 写者。
- **MP 口径只有一条 fastpath 证据**（§2.3.5 实测）。`prefill_fastpath` 在 MP 下报
  `unavailable_multiprocess`（model runner 在 worker 进程里，driver 读不到），
  所以"引擎侧 32484 / 客户端侧 33060 两来源互证"**只对单进程口径成立**。
  批次 3 / 4 / 5 都跑 MP，`gr_probe_counters` 是唯一来源 —— 它接错了没有第二条证据能发现。
- **`free_blocks` 只能跟同一次跑自己的基线比**（§2.3.5 实测，订正过两次）。
  第一版说是口径差：MP=0 报 9,875 blocks、MP=1 报 9,821（`158,000` vs `157,136` tokens）。
  第二次被 `calib_k.py` 推翻 —— 它也是 **MP=0**，却报 `157,136`。
  **所以同一个口径能给出两种池子，54 块的差额与口径无关**，是启动抖动
  （四跑里唯一的分野是 `Initial profiling/warmup run took`：0.23 s 那跑池子最大，1.2 s 的三跑都小）。
  **落地要求**：`meta.json` 不记录 `GPU KV cache size`（只有 `block_size`），
  池子只在启动日志里，所以每批要么从日志抄下本次池子、要么只报"占用 ÷ 容量"；
  `free_blocks` 的"回落"判据一律**同跑自比**。跨口径、跨跑比都是错的。
- **看门狗的进程集合是它自己认定的，不是客观的**（§2.3.5 实测两次）。一次是 MP 口径下
  `EngineCoreProc` 改名成 `VLLM::EngineCore` 而漏采（少 2.9 GB），一次是 `process_lost` 之后
  同一个数字 pid 被复用、曲线尾部描述的是另一个进程。读 RSS 曲线时**以 `pid_found` / `proc_set`
  事件为准**，不要以"pid 号还是那个号"为准；`proc_count` 恒为 1 时先确认是单进程口径还是漏采。
- **"跑的哪个 ModelRunner"没有进 `soak.log`**（§0.2 ⑦）。当前 run 只记 engine kwargs 与
  `prefill fastpath counter: installed`，两者都不区分 V1/V2。**已列入批次 1 的 harness 改动**
  （把 `os.environ.get("VLLM_USE_V2_MODEL_RUNNER")` 写进启动日志），在落地之前，
  任何一轮的 runner 口径都只能靠"没人设过这个变量"来推断。
- **Xid 通道**：宿主机非 root 读不了 dmesg（`/dev/kmsg`、`journalctl -k` 同样受限），
  现有四次运行对驱动级故障是盲区。需外部捕获内核日志后经 `--xid-log` 引入，或明确接受。
- **多进程下的对象图探针**：见 §2.1。批次 1 之前，MP 下这些探针会静默变成 `null`。
  **批次 1 之后**：会话/生命周期字段已由 `gr_probe_snapshot` 覆盖（§2.3 ①），
  但 `torch.cuda.memory_allocated/reserved`、`gc_counts`、beam registry **仍然是 `null`**
  —— 它们没有走新探针，报告里读起来仍像"这项是干净的"。
- **多进程下的 RSS**：见 §2.1，可能采错进程。**批次 1 必须解决**，否则批次 3 的内存结论无效。
- **真正的驱逐 / 抢占路径测不到**：见 §7.1。要覆盖只能换 legacy API，那是另一个议题。
- **`submit_once` 返回前取消（§3.3 场景 4）**：离线同步路径不可达，需异步客户端。
- **`tools/soak/` 整个目录未被 git 跟踪**：harness 自身没有版本管理，
  所有 harness 改动都不会进 code review。`SOAK_REV` 只能证明被测的引擎代码，不能证明 harness。
- **工作区副本与 74c4a9e 的差异**：`_run_sync_cleanup` 在 74c4a9e 对 `EngineDeadError` 立即抛出，
  工作区副本没有这个分支（§3.3）。harness 与引擎的版本对应关系要显式记录。
- **#389 的数值正确性不在这里证**（§1.5）：本方案只证"不漂移、不触发协议错、跨父提交可比"。
  单点数值对不对，证据是 #389 自带的 91 + 296 行单测；soak 与它们**分工，不重复**。
  写报告时不要把"5 h 没崩"写成"归一化域算对了"。
- **同一个上游符号存在三个互不相同的行号口径**：本文统一用**部署口径**
  （容器里的 `vllm 0.22.1 @ g0decac0d9`，site-packages）。另外两个是
  本地 worktree `D:/code/vllm-gr/vllm/vllm`（`main`，读取时 HEAD `8efa13b700`）与
  `docs/runtime_patch_inventory.md:15-18` 声明的基线 `releases/v0.22.1` → `dd72658e`。
  差异不是几行而是几十到几百行（`block_pool.reset_prefix_cache`：454 / 764 / 761；
  `scheduler.get_num_unfinished_requests`：1872 / 2470 / —）。**行为性结论三者一致，只有行号会漂。**
  本文一度按本地 main 改过一批行号，已全部回滚 —— 教训是**读上游行号前先确认是哪一份**。
- **批次 5 与 R1-427 的指纹不可比**：见 §6.2 开头。R1-427 在 `74c4a9e`，本轮在 `9c71c9d`，
  中间有 `constraints/*` 与 `ops/*` 的实质改动。只比延迟与内存形状。

---

## 9. 附录：代码位置索引

**版本口径**：`vllm_gr/*` 全部按被测提交 `9c71c9d`（探针快照 rev 记
`cd0a28d` → `561ebbb` → **`1f28138`（批次 2 起实际使用）**，见 §2.3.3）；上游 `vllm/*` 按**部署的那一份**
—— 容器内 `vllm 0.22.1`、`__commit_id__ = g0decac0d9`、site-packages 安装。
本地已有两份同名 checkout 与实跑版行号不同，详见 §8 与文首口径块。

| 主题 | 位置 |
|---|---|
| 离线取消链路 | `gr_session_client.py:177-192`、`engine_core_patch.py:376-410, 495-498` |
| abort 逐分支 | `gr_async_scheduler.py:639-648` |
| 六处语义变化 | `gr_async_scheduler.py:163-170`（`_drain_resources`）、`:183-188`（`attach_gr_retirement` 的守卫，函数定义在 `:181`）、`:224-228`（`consume_gr_retirement` 尾部，定义在 `:201`）、`:345-358`（dispatch extent）、`:511-527`（`update_gr_from_output` 的双重门）、`:568`（`_fail_gr_dispatch`，紧跟在 `:559` 的 `_make_failure_result` 之后） |
| 租约建立 / 释放 | 建立 `gr_async_scheduler.py:377`（写 `_kv_dispatch_leases`）；释放 `:525` (`pop`) + `:527` (`free_blocks`)；**前置门 `:512-513`（`metadata is None` 早退）与 `:515-518`（`owner_generation` 不匹配或 `dispatch_id` 不在在飞集合）、`:459-467`（回执与元数据的双重校验）**；字段 `beam_search_v1.py:215` |
| 准入闸 | `gr_async_scheduler.py:251-296`、`beam_search_v1.py:114-177` |
| pause 定义 | `vllm/v1/core/sched/interface.py:22-33`、`vllm/v1/engine/core.py:663-692`（`EngineCore`）、`:1571-1612`（`EngineCoreProc`，idle 回调 `:1253`） |
| GR pause 钩子 | `engine_core_patch.py:449-453, 710-733`（#403，非 #427，见 §1.4.3）、`gr_async_scheduler.py:64-73` |
| pause 契约文档 | `9c71c9d:docs/beam_stage_pr2.md:73-80` |
| `is_gr_session_retired`（#427 新增，批次 2 的驱动） | `core.py:240-242`、装入于 `engine_core_patch.py:191` |
| #389 数值契约 | `constraints/scoring.py`（`score_selected_tokens`）、`ops/` 的 `rec_constrained_topk`；判据见 §1.5 |
| 既有单测 | `tests/test_gr_pause_resume.py`、`tests/test_gr_async_scheduler.py:751/:787`、`tests/st/test_onerec_v1_scheduler_st.py:401-430`；#389 另带 91 + 296 行（见 §1.5） |
| 探针定义 / 调用点 | 定义 `run_soak.py:386-`（`select_probes`）、`:504-526`（`scan_containers`）、`:528`（`locate_engine_core`）、`:546`（`find_beam_registry`）、`:651-665`（`_find_gr_sessions`）；调用点 `:585-`（`ProbeThread`）、`:761-771`。`soak_watchdog.py:177-214, 320-350` |
| 布尔 oracle | `vllm/v1/core/block_pool.py:454-470`（部署口径）、`tools/soak/run_soak.py:1075-1076` |
