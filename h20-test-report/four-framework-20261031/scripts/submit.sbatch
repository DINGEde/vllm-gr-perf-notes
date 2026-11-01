#!/bin/bash
#SBATCH --partition=h20
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=12:00:00
#SBATCH --job-name=DingYi-gr-wb1024
#SBATCH --output=/home/david_cwq/DingYi/logs/gr-wb1024-%j.out
set -euo pipefail
RUNTAG=${1:-smoke}
WORK=/home/david_cwq/DingYi
UUID=$(nvidia-smi -i "$CUDA_VISIBLE_DEVICES" --query-gpu=uuid --format=csv,noheader)
SCRATCH=/mnt/scratch/users/david_cwq/DingYi/acceptance
mkdir -p "$SCRATCH"
printf '%s\n' "$UUID" > "$WORK/manifests/gr-wb1024-host-gpu-${RUNTAG}-${SLURM_JOB_ID}.txt"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
df -h "$SCRATCH"
srun --container-image="$WORK/images/gr-decode-2a598ffa-vllm0221.sqsh" --no-container-entrypoint \
 --container-mounts="$WORK:$WORK,$SCRATCH:$SCRATCH,$WORK/source/vllm-gr:$WORK/source/vllm-gr:ro,/home/david_cwq/models/OpenOneRec-OneRec-1.7B:/home/david_cwq/models/OpenOneRec-OneRec-1.7B:ro,$WORK/tools/competitor-suite-600d8035-wb1024:$WORK/tools/competitor-suite-600d8035-wb1024:ro,$WORK/tools/competitor-suite-600d8035:$WORK/tools/competitor-suite-600d8035:ro" \
 bash "$WORK/jobs/gr-wb1024/inside.sh" "$UUID" "$WORK/configs/gr-wb1024-${RUNTAG}-h20-20261031-33a1fdb-r1.json"
