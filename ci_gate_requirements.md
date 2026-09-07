# CI / PR 门禁要求

> 向 JiusiServe/vllm-gr 提 PR 时的门禁（pre-commit + GitHub Actions）要求与实测结论。
> 面向需要改代码、提 PR 的同事：知道门禁跑什么、哪些坑会白费时间、提交前如何自查。

---

## 1. 门禁总览

CI 有两层：

1. **pre-commit 钩子**（`.pre-commit-config.yaml`）——本地 `git commit` 和 CI `pre-commit` job 都会跑，`fail_fast: true`（一个失败即停）。
2. **GitHub Actions workflow**（`.github/workflows/pre-commit.yml`）——含版本断言、单 commit 检查、GPU benchmark 门禁。

workflow job 依赖链：`benchmark-policy` → `pre-commit` → `decode-benchmark` → `pangu-offline` → `pangu-online` → `benchmark-blocking`（汇总）。

---

## 2. 关键工具版本（必须对齐，否则误判）

| 工具 | 门禁版本 | 备注 |
|---|---|---|
| ruff | `v0.14.10`（pre-commit rev） | ⚠️ CI 实际跑 0.16.4（日志 `ruff==0.16.4`），两版默认规则不同（见 §4） |
| isort | `8.0.1` | `--profile black` |
| mypy | `1.11.1` | 3 个 hook：`mypy-3.11` / `mypy-3.12` / `mypy-3.13` |
| typos | `v1.43.5` | 拼写检查 |
| clang-format | `v21.1.2` | C++/CUDA |
| markdownlint | `v0.47.0` | 排除 `*.inc.md` |
| actionlint | `v1.7.9` | 校验 `.github/workflows/*.yml` |
| pre-commit-hooks | `v6.0.0` | yaml/json/trailing/EOF/line-ending/debug |
| vllm / vllm-gr | **0.22.1**（workflow 硬断言） | `importlib.metadata.version` 断言相等 |

---

## 3. pre-commit 钩子清单

来自 `.pre-commit-config.yaml`，本地 `pre-commit run --all-files` 全量、`pre-commit run` 只跑暂存文件：

**通用（pre-commit-hooks v6.0.0）**
- `check-yaml`（`--unsafe`）、`debug-statements`（禁 pdb/ipdb）、`end-of-file-fixer`、`mixed-line-ending`（`--fix=lf`，**统一 LF**）、`trailing-whitespace`（`--markdown-linebreak-ext=md`）

**Python**
- `isort`（`--profile black`）
- `ruff-check`（lint，`--output-format github --fix`）
- `ruff-format`（格式）
- `mypy-3.11/3.12/3.13`（`tools/pre_commit/mypy.py`，只查 changed files）

**本地自定义钩子（tools/pre_commit/）**
- `check-python-version`（3.11/3.12/3.13）
- `shellcheck`（shell 脚本）、`png-lint`（excalidraw 导出）
- `check-spdx-header`（SPDX 头，**排除** `benchmarks/open_one_rec/`）
- `check-filenames`（文件名禁空格）、`enforce-import-regex-instead-of-re`（用 `regex` 而非 `re`）
- `check-torch-cuda-call`（禁新 `torch.cuda` API）
- `validate-config`（配置有默认值 + 每个字段有 docstring）
- `check-pickle-imports`（禁新 pickle/cloudpickle import）
- `check-boolean-context-manager`（with 语句禁布尔运算）
- `check-characters`（`check_english.py`，文件字符校验）
- `run-tests`（跑全部测试，`tools/pre_commit/run_tests.sh`）

**C++/文档/其他**
- `clang-format`（排除部分 triton/ggml 内核）、`markdownlint`、`actionlint`

**commit-msg 阶段**
- `signoff-commit`（自动加 `Signed-off-by:`，见 §5）

---

## 4. 实测关键结论（避坑）

这些是从实际提 PR 过程中验证的结论，能省大量排查时间：

### 4.1 mypy 门禁**不检查** `vllm_gr/entrypoints/gr.py`

`tools/pre_commit/mypy.py` 里 `FILES=["vllm_gr/*.py"]` 被 `regex` 当正则编译成 `^(vllm_gr/*.py).*`，其中 `*` 是量词、`/*.py` 语义是「0+ 个 `/` + 任意字符 + `py`」，**匹配不到任何真实路径** → main group 恒空。

`gr.py` 不在 `SEPARATE_GROUPS`（attention/compilation/lora/model_executor/v1/...）也不在 `EXCLUDE`，被 `group_files()` 静默丢弃。权威验证 `group_files(["vllm_gr/entrypoints/gr.py"])` 返回空 dict。

**结论**：gr.py 里 ~20 个预先存在的 strict-mypy 错误（no-untyped-def/arg-type/attr-defined 等）**不阻塞门禁**，不必为它们浪费时间。

