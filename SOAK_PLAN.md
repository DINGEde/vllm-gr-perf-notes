# vllm-gr 长稳测试方案

> 本文是对 `tools/soak/` 后续测试方向的调研与方案。所有结论以代码为准，逐条给 `file:line`。
> **版本口径**：`vllm_gr/*` 一律取自 `git show 74c4a9e:<path>`。工作区 HEAD 是 `42e633b`，
> **不含 #427**，行号与 74c4a9e 差 5~18 行，不可混用。`vllm/*` 指上游基线
> `vllm-project/vllm@releases/v0.22.1`（`dd72658e`，见 `74c4a9e:docs/runtime_patch_inventory.md:15-20`），
> 本地 checkout 在 `D:\code\vllm-gr\vllm\vllm`（**注意其 worktree 是 main，行号与 0.22.1 tag 不同**，
> 例如 `_preempt_request` 在 main 是 1336、在 0.22.1 是 929）。

---

## 摘要

**要解决什么。** 四次长跑（R0/R1/R1b/R1-427，3.25 小时、207,606 个请求、零硬故障）走的都是会话生命周期里**唯一一条**路线：提交 → 完成 → `release_gr_session`。取消、原生暂停、驱逐、抢占这四条路线一条都没走到 —— 而这四条在 `a94830c8` 就已经存在，只是从没被端到端验证过（§1.1）。这批测试就是补它们。

**五批，按依赖排序而不是按价值排序：**

| 批次 | 做什么 | 前置 | 规模 | 主要判据 |
|---|---|---|---|---|
| **1**（§2） | **探针基础设施**：Core 侧可查询的探针摘要、harness 双口径、watchdog 修 fork 同名 | — | 开发为主 + 1 次 30 min 验证跑 | 单进程与多进程两套口径读出同一组字段 |
| **2**（§3） | **取消 soak（离线）**：1 次标定跑 + K 步驱动 + 看门狗，七个子场景 | 1 | 7 场景 × 1 h = 7 h 循环 | 无挂死；`_gr_v1_sessions` 无 ABORTED 残留；`reset_prefix_cache()` 恒 `True` |
| **3**（§4） | **pause soak（多进程）**：`call_utility("pause_scheduler", ...)`，五个子场景 | 1、2 | 5 场景 × 1 h = 5 h 循环 | pause Future 能在合理时间内完成 |
| **4**（§5） | **组合轮**：取消 × 暂停 × 同 id 复用 × prefix hit | 2、3 | 4~8 h 连续循环 | 批次 2 与 3 的判据同时成立，且快照无单调上升 |
| **5**（§6） | **全量 5 h 基线复跑**（新口径） | 1~4 | 5 h 循环 | 与旧口径结论一致，无新增持续上升序列 |

合计约 **26 小时请求循环**，外加标定与开发开销。时间不设上限（§0.1），上面的"规模"是运行量而不是预算。

**四条必须先知道的前提**（详见 §0.2）：

1. **这些路线不是 #427 引入的。** 会话表、五态机、`has_gr_cleanup_work`、原生暂停钩子在 `a94830c8` 就全部存在。这是补一个老缺口，不是验证一次新改动 —— 所以判据要按"能力是否成立"来定，而不是按"#427 有没有改坏"（§1.4）。
2. **批次 3 强制多进程**（`has_work()` / idle 回调 / 返回 `Future` 的 `pause_scheduler` 只存在于 `EngineCoreProc`，§4.3），而多进程会让现行对象图探针**静默变成 `null`**（§2.1）。所以批次 1 必须在批次 3 之前，否则批次 3 拿到的是空白，而且看不出是空白。
3. **批次 2 必须在批次 3 之前**：只有取消机制能稳定制造"会话处于 DRAINING / RETIRING"的状态，那是 pause 场景的必要输入。
4. **不测真正的驱逐 / 抢占** —— 造不出来（§7.1）。本方案测的是它的**不变量**（租约归零、free list 顺序、`reset_prefix_cache()` 恒 `True`），不是压力本身。

**怎么读这份文档**：§1 说明测试范围是怎么定下来的；§2~§6 是五个批次，每节按「必要性 → 代码在哪 → 怎么测」展开；§7 是贯穿所有批次的不变量判据；§8 是已知盲区；§9 是代码位置索引。

---

## 0. 本版修订说明

### 0.1 已定的约束（不再作为决策项）

| 约束 | 取值 |
|---|---|
| 取消测试的驱动方式 | **离线路径** |
| 与 daily dashboard / 其它配置的可比性 | **不考虑** |
| 时间预算 | **不设上限** |
| 取消 / 暂停 / 驱逐 | **推理系统的必备能力** |

