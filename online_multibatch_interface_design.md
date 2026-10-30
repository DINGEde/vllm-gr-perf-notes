# 在线多输入 Beam 推理接口设计与分阶段交付说明

- **文档用途**：供接口、推理引擎、测试及运维相关人员进行设计与代码评审。
- **核对日期**：2026-10-19（Asia/Shanghai）。
- **适用范围**：vllm-gr V1 Beam Search 的非流式 `POST /v1/completions` 路径；上游接口兼容性以当前使用的 vLLM 0.22.1 为基准。

本文区分三类信息：代码实现所规定的行为、PR 中报告的验证结果，以及尚未完成的验收事项。PR 状态和提交号是核对日期的快照；后续代码更新应同步修订本文。

## 1. 设计摘要

本功能将同一 HTTP 请求中的多个输入组织为引擎能够执行的固定批次，并在需要时串行执行多个批次，最终返回一个完整响应。功能分为两个有依赖关系的交付阶段：

- **PR1：固定批次在线基础支持。** 完成在线请求协议、输入输出处理和 HTTP 入口适配，使满足条件的 2 条或 4 条输入以一个 Beam session 执行。
- **PR2：任意长度输入列表的拆批与归并。** 在 PR1 基础上，将输入列表规划为连续的 4/2/1 子批次，逐批执行并按原始输入顺序归并结果。

两个 PR 均保留 `GRExecutionCapacity.active_sessions = 1`，不修改 Scheduler、Worker 或 CUDA 执行逻辑，不增加 endpoint、请求字段或响应字段。目标是复用现有批执行能力，而不是引入多 session 并行、跨 HTTP 请求合批或连续批处理。

当前验证能够支持接口路径、批次划分和部分生命周期行为的评审，但不能据此认定跨 batch 大小的模型输出等价，也不能认定性能与生产质量已经完成验收。

## 2. PR 状态、依赖与变更边界

### 2.1 当前版本

