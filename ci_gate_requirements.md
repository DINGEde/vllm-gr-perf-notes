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

### 5.1 实战：给已提 PR 追加 fix commit（单 commit 门禁）

给已提的 PR 追加修复时，多出第二个 commit 会触发 `Check for single commit` 失败，报 `Error: The PR has 2 commits. Please squash it into one commit.`。必须 squash 成一条再 force-push：

```bash
# 1. 回到 base 分支 tip，把本分支全部改动放回暂存区（staged）
git reset --soft <base_sha>

# 2. 一次提交（-s 自动加 Signed-off-by）
git commit -s -m "..." -m "..."

# 3. force-push
git push --force git@github-vllm-gr:DINGEde/vllm-gr.git <branch>:<branch>
```

**force-push 必须用 `--force`，不能用 `--force-with-lease`**：push 目标是完整 URL（`git@github-vllm-gr:DINGEde/vllm-gr.git`）而非配置好的 remote，本地没有 remote-tracking ref，`--force-with-lease` 会报 `stale info` 拒绝推送。

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

---

## 8. CI 失败定位技巧

门禁红了之后，不必一个个点网页，用 gh 命令行直接定位失败原因：

```bash
# 0. 找最新 run id（按 head 分支过滤）
gh run list --repo JiusiServe/vllm-gr --branch <head-branch> --limit 3 \
  --json databaseId,name,conclusion,headSha

# 1. 列出该 run 的 job 与结论，定位是哪个 job 失败
gh api repos/JiusiServe/vllm-gr/actions/runs/<run_id>/jobs \
  --jq '.jobs[] | "\(.id) \(.name): \(.conclusion)"'

# 2. 拉失败 job 的日志（失败的具体原因在这里）
gh run view <run_id> --repo JiusiServe/vllm-gr --job <job_id> --log
```

**两个关键点（本次实测踩坑）**：

1. **check-runs 的 annotations 经常信息不足**。例如单 commit 门禁失败时，check-runs annotations 只给 `Process completed with exit code 1`，真正的失败原因（`Error: The PR has 2 commits...`）只在 job 日志里。所以定位必须拉 job 日志，不能只看 check 列表的绿/红。

2. **日志下载走 Azure blob / results-receiver 域名，本地代理（`127.0.0.1:5188`）可能 EOF**。实测：
   - `gh run view <run_id> --log-failed` → 报 `Get "https://results-receiver.actions.githubusercontent.com/...": EOF`
   - `gh api .../actions/jobs/<job_id>/logs` → 报 `Get "https://productionresultssa15.blob.core.windows.net/...": EOF`
   - ✅ `gh run view <run_id> --job <job_id> --log` → 可用（重试几次，EOF 多为瞬时抖动）

   所以优先用 `gh run view --job <id> --log`；若失败多试几次再考虑换 `--log-failed`。