### 0.2 调研推翻的四个原假设

这一版是在读完代码后重写的。原方案里有四条判断是错的，必须先讲清楚，因为它们决定了后面所有排序：

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

---

## 1. 测试范围是怎么定下来的

这一节回答"为什么测这几条、为什么不测那几条"。§2 之后的每一批都从这里拿依据。

### 1.1 现有覆盖到哪为止

四次长跑（R0/R1/R1b 钉在 `a94830c8`，R1-427 钉在 `74c4a9e`，合计 3.25 小时、207,606 个请求、零硬故障）
覆盖的**只是会话生命周期的正常路径**：请求提交 → 请求完成 → `finally: close()` → `release_gr_session`。

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
| **取消时仍有在飞 dispatch** | abort 里那句 `_drain_resources` 被 `not session._in_flight_dispatches` 挡住，释放托付给最后一个回执 | `gr_async_scheduler.py:622-631` | 批次 2（§3） |
| **暂停期间终态交付** | GR 把收尾计入 workload 才能不空闲；`_discard_pending_native_finish` 的 discard 分支是 #427 新增 | `engine_core_patch.py:449-453, 719-727` | 批次 3（§4） |
| **同 session_id 复用导致迟到 / 重复回执** | 事件会发生（ST 刻意用 `"same-public-id"` 反复请求），影响被三层阻断 | `:498-502`（owner_generation + dispatch_id）、`engine_core_patch.py` 的 `dispatched_sessions` 过滤、`:512-513` 对象同一性 | 批次 2、4（§3、§5） |
| **会话释放窗口内 KV block 仍被 worker 引用** | lease 机制存在的理由；窗口真实存在 | `:384-386` 注释 + `:508-510` 释放 | 判据，§7 |

**已有单测覆盖什么**（长稳的增量是"长时间反复跑 + 组合 + 不漂移"，不是补语义）：

- `tests/test_gr_async_scheduler.py:751 test_native_kv_dispatch_lease_survives_abort_until_receipt`
- `:787 test_dispatch_lease_preserves_native_eviction_order` —— 5 维参数化，断言 free list 顺序与原生 `free()` 完全一致
- `tests/st/test_onerec_v1_scheduler_st.py:401-430` —— 两种时机的取消 + 取消后引擎可复用
- `tests/test_async_scheduling_scheduler.py:407` —— 旧 generation 回执重复投递，断言 native 计数不变

ST 测试的覆盖清单说明作者认为什么重要：N=1/2/3 与 prefix hit/miss 的等价性、
公共结果必须晚于 worker 释放（`:262`）、worker 侧 owner/session/workspace 三段释放归零、
两种取消时机、graph replay 与 eager 双路径、KV block 边界 1023/1024/1025 与 graph bucket 边界。

### 1.4 判据对着哪个语义定：#427 前后的差异

**这一节只回答一个问题：写判据时，期望行为该以哪个版本为准。**
答案是 `74c4a9e` —— 因为基线在几条目标路径上的行为是明确的 bug，照它写判据会把 bug 判成正确。

#### 1.4.1 基线存在性对照

`git show a94830c8:vllm_gr/v1/engine/{beam_search_v1,gr_async_scheduler}.py`：

| 符号 | `a94830c8` | `74c4a9e` 的变化 |
|---|---|---|
| `abort_gr_session` | `:632-641` 存在 | **函数体逐字相同**（仅 import 空行差） |
| `release_gr_session` | `:644-654` 存在 | 同上 |
| `_in_flight_dispatches` | `:382, 494, 497` | 相同语义 |
| `_kv_dispatch_leases` | `:378` | 构造方式改为单遍（性能） |
| `_pending_terminal_result` | `:529, 543, 576` | 相同语义 |
| `_gr_v1_sessions` 表 | `engine_core_patch.py` 5 处已读 | **表本身是旧的**，只是前几轮没探测 |
| `GRResourceStatus` 五态 | `beam_search_v1.py:38` | 相同 |
| `GRExecutionCapacity(active_sessions=1, dispatches_per_session=2)` | `:47-63` | 相同 |
| `has_gr_cleanup_work` | `gr_async_scheduler.py:62` | 相同 |
| native pause 钩子 | `tests/test_gr_pause_resume.py` 已存在 | 只改了 2 处断言 |
| `_dispatch_records` | **不存在** | **新增**（`beam_search_v1.py:209`） |

