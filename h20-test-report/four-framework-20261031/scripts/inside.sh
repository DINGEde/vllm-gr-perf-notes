#!/bin/bash
set -euo pipefail
EXPECTED_UUID=$1
WORK=/home/david_cwq/DingYi
TOOL=$WORK/tools/competitor-suite-600d8035-wb1024/scripts/gpu_competitors
VENDOR_COMMIT=520e68cbc9632b38d0f61129831c9d0a934d31df
CONFIG=${2:-$WORK/configs/gr-wb210-h20-20261031-33a1fdb-r1.json}
echo "CONFIG=$CONFIG"
echo "TOOL=$TOOL"
export PYTHONNOUSERSITE=1 CUDA_DEVICE_ORDER=PCI_BUS_ID USE_TF=0 VLLM_WORKER_MULTIPROC_METHOD=spawn
SCRATCH=/mnt/scratch/users/david_cwq/DingYi/acceptance/gr-wb1024
mkdir -p "$SCRATCH" "$WORK/envs/competitors"
export UV_CACHE_DIR=$SCRATCH/uv TMPDIR=$SCRATCH/tmp XDG_CACHE_HOME=$SCRATCH/xdg XDG_CONFIG_HOME=$SCRATCH/config
export FLASHINFER_WORKSPACE_BASE=$SCRATCH/flashinfer TORCH_EXTENSIONS_DIR=$SCRATCH/torch CUDA_CACHE_PATH=$SCRATCH/cuda HF_HOME=$SCRATCH/hf
export TRITON_CACHE_DIR=$SCRATCH/triton
# SGLang writes $HOME/.cache otherwise; the container mounts $HOME read-only.
export SGLANG_CACHE_DIR=$SCRATCH/sglang SGLANG_JIT_CACHE_DIR=$SCRATCH/sglang-jit
# torch dynamo/inductor resolve ~/.cache unless redirected; $HOME is read-only here.
export HOME=$SCRATCH/home TORCHINDUCTOR_CACHE_DIR=$SCRATCH/torchinductor
mkdir -p "$UV_CACHE_DIR" "$TMPDIR" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$FLASHINFER_WORKSPACE_BASE" "$TORCH_EXTENSIONS_DIR" "$CUDA_CACHE_PATH" "$HF_HOME" "$TRITON_CACHE_DIR" "$SGLANG_CACHE_DIR" "$SGLANG_JIT_CACHE_DIR" "$HOME" "$TORCHINDUCTOR_CACHE_DIR"
export GIT_CONFIG_COUNT=3 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0=$WORK/source/vllm-gr GIT_CONFIG_KEY_1=safe.directory GIT_CONFIG_VALUE_1=$WORK/tools/competitor-suite-600d8035-wb1024 GIT_CONFIG_KEY_2=safe.directory GIT_CONFIG_VALUE_2=$WORK/tools/competitor-suite-600d8035
/usr/bin/python3 - "$EXPECTED_UUID" <<'PY'
import os,sys,torch
assert os.environ.get('CUDA_VISIBLE_DEVICES') and ',' not in os.environ['CUDA_VISIBLE_DEVICES']
assert torch.cuda.device_count()==1
n=lambda x:str(x).lower().removeprefix('gpu-')
assert n(torch.cuda.get_device_properties(0).uuid)==n(sys.argv[1])
print('ALLOCATION_UUID_OK',flush=True)
PY
cd "$TOOL"
git rev-parse HEAD
test "$(git rev-parse HEAD)" = "$VENDOR_COMMIT"
test -z "$(git diff HEAD -- .)"
cd "$WORK/source/vllm-gr"
echo "GR_HEAD=$(git rev-parse HEAD)"
git merge-base --is-ancestor 33a1fdb2e39dea9e30f648405bd263eb62b15323 HEAD
echo "=== PREVIEW ==="
cd "$TOOL"
bash run.sh --config "$CONFIG" --plan-only
echo "=== SETUP ==="
bash run.sh --config "$CONFIG" --lease-held --workers 1 --setup-only
RESULT_ROOT=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["root"])' "$CONFIG")
ENV_PYTHON=$(/usr/bin/python3 - "$RESULT_ROOT/bootstrap.json" <<'PY'
import json,sys
b=json.load(open(sys.argv[1])); assert len(b['outcomes'])==1, b['outcomes']
assert b['outcomes'][0]['status']=='ready', b['outcomes'][0]
print(b['outcomes'][0]['spec']['python'])
PY
)
echo "ENV_PYTHON=$ENV_PYTHON"
"$ENV_PYTHON" - "$EXPECTED_UUID" <<'PY'
import sys,torch
n=lambda x:str(x).lower().removeprefix('gpu-')
assert torch.cuda.device_count()==1
assert n(torch.cuda.get_device_properties(0).uuid)==n(sys.argv[1])
print('COMPETITOR_TORCH_UUID_OK',torch.__version__,torch.version.cuda,flush=True)
PY
echo "=== RUN ==="
exec bash run.sh --config "$CONFIG" --lease-held --workers 1