| 项目 | PR1 | PR2 |
| --- | --- | --- |
| PR | [JiusiServe/vllm-gr#461][pr1] | [DINGEde/vllm-gr#1][pr2] |
| 标题 | `feat(openai): support fixed-size online beam batches` | `feat(openai): split and merge arbitrary prompt lists` |
| 状态 | Open，非 Draft，尚未合入 | Open，Draft |
| Head 分支 | `DINGEde:feat/online-multi-prompt` | `DINGEde:feat/online-prompt-list-split` |
| Head commit | `e8fe3dd520e4fca5dd63972b99f9182a25365f77` | `057b664252634bc3ff00b59e6a909e6d25a1bf44` |
| Base 分支 | `JiusiServe:decode_graph` | `DINGEde:feat/online-fixed-batch` |
| GitHub diff | 15 个文件，+1243 / -81 | 6 个文件，+660 / -256 |

PR1 的上游 base 快照为 `5c9495d19f39591dc6b86a97c0290e8773fc533e`。PR2 的 base commit 与 PR1 当前 head 均为 `e8fe3dd`；因此，`feat/online-fixed-batch` 在此处是 PR1 内容的基线分支，而不是第三项独立功能。

按“新增行数＋删除行数”统计，PR1 为 1324 行，PR2 为 916 行。PR2 已低于此前约定的 1000 行目标，PR1 尚未达到该目标，不应将“已拆分”表述为“两个 PR 均已满足规模要求”。

### 2.2 功能依赖

```text
上游 decode_graph
    |
    +-- PR1：在线多输入协议及固定批次 HTTP 支持
            |
            +-- PR2：HTTP 内连续拆批、串行执行及结果归并
```

PR1 可以独立运行和验证，不依赖 PR2。PR2 依赖 PR1 提供的多输入在线调用能力，其 diff 不应再次包含 PR1 的协议和输入输出适配。

| 代码范围 | PR1 的职责 | PR2 的职责 |
| --- | --- | --- |
| `entrypoints/beam_search_v1/protocol.py` | 将在线输入字段统一为 `prompts` 元组 | 不再修改 |
| `entrypoints/beam_search_v1/input_processor.py` | 为批内各输入构造 `EngineCoreRequest`，形成批请求 | 不再修改 |
| `entrypoints/beam_search_v1/output_processor.py` | 按 `item_index` 还原各输入的输出 | 不再修改 |
| `entrypoints/openai/serving_engine.py` | 使在线 `beam_search_v1` 接受单输入或输入序列 | 不再修改 |
| `entrypoints/openai/beam_search_patch.py`、`patch.py` | 安装并校验 HTTP 批处理适配 | 复用已有注册 |
| `entrypoints/openai/completion_batch_patch.py` | 固定批次的选择、执行、回退与响应转换 | 扩展为规划后串行执行及跨批次归并 |
| `entrypoints/openai/completion_batch_plan.py` | 不包含 | 新增无 torch/vLLM 依赖的规划器 |
| 单元测试、既有 scheduler ST、quickstart | 验证和说明固定批次能力 | 增量验证和说明拆批及生命周期行为 |

表内生产代码路径均以 `vllm_gr/` 为前缀。已核对的两个 PR diff 不包含 `vllm_gr/v1/` 或 `csrc/` 的执行实现变更。对应实现见 [PR1 HTTP 适配][pr1-http]、[PR2 HTTP 适配][pr2-http]和[规划器][planner]。

### 2.3 PR2 位于 fork 的原因及后续流程

当前上游仓库不存在可供 PR2 使用的 PR1 基线分支。若直接以尚未合入 PR1 的上游 `decode_graph` 为 base，PR2 的比较范围将包含 PR1 的变更。以 fork 内的 `feat/online-fixed-batch` 为 base，可以在草稿中独立展示和评审第二阶段增量。

后续提交流程如下：

1. PR1 发生修订时，同步确认 PR2 所依据的基线，避免草稿继续基于过期的 PR1。
2. PR1 合入后，获取最新上游 `decode_graph`，识别并保留仅属于 PR2 的增量。
3. 若 PR1 被 squash 或 rebase 合入，应显式使用原 PR1 边界移植 PR2 增量，避免再次引入 PR1 提交；若原提交祖先关系被保留，也应检查实际 diff 后再决定是否需要 rebase。
4. 在上游基线上重新执行相关测试，确认上游 PR 只包含第二阶段变更。
5. **在 JiusiServe/vllm-gr 新建对应的上游 PR**，引用当前 fork 草稿和 #461。fork PR 不能通过修改 base 跨仓库转换为同一个上游 PR；新 PR 将具有独立编号和评审记录。
6. 验证及评审条件满足后，将上游 PR 标记为 Ready for review；fork 草稿保留交叉引用，并按团队流程标记为已被替代或关闭。

上述流程是后续发布安排，并非本文修订已经执行的操作。

## 3. 术语与问题定义

### 3.1 四类边界

| 术语 | 本文定义 |
| --- | --- |
| HTTP 请求 | 对外接口与响应边界，可以携带一个或多个逻辑输入 |
| 逻辑输入数 `k` | 经 `prompt_to_seq` 解释后得到的输入数量；`list[int]` 是一个 tokenized prompt，不代表多条输入 |
| 执行 batch 大小 `B` | 一个 Beam session 内固定的输入成员数量；当前支持 `B ∈ {1, 2, 4}` |
| Beam width `n` | 每个输入的 beam 宽度，不等同于 batch 大小 |
| 单 owner | 同一时刻只有一个 session 持有执行所有权，不等同于系统只能接收或排队一个请求 |

一个 session 包含 Prefill 和后续 Decode 阶段。将多个输入放入同一 session，表示它们进入同一批执行路径，**不表示整个请求只发生一次模型 forward 或一次 kernel 调用**。

### 3.2 上游接口与本项目约束

在 vLLM 0.22.1 的 completions 路径中，渲染后的输入由 `_create_completion` 逐个处理，并在非流式路径汇总为一个响应。对于本项目接管的 Beam 入口，这意味着逐输入调用 `self.beam_search`。

一般推理引擎可以在调度层组织多个独立请求，执行 batch 无需与某个 HTTP 列表完全一致。但在当前 vllm-gr V1 Beam 路径中，单 owner 和固定 session 成员契约并不会自动把不同 Beam session 的输入合成一个批次。因此，仅提交 prompt 列表不能保证利用既有的 B=2/4 执行能力。

`active_sessions = 1` 限制的是执行所有权，而不是所有在途请求的数量。多个请求可以处于排队或其他非执行状态；跨请求组批也不是理论上不可能，只是本次改造不实现该调度层能力。

本方案选择在同一 HTTP 内、session 提交前完成组批。该边界不需要重构执行所有权或运行中的 session 成员管理。

### 3.3 适用范围与非目标

本方案仅接管多输入、非流式、V1 Beam completions。非 Beam、streaming、`prompt_embeds`、非 V1 引擎及单输入请求继续由原 handler 处理。“委托原 handler”不代表这些请求必然成功，其合法性仍由原接口校验。

本方案不实现跨 HTTP 合批、连续批处理、多 session 并行、全局最优装箱或流式 Beam 输出。既有 Chunked Prefill、并行拓扑、模型类型、beam width 和生成阶段限制保持不变。

“任意长度列表”是输入条数层面的扩展，并非无限请求大小或任意模型参数支持；每条输入仍须满足模型、调度和引擎约束。

## 4. PR1：固定批次在线基础支持

### 4.1 输入输出适配

PR1 将在线请求的单输入字段改为 `OnlineBeamSearchRequest.prompts`，使用元组固定成员集合。输入处理器为各成员构造独立的 `EngineCoreRequest`，随后生成一个批请求；输出处理器通过 `item_index` 关联原始输入、token 边界和各 beam 输出。

`beam_search_v1` 的多输入在线接口是 PR1 引入的适配能力，不应与 PR1 之前已有的底层批执行能力混为一谈。

### 4.2 HTTP 路由流程

适配通过替换 `OpenAIServingCompletion._create_completion` 安装，保留安装时捕获的原函数用于委托，并保留上游调用层的清理包装。安装使用 marker 保证幂等；启动校验检查 dispatch 是否已安装、所调用的上游方法是否存在。这些结构检查不能替代上游升级后的签名与行为兼容性测试。

1. 检查是否属于本适配范围。不属于范围时，在本补丁调用渲染器之前委托原 handler。
2. 检查输入数量是否为 2 或 4。其他数量通过 `_decline` 记录原因并委托原 handler。
3. 调用 `render_completion_request`；若返回错误，则直接返回。渲染数量与预期不一致时返回服务端错误。
4. 检查 `max_num_seqs`、逐输入有效生成预算是否一致，以及包含 begin token 的总 Prefill 长度是否满足有效调度预算。
5. 满足条件时，调用一次 `beam_search(prompt=engine_inputs, ...)`；完整消费并关闭生成器，校验输出数量。
6. 调用一次 `request_output_to_completion_response`，返回原接口格式的响应。

**回退不全部发生在渲染前。** 当前 PR1 的序列容量、有效生成预算和 Prefill token 预算检查均在本补丁渲染之后。此时委托原 handler，原 handler 可能再次渲染。只有适用范围排除及不支持的固定批次数量，在本补丁渲染之前回退。实现依据见 [PR1 dispatch][pr1-http]。

### 4.3 生成预算与容量

各输入的有效生成预算由上游 `get_max_tokens` 按模型限制、请求参数及服务端默认值解析。批内预算必须一致，不能简单取所有预算的最小值。

容量检查针对完整固定批次，包括 `max_num_seqs`、`max_num_batched_tokens`，以及存在时更小的 `max_num_scheduled_tokens`。不满足时回退，不因“无法合批”新增数量类错误；原路径仍可能因输入本身不合法或资源不足而失败。

## 5. PR2：连续拆批与结果归并

### 5.1 执行流程

```text
一个 HTTP 请求，包含 k 条逻辑输入
    -> 渲染全部输入一次
    -> 解析逐输入预算，校验规划所需条件
    -> 生成完整子批次计划及 Beam 参数
    -> 子批次 0：执行、消费输出、关闭迭代器
    -> 子批次 1：执行、消费输出、关闭迭代器
    -> ...
    -> 按输入顺序汇总结果
    -> 一次响应转换，返回一个 CompletionResponse
```

PR2 不再因列表数量、整体容量或预算差异回退。即使最终全部拆为单例，也在当前路径执行，避免由回退引起的再次渲染。

### 5.2 规划规则

令 `l_i` 为第 i 条输入的渲染后长度，`g_i` 为其通过 `get_max_tokens` 解析的有效生成预算。规划使用的 Prefill 长度为 `L_i = l_i + r`：

- 当 `vllm_xargs.begin_token is not None` 时，`r = 1`；否则为 0。此处不是对 begin token 字符串进行真值判断。
- `g_i` 按现有接口使用渲染长度解析；附加 begin token 的长度用于 Prefill 容量检查。
- `S = max_num_seqs`。
- 若配置 `max_num_scheduled_tokens`，则 `T = min(max_num_batched_tokens, max_num_scheduled_tokens)`；否则 `T = max_num_batched_tokens`。

规划器从当前位置依次尝试降序排列的支持尺寸，选择满足以下条件的最大连续分段：

1. `B ∈ {4, 2, 1}`，且 `B <= S`，分段不越过输入列表末尾。
2. 分段内 `Σ L_i <= T`。
3. 分段内所有 `g_i` 相等。

开始规划前检查所有单例的长度和生成预算。若某条输入的 `L_i` 不在 `[1, T]` 内，或生成预算非正，则在提交任何子 session 之前报错。若循环无法推进，抛出 `RuntimeError`，避免内部不变量失效导致死循环。

这是保持输入顺序的连续贪心策略，不进行重排、等待凑批或全局装箱优化。规划只校验其能够判定的条件；它不预留 KV 空间，也不能保证后续引擎资源准入或执行一定成功。

### 5.3 典型划分

下表假设每条输入合法、批内有效生成预算相同，且容量充足；容量或预算不同可能进一步拆分。

| 输入数 k | 子批次大小 | session 数 |
| --- | --- | --- |
| 1 | 原单输入路径 | 1 |
| 2 | 2 | 1 |
| 3 | 2 + 1 | 2 |
| 4 | 4 | 1 |
| 5 | 4 + 1 | 2 |
| 6 | 4 + 2 | 2 |
| 7 | 4 + 2 + 1 | 3 |
| 9 | 4 + 4 + 1 | 3 |

当 `max_num_seqs = 1` 时，多输入计划由单例组成；当相邻生成预算不同且无法形成兼容分段时，也可能全部使用单例。两者均属于合法计划，而非输入截断。

### 5.4 所有权与标识

子批次的成员在提交前固定。同一 HTTP 内，前一生成器消费与关闭成功后，才进入下一次提交；子批次之间通过 `await asyncio.sleep(0)` 提供取消检查点。生成器关闭调用沿用现有前端的 session 清理机制；清理异常时不得继续提交后续批次。

仅一个子批次时保留父请求 ID；多个子批次使用 `{request_id}-batch-{i}`。最终响应仍使用父请求 ID。

父 HTTP 不在所有子批次之间独占 owner，其他请求可能获得执行机会。因此，本方案不提供请求级事务隔离，也不新增跨请求的公平性或延迟保证。

## 6. 接口行为与错误契约

| 条件或事件 | PR1 | PR2 |
| --- | --- | --- |
| 满足所有约束的 2/4 条输入 | 一个固定 session | 一个固定 session |
| 其他多输入数量 | 记录原因，委托原 handler | 连续拆为合法的 4/2/1 |
| 整体序列或 Prefill 容量不足，但单例合法 | 记录原因，委托原 handler | 使用更小子批次 |
| 相邻有效生成预算不同 | 记录原因，委托原 handler | 分开执行，保留各自预算 |
| 单条输入超出规划所用调度预算 | 沿用原路径校验 | 首个子批次提交前报 `VLLMValidationError` |
| 渲染数量或执行结果数量不一致 | 服务端错误 | 服务端错误，并停止后续子批次 |
| 执行或清理失败 | 不重试，不返回正常结果 | 停止后续提交，不重放已完成部分 |
| 执行期间取消 | 关闭当前迭代器 | 关闭当前迭代器并停止后续提交 |

单输入及不属于适配范围的请求仍委托原 handler；表内 PR2 的规划期错误语义仅适用于被接管的多输入路径。

### 6.1 响应语义

所有子批次成功后，按照原始输入顺序汇总 `RequestOutput`，交由现有 response converter 生成响应。正常 Beam 返回约定下，k 条输入、每条 n 个 beam 对应 `k × n` 个 prompt-major 扁平 choices，索引连续，usage 覆盖整单。

“一个响应对象”或“choice 数量正确”不足以证明发生了批执行。真实 session 数和 batch 大小需要通过执行探针或其他运行观测确认。

### 6.2 失败与取消

本接口以完整结果作为正常响应的返回条件，不提供部分成功响应，但不具备事务回滚能力。已完成子批次的计算和缓存影响不会因后续失败而撤销，前端也不会自动重放已完成工作。

在已接管的执行段内，`CancelledError` 按 vLLM 0.22.1 非流式 completions 的约定转换为 `Client disconnected` 错误响应，而非重新抛出。这是 HTTP adapter 边界的兼容行为，并非底层取消传播方式的修改，也不是成功响应。

断连监听仍由上游 `with_cancellation` 负责。本适配不增加第二个 ASGI receive 消费者。当前验证不能替代真实 TCP 断连测试。

## 7. 可观测性

| 路径 | 日志格式 | 含义 |
| --- | --- | --- |
| PR1 固定批次 | `Beam batch session request_id=... prompts=...` | 即将提交的固定批次 |
| PR1 无法形成固定批次 | `Beam batch declined request_id=... prompts=... reason=...` | 数量、容量或预算导致的回退 |
| PR2 已生成合法计划 | `Beam batch plan request_id=... prompts=... sub_batches=... sizes=...` | 子批次计划，包括全单例计划 |

这些新增日志不记录 prompt 内容，不改变响应 schema，也不代表执行已经成功。PR1 在通用适用范围检查中直接委托的请求不会输出 `declined` 日志；不能将其描述为“每一种委托都有日志”。PR2 规划失败或参数校验失败时也不保证输出计划日志。

两版实现均使用 `vllm_gr.logger.init_logger`，将模块 logger 放入 `vllm.` 命名空间，以复用 vLLM 的日志配置。实际 INFO 可见性仍取决于部署中的日志级别、handler 和过滤配置。不能把某个容器的日志配置观察推广为所有部署环境的必然行为。实现依据见 [logger helper][logger]。

## 8. 验证证据与结论边界

### 8.1 已报告的执行结果

以下数量与耗时来自核对时的两个 PR 正文。本文修订核对了代码和测试断言，**未重新执行测试，也未重新审计全部原始运行日志**。结果应与对应运行的 commit、模型、Constraint Table 及配置共同归档。

报告环境：服务器 10018、Python 3.12.13、torch 2.11.0+cu130、vLLM 0.22.1、NVIDIA L20、OneRec-1.7B BF16、beam width 128、生成的测试 Constraint Table。

| 验证项 | PR1 报告 | PR2 报告 |
| --- | --- | --- |
| 单元测试 | 全部受版本控制的单元测试：4008 passed、1388 skipped、0 failed；包含 32 个 completions-batch 测试 | 71 个相关单元测试通过，14.10 s |
| GPU 系统测试 | 3 个通过，454.39 s；包含固定批次 HTTP 用例 | 1 个拆批 HTTP 用例通过，152.13 s |
| 静态检查 | Ruff、diff 检查及 pinned mypy 通过 | Ruff 和 diff 检查通过 |

PR1 的 32 个测试包含在 4008 个通过结果中，不能相加；两个 PR 的测试集也存在继承和重叠，不能据此计算独立覆盖总数。Skipped 不等于验证通过。GPU 墙钟时间包含引擎启动和编译，不是请求延迟。

### 8.2 当前 GPU 测试实际证明的内容

[PR1 系统测试][pr1-st]通过官方 completions ASGI 路由提交 2 条和 4 条输入，断言 choice 数、索引、usage，以及阶段探针记录的 batch 大小和唯一 session 数；随后检查 retired、registry、active sessions 和 output processor 状态。该 HTTP 批处理用例本身不包含逐条 B=1 或同形状手动输出对照。

[PR2 系统测试][pr2-st]增加 2/3/4/5/6 条输入，使用实际 stage 事件验证 `[2]`、`[2,1]`、`[4]`、`[4,1]`、`[4,2]`。其输出对照是：

```text
A：一次 HTTP 提交 6 条输入，自动形成 4+2
B：先提交前 4 条，再提交后 2 条，合并两次 HTTP 的 choices
断言：A 与 B 的 choice.text 序列相等
```

两侧在对照开始前重置 prefix cache。该对照用于检验此测试数据和配置下的拆批归并文本一致性，**不是 6 次 B=1 调用，不是直接调用底层 beam_search，也未断言 token IDs 或累计分数相等**。旧分支中的其他实验不能代替当前测试分支的断言。

### 8.3 取消与资源释放的证据边界

PR2 的 GPU 用例启动 ASGI 请求后让出一次事件循环并取消任务，随后等待引擎状态收敛，再验证后续请求可完成。取消可能发生在 session 提交之前，也可能发生在执行期间，因此不能确定中断的是第几个子批次。

证据 JSON 中取消行的 `sessions=0` 和 `sizes=[]` 是测试显式写入的记录值，不是该次取消对应的 stage 统计，不能用来证明“没有任何 session 执行”。第二批失败/取消后不得提交第三批的确定性行为，应依据相应单元测试，而不是该 GPU 取消记录。

清理断言覆盖已检查的状态字段，不构成对所有 GPU 分配、workspace 或进程级资源均无泄漏的证明。报告中的 semaphore resource-tracker 退出警告仍需独立定位。

### 8.4 跨 batch 大小的模型质量

早期对照已报告 B=2/4 与独立 B=1 调用在候选、排序和分数上的差异，根因尚未确定。该风险首先属于 PR1 引入的在线合批能力，不能推迟到 PR2 才处理。

同形状分组的文本一致性，只能支持对应路径的实现正确性，不能证明跨 batch 大小的数值等价，也不能代替业务质量验收。仍需使用代表性输入和生产 Constraint Table 检查 token 序列、候选集合、排序、分数及业务指标。

若业务要求与逐条 B=1 严格一致，当前证据尚不满足该条件。不得将差异未经验证地归因为无害浮点误差，也不得仅因底层直接调用可复现差异，就认定上线风险不存在。

### 8.5 性能结论

历史定长实现曾记录同一四输入 HTTP 工作负载的 p50 从 237.6 ms 变为 210.9 ms，但并非当前两个 PR head 的重测结果。该数据仅作为历史参考，不列为当前性能验收依据。

当前缺少新版本的端到端延迟、吞吐、P95/P99、多客户端负载及长期稳定性结果。不依据历史数据进行未经验证的实例漂移校正或 HTTP/序列化耗时归因。

## 9. 团队评审与后续验收

| 评审方向 | 必查内容 | 适用阶段 |
| --- | --- | --- |
| API 兼容性 | 非目标路径委托、tokenized prompt 的计数、回退阶段、响应索引和 usage | PR1、PR2 |
| 引擎接口 | 在线输入输出关联、固定成员、单 owner、容量与 begin token 计量 | PR1；PR2 验证继承约束 |
| 规划与生命周期 | 连续划分、逐输入预算、提交前校验、关闭顺序、异常后停止提交 | PR2 |
| 模型质量 | B=1 与 B=2/4 的差异根因及业务接受标准 | PR1 必须单独评审，PR2 继承结论 |
| 运维与可靠性 | 部署日志可见性、真实 socket 断连、取消恢复、退出警告 | PR1、PR2 |
| 性能 | 当前 commit 的延迟、吞吐、尾延迟和压力测试 | PR1、PR2 |
| 发布与差异范围 | PR2 基线同步；上游 PR 只包含第二阶段增量；重建后的测试记录 | PR2 提交上游前 |

建议采用以下验收门槛：

1. 以当前提交重新确认 CI 结果，区分通过、跳过、未执行和失败；对 Pangu 等环境相关门禁提供实际运行证据，不能仅凭路径委托推断门禁通过。
2. 为当前 GPU 分组测试保存可追溯的原始日志、配置和模型/约束表版本；若要求更强一致性，应补充 token/score 断言。
3. 在生产代表性工作负载上完成跨 batch 模型质量评估，形成明确的接受条件。
4. 在真实服务进程和网络连接下验证断连、超时、失败恢复及多客户端行为。
5. PR1 合入后，重新基于上游构建、验证 PR2，不能直接复用 fork 基线的结果声明最终兼容性。

当前两个 PR 均未包含原整合分支中的任意长度 `client_batch.sh` 示例。若团队要求可直接运行的客户端示例，应单独补齐并核对其参数约束；不能将旧分支示例列为当前已交付内容。

本次文档修订仅更新设计说明，不修改实现代码、PR 状态或发布流程。

## 10. 评审入口与版本化依据

以下链接固定到本次核对的 commit，避免分支更新后引用的实现发生变化。PR 正文中的历史描述与固定版本代码不一致时，以代码及实际测试断言界定功能和证据范围。

- [PR1：上游评审入口][pr1]
- [PR2：fork 草稿评审入口][pr2]
- [PR1：固定批次 HTTP 路由][pr1-http]
- [PR2：拆批执行与响应归并][pr2-http]
- [PR2：纯拆批规划器][planner]
- [既有执行容量与运行约束][capacity]
- [PR1：固定批次系统测试][pr1-st]
- [PR2：拆批系统测试][pr2-st]
- [日志命名空间适配][logger]

[pr1]: https://github.com/JiusiServe/vllm-gr/pull/461
[pr2]: https://github.com/DINGEde/vllm-gr/pull/1
[pr1-http]: https://github.com/DINGEde/vllm-gr/blob/e8fe3dd520e4fca5dd63972b99f9182a25365f77/vllm_gr/entrypoints/openai/completion_batch_patch.py
[pr2-http]: https://github.com/DINGEde/vllm-gr/blob/057b664252634bc3ff00b59e6a909e6d25a1bf44/vllm_gr/entrypoints/openai/completion_batch_patch.py
[planner]: https://github.com/DINGEde/vllm-gr/blob/057b664252634bc3ff00b59e6a909e6d25a1bf44/vllm_gr/entrypoints/openai/completion_batch_plan.py
[capacity]: https://github.com/DINGEde/vllm-gr/blob/057b664252634bc3ff00b59e6a909e6d25a1bf44/vllm_gr/v1/engine/beam_search_v1.py
[pr1-st]: https://github.com/DINGEde/vllm-gr/blob/e8fe3dd520e4fca5dd63972b99f9182a25365f77/tests/st/test_onerec_v1_scheduler_st.py
[pr2-st]: https://github.com/DINGEde/vllm-gr/blob/057b664252634bc3ff00b59e6a909e6d25a1bf44/tests/st/test_onerec_v1_scheduler_st.py
[logger]: https://github.com/DINGEde/vllm-gr/blob/057b664252634bc3ff00b59e6a909e6d25a1bf44/vllm_gr/logger.py