#### 1.4.2 #427 真正新增的东西

**一个字段** —— `GRSessionState._dispatch_records: dict[int, GRStageMetadata]`（`beam_search_v1.py:209-211`），
只增不删的 dispatch 账本。它替代了基线的「一组 host 侧标量 + 一个可变引用」：

| | 基线 | 74c4a9e |
|---|---|---|
| stage 身份 | `stage_index = session.issued_stages`（会被 drain 路径改写） | `num_computed_tokens - num_prompt_tokens`（native 进度当唯一真值） |
| 设备依赖链 | `session.latest_device_state` 是可变的、issue 时被覆盖 | 由 `_dispatch_records` 派生（`:244-248`） |
| 回执匹配 | 只查 `dispatch_id not in _in_flight_dispatches`（无对象同一性） | 对象同一性校验：`if session._dispatch_records.get(id) != metadata: raise`（`:512-513`） |
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
| 5 | **draining 分支移到回执校验之后** | `:511-525` | 基线在 `_read_worker_payload` **之前**处理 draining；现在畸形回执在 draining 状态下**算协议失败**，且 `_drain_resources` 每帧无条件调用 |
| 6 | **协议失败覆盖早先的成功终态** | `_fail_gr_dispatch` `:559-571` | 旧：无条件置 `FINISHING`（会覆盖 ABORTED/FAILED）。新：已取消/已失败的会话不被翻回；但**已缓冲的成功终态会被协议失败覆盖** |

配套（`engine_core_patch.py`，是 1/3 成立的前提）：`dispatched_sessions` 过滤加
`owner_generation` + `dispatch_id` 双重校验（"Stale results must not reach native token accounting,
even if a new owner reuses the ID"）；`_discard_pending_native_finish(..., worker_released=True)`
新增 `finished_req_ids.discard(...)`（§4.1 解释为什么这是 pause 能收敛的前提）。

**变化 1、2、3 合起来是同一个机制**：基线允许"会话已经摘掉了，但 worker 还拿着它的资源和 KV"。
取消路径与暂停路径都会撞上这个窗口，所以 §7 的租约不变量判据是这几条路径的共同下游。

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

**第二个必要性是 RSS 会采错进程。** vllm 0.22.1 默认 `VLLM_WORKER_MULTIPROC_METHOD="fork"`
（`envs.py:883-884`），**fork 出来的 EngineCoreProc 子进程 cmdline 与父进程逐字相同**，
而 `find_container_pid` 取 `docker top` 的**第一条** cmdline 匹配 —— 可能整个 5 小时都在采错进程，
且两次运行可能不同。这与 `soak_watchdog.py:320-330` 已记录过的事故是同一类。
不修的话，批次 3 的内存结论无效。

### 2.2 代码在哪

**探针的获取路径**（`run_soak.py`）：`locate_engine_core(:528-546)` → `scan_containers(:761-771)` →
`select_probes(:628)`；beam registry 用 BFS 靠 `vars()` 找同时含 `_slot_map` 与 `_free_slots` 的对象；
`_find_gr_sessions` 走 `resolve_path(engine_core, "scheduler._gr_v1_sessions")`（`:651-665`）。

**watchdog 的采集路径**：`read_proc(pid)`（`soak_watchdog.py:177-214`，单 pid，无子进程遍历）、
`find_container_pid(container, pattern)`（`:320-350`）。cgroup 只用于 CPU（`:267-290` 只拼 `cpu.stat`，
**没有 `memory.current`**）。唯一没被漏掉的显存口径是 `--query-compute-apps`（`:414-431, 479`），
它覆盖**所有** compute 进程的显存。

**跨进程查询的现有范式**：`vllm_gr/v1/engine/core.py:240-242` 的 `is_gr_session_retired`，
经 `engine_core_patch.py:191` 装到 `EngineCore`，前端用 `call_utility` 调它
（`gr_session_client.py:178-187` 已经在这么干）。新探针照这个范式加。

**driver 侧本来就有、不需要改引擎的等价物**（`SchedulerStats`）：EngineCore 子进程每步构造并推给 driver
（`core.py:1814-1817`），driver 侧 `llm_engine.logger_manager` 能拿到（`llm_engine.py:305, 318-319`）。
字段（`vllm/v1/metrics/stats.py:171-200`，`scheduler.py:1968-2000` 构造）：

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

**① Core 侧新增可查询的探针摘要。** 在 `EngineCore` 上加一个纯查询方法，返回可序列化的摘要：