> 但改 `SEPARATE_GROUPS` 里的目录（如 `vllm_gr/v1/`、`vllm_gr/attention/`）时 mypy 会真跑。

### 4.2 ruff 版本差异：0.14.10（rev）vs 0.16.4（CI 实际）

- **lint**（`ruff-check`）：只启用 E4/E7/E9/F。容器 0.16.4 默认规则集扩大后会多报 `BLE001/RUF017/RUF007/TC004/I001`——这些 0.16 新增默认规则**门禁不命中**。
- **format**（`ruff-format`）：关键配置是 `pyproject.toml` 的 `[tool.ruff] line-length = 100`（**不是默认 88**）。
- **教训**：不要手动按 88 猜 ruff format 换行（会反向拆错）。直接用：

  ```bash
  docker exec -i vllm-gr-dev ruff format --check --diff --line-length 100 \
    --stdin-filename gr.py - < gr.py
  ```

### 4.3 提交前用门禁版工具自查

```bash
# 门禁版本，避免本地新版本误报/漏报
python3 -m pip install ruff==0.14.10 isort==8.0.1

ruff check gr.py
ruff format --check gr.py
isort --check-only --profile black gr.py
```

### 4.4 import 排序（isort `--profile black`）

- `from typing import ..., cast` 加在最后。
- `from transformers import PreTrainedTokenizerBase` 插在 `tqdm` 与 `vllm` 之间。

---

## 5. workflow 层硬要求（`.github/workflows/pre-commit.yml`）

| 要求 | 位置 | 说明 |
|---|---|---|
| **PR 单 commit** | `Check for single commit` | `github.event.pull_request.commits != 1` 直接 exit 1；多 commit 需 squash |
| **Signed-off-by** | `signoff-commit` hook（commit-msg 阶段） | 自动追加，无需手写 |
| **版本断言** | `Verify vLLM compatibility` | `vllm == 0.22.1` 且 `vllm-gr == 0.22.1`，否则 exit 1 |
| **editable 安装** | `uv pip install -e ".[dev]"` | 用 `uv`，缓存 key 含 pyproject/setup/requirements hash |
| **设备清理** | `kill_vllm_on_device.sh` / `check_vllm_on_device.sh` | job 前后杀/查残留 vllm 进程 |

**运行环境**：`runs-on: self-hosted`，Python 3.11，`astral-sh/setup-uv`，venv 缓存到 `/data/actions/cache`。

---

## 6. GPU benchmark 门禁（`decode-benchmark` job）

阈值来自 `.github/benchmark-gate.yaml`（`version: 2, name: development`），由 `benchmark-policy` job 动态解析（支持 `device_overrides` 按板卡覆盖）：

### 6.1 准确率（accuracy，`failure_mode: blocking`）

| 指标 | 阈值 |
|---|---|
| pass@1 | ≥ 0.09 |
| pass@32 | ≥ 0.29 |
| recall@32 | ≥ 0.04 |

### 6.2 性能（performance，`failure_mode: warning`，不阻塞）

| 指标 | 阈值 |
|---|---|
| p99_ttft | ≤ 300ms（NPU Ascend910_9362 覆盖为 280ms） |
| p99_tpot | ≤ 0.02 |
| online_elapsed_seconds | ≤ 20s |
| offline_elapsed_seconds | ≤ 20s（NPU 覆盖为 25s） |

### 6.3 Pangu 1B 准确率（`failure_mode: blocking`）

| 指标 | 阈值 |
|---|---|
| offline_accuracy | ≥ 1.00（NPU 覆盖 0.875） |
| online_accuracy | ≥ 1.00（NPU 覆盖 0.875） |

> `online_non_accuracy` 也是 `blocking`。`decode-benchmark` 跑 `tests/system/run_benchmark_and_verify.sh`（需 `HF_TOKEN` 下 gated OpenOneRec 数据集 + 建 catalog/constraint triples）。

---

## 7. 提交前自查清单

改 Python 代码提 PR 前，本地按顺序过一遍：

```bash
# 1. 行尾统一 LF（Windows 本地注意 CRLF 会污染 diff）
sed -i "s/\r$//" <file>

# 2. 门禁版工具校验
pip install ruff==0.14.10 isort==8.0.1
ruff check <file> && ruff format --check <file> && isort --check-only --profile black <file>

# 3. 单 commit（squash 成一条）
git log --oneline @{u}..HEAD    # 应只有 1 条

# 4. 提交（-s 自动加 Signed-off-by）
git commit -s
```

**提交路径提示**（详见项目 memory）：服务器 SSH over 443 → 自己的 fork `DINGEde/vllm-gr` push → 本地 Windows `gh`（带代理）`gh pr create`。服务器对 GitHub 只通 SSH over 443，HTTPS 全断。