```python
def gr_probe_snapshot(self):
    sessions = getattr(self.scheduler, "_gr_v1_sessions", {})
    return {
        "session_ids":      sorted(sessions),
        "terminal_pending": sum(s.terminal_result_pending for s in sessions.values()),
        "by_status":        Counter(s.resource_status.name for s in sessions.values()),
        "in_flight":        sum(len(s._in_flight_dispatches) for s in sessions.values()),
        "leases":           sum(len(s._kv_dispatch_leases) for s in sessions.values()),
        "dispatch_records": sum(len(s._dispatch_records) for s in sessions.values()),
        "free_blocks":      self.scheduler.kv_cache_manager.block_pool.get_num_free_blocks(),
    }
```

安装方式照抄 `is_gr_session_retired`（§2.2）。**这是本方案唯一需要改引擎代码的项，而且只加查询、不加逻辑。**
改动单独成一个提交，便于 review 时与 harness 改动分开看。

**② harness 分层派发两条口径。** `run_soak.py` 的探针层加一层分派：能直接拿到 `engine_core.scheduler`
就走对象图（现状），拿到 `SyncMPClient` 就走 `call_utility("gr_probe_snapshot")`。
**两边输出同一组字段名**，报告里直接并排。

**③ 报告区分「读到 0」与「读不到」。** 全部探针列都单开一栏 `null`，不与 0 混在一起。

**④ watchdog 修掉 fork 同名采错进程。** 优先用 cgroup 归属定位（`/proc/<pid>/cgroup` 匹配容器 id），
拿不到再退到 cmdline，并与 `--query-compute-apps`（`:414-431, 479`）交叉验证。

**顺带可选**：把 `SchedulerStats` 里的 `kv_cache_usage` 与 `num_preempted_reqs` 一起接进来 ——
driver 侧就能拿到，成本很低，而且是单进程口径下也没有的两个数。

**验证方式**：同一负载分别用 `VLLM_ENABLE_V1_MULTIPROCESSING=0/1` 各跑一次 30 min。

**判据**：多进程下 `session_ids` / `by_status` / `in_flight` / `leases` 有**非零**读数
（全 0 说明探针没接对，不是"干净"）；RSS 采到的是容器内的 EngineCoreProc 而不是 driver；
两种口径的 `free_blocks` 一致。

---

## 3. 批次 2：取消 soak（离线）

### 3.1 必要性

**取消是推理系统的必备能力**（§0.1），而现有四次长跑一次都没取消过请求。这条路径上有两个具体的风险：

**风险一：释放被**设计成**延迟的，而延迟没有上限。** `abort` 里有三处"不会立即释放"的情形
（§3.2 分支表），三者都需要**有人继续泵引擎**。而 `abort` 内部的泵循环
（`gr_session_client.py:182-187`）**无超时无上限** —— 只要退休证明或最后一个回执不来，
调用方**永久挂死**，而且引擎不会报错，`has_gr_cleanup_work` 还会让引擎一直"不空闲"，
外部看起来只是变慢。这是本批次最需要防的失败形态。

**风险二：取消路径不显式还 KV 租约。** `abort_gr_session` / `finish_gr_session` / `_close_gr_session` /
`release_gr_session` **都不碰** `_kv_dispatch_leases`，靠 `update_gr_from_output` 开头无条件 `pop` 兜底
（§7.1）。也就是说租约的归还**完全依赖回执到达** —— 回执不来，租约就不还，KV block 就永不释放。
取消路径正是"回执可能不来"的路径。

**先说清不测什么**：`gr_async_scheduler.py` 里那两个 `abort_gr_session` / `release_gr_session`
**是全仓无调用点的死代码**：

```
$ git grep -n "abort_gr_session\|release_gr_session" 74c4a9e -- vllm_gr/
gr_async_scheduler.py:622   def abort_gr_session(...)      ← 定义
gr_async_scheduler.py:653   scheduler_cls.abort_gr_session = abort_gr_session   ← 安装
gr_session_client.py:233    llm_engine_cls.abort_gr_session = abort_gr_session_sync  ← 另一套
```

调度器上那两个被 `:653-654` 装到类上，但全仓无调用点（含动态派发，`getattr(..., "abort_gr_session")`
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

`abort_gr_session`（`gr_async_scheduler.py:622-631`）的逐分支走向：

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

**第一个动作是标定跑，不能跳过。** K 决定撞哪个在途窗口，而 K 的正确取值取决于每个 stage 占多少步。
标定跑要打印：每个 stage 的起始 / 结束步号、prefill 与 decode 各占多少步、`is_last_stage` 落在第几步。
**没有标定就直接扫 K，等于在不知道靶子在哪的情况下开枪。** `K=0` 只测"未 dispatch 就 abort"的快速路径；
要撞「abort 时有在途 dispatch」必须让 K 落在 prefill / decode 在途窗口，**N=3 命中概率最高**。

**harness 要加的三样东西**：

| 改动 | 原因 | 具体做法 |
|---|---|---|
| abort 看门狗 | §3.1 风险一：泵循环无超时无上限，且引擎不报错 | `client.abort` 放子线程 + `join(timeout)`；超时后由独立线程采 `gr_probe_snapshot` 判断卡在哪一步 |
| 同线程驱动 | `engine.step()` 无锁 | K 步与 abort 必须在同一线程，见上面的代码 |
| 清理重试可观测 | `client.py:41-54` 的 `_run_sync_cleanup` 无限重试（退避 0.1→×2 封顶 5.0s，在调用线程），是静默挂死的另一种形态 | 记录每次调用返回耗时，超阈值告警。**注意版本差异**：`74c4a9e` 对 `EngineDeadError` 立即抛出，而**工作区副本没有这个分支**，会永久重试不返回 |

**七个子场景 × 各 1 h 循环**：

| # | 场景 | 打的是哪段代码 | 怎么构造 |
|---|---|---|---|
| 1 | prefill 在途时取消 | `_in_flight_dispatches` 含第 0 个 dispatch | K 落在 prefill 窗口内 |
| 2 | 中间 decode step 在途时取消 | 中间 dispatch | K 落在 decode 窗口中部 |
| 3 | 末段 decode 在途时取消 | 接近 `is_last_stage`，退役与 abort 竞争 | K 接近最后一个 stage |
| 4 | `submit_once` 返回之前取消 | `client.py:216-235` `_rollback_submission` 的 race | **不在本批范围** —— 离线同步路径上 `submit_once` 不返回就不会有 session 对象，需异步客户端 |
| 5 | 终态之后取消 | 应走 release 而非 abort（分流失效检测） | 正常跑完后调 `close` |
| 6 | 对同一 session 连续重复取消 | 幂等契约（`protocol.py:140-141`） | 重复 abort 同一 id |
| 7 | 取消 × 同 session_id 复用 | `owner_generation` + `_dispatch_records` 对象同一性 | 同一 id 反复提交 + 取消，制造迟到回执 |

（场景 4 的注释 `client.py:220-221`「Fence acceptance before aborting」确实标示了一个被识别的竞态，
但离线同步路径不可达，标为"不在本批范围"。）

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

**为什么必须这么做**：`scheduler.py:2278-2284` 的 `get_num_unfinished_requests()` 在 PAUSED_ALL 下
**返回 0**。于是「只保留了 GR 会话、没有可运行请求」的引擎会被判定为**空闲** ——
但那些会话还有收尾要跑（终态投递 + worker release proof + KV 释放）。
**GR 必须把它们算成 workload，否则 pause 的 Future 永不完成，引擎永久挂起。**
这就是本批次的核心风险，也解释了为什么"pause Future 能不能完成"是最主要的判据。

**反向的一半也是 #427 新增的**（`engine_core_patch.py:719-727`）：

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

**这段注释本身就是"暂停与 GR 收尾耦合"的直接证据**，也是 #427 唯一处理 pause 的地方。

**设计契约**见 `74c4a9e:docs/beam_stage_pr2.md:73-80`：

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

`PauseState` 与调度策略（`vllm/v1/core/sched/interface.py:23-37` @0.22.1）：

```python
class PauseState(enum.IntEnum):
    UNPAUSED = 0
    PAUSED_NEW = 1   # 不调度新请求，running 继续
    PAUSED_ALL = 2   # 不调度任何请求
```

`PauseMode = Literal["abort", "wait", "keep"]`（`vllm/v1/engine/__init__.py:26`）。

`EngineCore.pause_scheduler(mode, clear_cache)`（`core.py:821-850`）只改调度策略，**不拦截 `add_request`**
（新请求照样进 waiting，只是不被调度）。`mode="wait"` 在 inproc-engine 模式下被显式拒绝（`:839-840`）。

**暂停完成的判定是"引擎到达 idle"**：`EngineCoreProc.pause_scheduler`（`core.py:1764-1805`）
返回一个 `Future`，由 `_idle_state_callbacks` 在 `has_work()` 为假时 set_result。
`has_work() = engines_running or scheduler.has_requests() or bool(batch_queue)`（`core.py:1346-1352`）。
**GR 就是从 `scheduler.has_requests()` 这一个人口切进去的**（上面的 `@wraps` 补丁）。

### 4.3 怎么测

**离线可达性有三条路径，选第二条。** 断点在 `EngineCoreClient` 抽象基类：
`InprocClient`（`core_client.py:276-367`）、`SyncMPClient`（`:779-948`）**都没有 pause**，
只有在线用的 `AsyncMPClient:1130-1139` 有。`GRLLM` / `LLM` 也都没有
（`LLM` 只到 `sleep` / `wake_up`，`llm.py:796/821`）。

| 路径 | 评价 |
|---|---|
| `llm.sleep(level=0, mode="keep")` | 公开，且 `level=0` 时 `clear_prefix_cache = (level>=1) = False`，**恰好等于** `pause_scheduler(mode="keep", clear_cache=False)`（`core.py:875`）。但 SyncMPClient 下是阻塞黑盒，**拿不到 Future，而 Future 正是主要判据** |
| **`llm.llm_engine.engine_core.call_utility("pause_scheduler", "keep", False)`** | **推荐**。UTILITY 派发是泛化的（`core.py:1485-1498`、`:1534-1552`、`core_client.py:875-882`）。**GR 自己的前端已经在用同一范式**（`gr_session_client.py:178-187` 就是这么调 `is_gr_session_retired` 的） |
| `engine_core.engine_core.pause_scheduler(...)` | InprocClient 专用；恒返回 None，无 idle 回调，`mode="wait"` 直接 raise |

**结论：需要绕过公开接口直接操作 EngineCore，但不必在线。** 不必在线的原因：`pause_scheduler`
是**进程内同步方法**，完成条件就是本进程的 `has_work()` + idle 回调，与前端是不是 HTTP 无关。
唯一"在线独占"的是 `AsyncMPClient.pause_scheduler_async` 这个**封装**，不是能力。

**但必须开多进程。** `has_work()` / `_idle_state_callbacks` / `pause_scheduler` 返回 `Future` /
`_process_input_queue` —— **这一整套只存在于 `EngineCoreProc`**。InprocClient 的 step 路径是
`engine_core.step_fn()` + `post_step()`（`core_client.py:289-292`），**从不进入 `_process_input_queue`**，
所以 idle 回调链在 InprocClient 下根本不会被驱动。

→ **`VLLM_ENABLE_V1_MULTIPROCESSING=1` 是硬性前提**，也正是批次 1 必须排在前面的原因（§2.1）。

**驱动方式**：

```python
core = llm.llm_engine.engine_core
fut  = core.call_utility("pause_scheduler", "keep", False)   # 或 "abort"
# 等 fut 完成 —— 这个等待本身就是被测行为
core.call_utility("resume_scheduler")
```

**五个子场景 × 各 1 h**：

| # | 场景 | 打的是什么 |
|---|---|---|
| 1 | 会话在 `DRAINING` / `RETIRING` 时 pause，再 resume | `has_gr_cleanup_work` 的托付是否兑现 |
| 2 | 暂停窗口内发起取消（与 §3 组合） | 收尾能否在暂停状态下推进 |
| 3 | 暂停跨越多个会话的完整生命周期 | `finished_req_ids` 残留导致 pause 永不完成（§4.1 反向补丁） |
| 4 | `PAUSED_NEW` vs `PAUSED_ALL` 两种档位 | 两种 pause 策略下 GR 收尾的差异 |
| 5 | 反复 pause / resume N 次 | 累积漂移 |

**判据**：

- **`pause_scheduler` 的 Future 能在合理时间内完成 —— 这是最主要的判据。**
  不完成就是 §4.1 那个"GR 收尾不算 workload 就永不空闲"的 bug，而它表现为**永久挂起**，
  所以超时上限必须显式设，不能等。
- 恢复后 `_pending_terminal_result` 归零、`_gr_v1_sessions` 排空、`_in_flight_dispatches` 归零
- 无长期滞留的 `RETIRING`
- `has_requests()` 在清理完成后返回 False
- 暂停期间 `scheduled.total_num_scheduled_tokens == 0`（退休必须是零 token 控制帧）
- 恢复后引擎可复用（跑一轮正常请求，指纹一致）

---

## 5. 批次 4：组合轮

### 5.1 必要性

**单点跑通不蕴含组合跑通。** 批次 2 制造 DRAINING 状态、批次 3 在 DRAINING 上暂停，
两者对同一组字段的期望值可能不同。具体地，交集在 `_discard_pending_native_finish` 的
`worker_released=True` 分支（`engine_core_patch.py:719-727`）：它在 **PAUSED_ALL 且会话被融合释放**时
才生效，而这正是"暂停 + 取消"同时发生才构造得出来的状态 —— 批次 2 或批次 3 单独跑都不会进入它。

### 5.2 代码在哪

无新增代码，打的是批次 2 与批次 3 的代码在**同一时刻**的交互：
`engine_core_patch.py:719-727`（discard 分支）、`:376-410`（abort 后的会话同步）、
`gr_async_scheduler.py:498-502`（`owner_generation` + `dispatch_id` 双重校验）、
`:512-513`（`_dispatch_records` 对象同一性）。

### 5.3 怎么测

**场景矩阵**（挑交叉点，不做笛卡尔积）：

- 暂停期间取消一个已处于 DRAINING 的会话
- 取消后**立刻**用同一个 `session_id` 提交新请求（跨 `owner_generation`，与 §3 场景 7 走同一段代码）
- 上述动作夹在 prefix hit / miss 交替的负载里（保持与现有 soak 相同的请求形状）
- 时间上覆盖多个 pause / resume 周期

**规模**：连续 4~8 h。

**判据**：批次 2 与批次 3 的判据**同时**成立，且 `gr_probe_snapshot` 的每一个计数在整轮里
都不出现单调上升（复用现有报告工具的 Mann-Kendall + 斜率置信区间 + 幅度三条件）。

---

## 6. 批次 5：全量 5 h 基线复跑（新口径）

### 6.1 必要性

**目的不是再证一次"没泄漏"，而是确认新口径没有引入观测偏差。**
批次 1 换了探针（新增 Core 侧查询、改 watchdog 的进程定位），而**探针本身不能改变被测量的东西** ——
现有 harness 用 `vars()` 而不是 `getattr()` 就是为这个（`getattr()` 会触发 property 描述符）。
所以口径变更之后必须有一次可对照的运行，证明结论不是观测方式变出来的。

### 6.2 怎么测

用批次 1 的双口径探针跑一次与 R2 同规格的 5 h，与 R1-427 的结论逐节对照。
避开 03:00–05:30（crontab 的 `sync_scripts.sh` 与 `daily_runner.sh` 会抢 GPU 和仓库），
其余流程沿用 `tools/soak/README.md`。

**判据**：延迟分布与内存形状与 R1-427 一致；无新增的持续上升序列；`null` 栏为空或已解释。

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
`_preempt_request` 会立刻 `kv_cache_manager.free(request)`（`scheduler.py:938`），把 ref_cnt 降到 0
并放回 free queue，**而 worker 可能还在读这块 KV**。所以必须由 scheduler 自己额外 `touch` 一次
把 ref_cnt 顶成 2，再用 lease 在 `update_gr_from_output:508-510` 减回去。

释放只在回执到达时发生（`:508-510` 一次 `free_blocks`）。**取消路径不显式还租约** ——
`abort_gr_session` / `finish_gr_session` / `_close_gr_session` / `release_gr_session` 都不碰它，
靠 `update_gr_from_output` 开头无条件 `pop` 兜底。全仓只有这两处引用 `_kv_dispatch_leases`。
这就是为什么它是批次 2、3、4 的**共同下游**：这三批都会制造"回执可能不来"的时刻。

**为什么这一维度是判据而不是压力源。** 原计划想造驱逐压力来测这条路径，读了代码之后发现**造不出来**，
而且方向是反的。三处：

**修正一：前缀缓存不消耗可用块。** `get_num_free_blocks()` 统计的是 free queue，
而 **cached block 就在 free queue 里**（`block_pool.py:44-47`、`:166` 的 docstring
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

另外两点：`PreemptionMode` 在 vllm 0.22.1 **已不存在**（只有文档里一句旧日志样例），
该版本只有 recompute 一种抢占，唯一成因是 `allocate_slots()` 返回 None（`scheduler.py:483`）；
且 `run_soak.py:224` 在 v1 下设 `max_num_seqs=1`，**连并发都没有**。

**结论**：真正的驱逐 / 抢占路径要覆盖，只能换 legacy `beam_search` API
（那里 `max_num_seqs = max(128, beam_width)`，有真并发，但**不走 #427 的 lease 机制**，
测不到我们要测的东西）。留作独立议题另行评估。本方案聚焦**不变量断言**。

### 7.2 代码在哪

| 环节 | 位置 |
|---|---|
| 建立租约 | `gr_async_scheduler.py:388-394`（`touch` + 记账） |
| 释放租约 | `:508-510`（`update_gr_from_output` 里唯一的 `free_blocks`） |
| 兜底 pop | `update_gr_from_output` 开头无条件 pop |
| 泄漏的硬症状 | `:137-148`、`:146-148` 的 `RuntimeError("...lost reserved KV capacity after Beam generation")` |
| 布尔 oracle | `vllm/v1/core/block_pool.py:463-471`（@0.22.1） |

### 7.3 怎么测

**`reset_prefix_cache()` 是本 harness 里唯一二值、且最灵敏的泄漏判据。**

```python
# vllm/v1/core/block_pool.py:463-471 (@0.22.1)
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
2. 每次取消 / 暂停动作之后**立即**调一次，而不是等到下一轮
3. 配合 `block_pool.get_num_free_blocks()` 回到基线，以及 `_kv_dispatch_leases` 归零的**时序**
   （是"回执到达时归零"还是"abort 时归零"—— 后者说明兜底 pop 提前跑了，违反窗口保证）

**泄漏的后果链**（用来判断"变慢了"到底是不是这条链）：

1. block `ref_cnt` 永远 ≥1 → 永不回 free queue → `get_num_free_blocks()` 单调下降、`kv_cache_manager.usage` 单调上升
2. 超过准入闸的静态余量后，`allocate_slots` 返回 None。此时**不是**普通抢占：
   - 已发过 stage → 硬错 `RuntimeError("Asynchronous scheduling lost reserved KV capacity after Beam generation")`（`:137-148`）
   - `issued_stages == 0` → 落到原生 `scheduler.py:483` 自我抢占（`preempted_req == request` → break），
     下一轮重新 prefill，形成 **livelock**
3. `reset_prefix_cache()` 返回 False（最早、最二值的信号）

---

## 8. 已知盲区

- **Xid 通道**：宿主机非 root 读不了 dmesg（`/dev/kmsg`、`journalctl -k` 同样受限），
  现有四次运行对驱动级故障是盲区。需外部捕获内核日志后经 `--xid-log` 引入，或明确接受。
- **多进程下的对象图探针**：见 §2.1。批次 1 之前，MP 下这些探针会静默变成 `null`。
- **多进程下的 RSS**：见 §2.1，可能采错进程。**批次 1 必须解决**，否则批次 3 的内存结论无效。
- **真正的驱逐 / 抢占路径测不到**：见 §7.1。要覆盖只能换 legacy API，那是另一个议题。
- **`submit_once` 返回前取消（§3.3 场景 4）**：离线同步路径不可达，需异步客户端。
- **`tools/soak/` 整个目录未被 git 跟踪**：harness 自身没有版本管理，
  所有 harness 改动都不会进 code review。`SOAK_REV` 只能证明被测的引擎代码，不能证明 harness。
- **工作区副本与 74c4a9e 的差异**：`_run_sync_cleanup` 在 74c4a9e 对 `EngineDeadError` 立即抛出，
  工作区副本没有这个分支（§3.3）。harness 与引擎的版本对应关系要显式记录。

---

## 9. 附录：代码位置索引

| 主题 | 位置 |
|---|---|
| 离线取消链路 | `gr_session_client.py:177-192`、`engine_core_patch.py:376-410, 495-498` |
| abort 逐分支 | `gr_async_scheduler.py:622-644` |
| 六处语义变化 | `gr_async_scheduler.py:163-170, 183-188, 224-228, 345-358, 511-525, 559-571` |
| 租约建立 / 释放 | `gr_async_scheduler.py:388-394`、`:508-510` |
| 准入闸 | `gr_async_scheduler.py:251-296`、`beam_search_v1.py:114-177` |
| pause 定义 | `vllm/v1/core/sched/interface.py:23-37`、`vllm/v1/engine/core.py:821-850, 1764-1805` |
| GR pause 钩子 | `engine_core_patch.py:449-453, 719-727`、`gr_async_scheduler.py:64-73` |
| pause 契约文档 | `74c4a9e:docs/beam_stage_pr2.md:73-80` |
| 既有单测 | `tests/test_gr_pause_resume.py`、`tests/test_gr_async_scheduler.py:751/:787`、`tests/st/test_onerec_v1_scheduler_st.py:401-430` |
| 探针实现 | `run_soak.py:528-546, 628, 651-665, 735-771`、`soak_watchdog.py:177-214, 320-350` |
| 布尔 oracle | `vllm/v1/core/block_pool.py:463-471`、`tools/soak/run_soak.py:1075-1076` |
