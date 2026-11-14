#!/usr/bin/env bash
# 满预算正式跑：DreamerV3(dreamer) vs R2-Dreamer(r2dreamer) × seeds 0/1/2
# 预算 5.1e5（设计值），eval_every 1e4，串行，全程 compile=True
# logdir 落在 Windows 可见目录，方便导出与断点续跑
set -uo pipefail
export PATH="/root/.local/bin:$PATH"
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

REPO=/mnt/c/Users/10348/Desktop/超越之径/D3/repos/r2dreamer
BASE=/mnt/c/Users/10348/Desktop/工作/DreamerV3复现
# ⚠️ logdir 必须是纯 ASCII：Hydra 的 override 语法不接受中文路径。
#    2026-11-03 实测：logdir=.../工作/DreamerV3复现/... 直接 LexerNoViableAltException，
#    6 个 run 各 4 秒全死（未浪费机时）。所以训练日志写 WSL 本地，事后再导出 JSONL 到 Windows。
LOGROOT=/root/r2d/logdir_live
PROG=$BASE/进展.md
PY=/root/r2d/.venv/bin/python

STEPS=${STEPS:-510000}
EVAL_EVERY=${EVAL_EVERY:-10000}
ARMS=${ARMS:-"dreamer r2dreamer"}
SEEDS=${SEEDS:-"0 1 2"}

cd "$REPO" || exit 1

log() { printf '%s  [wsl-full] %s\n' "$(date '+%Y-%m-%d %H:%M')" "$1" >> "$PROG"; }
is_done() { # $1=logdir $2=目标步数——末步够就算完成（不用 0.99）
  local d="$1" target="$2"
  [ -d "$d" ] || return 1
  local last
  # ⚠️ 2026-11-10 修：原来 grep 的是 events.out.tfevents.*（protobuf 二进制），永远匹配不到
  #    ⇒ is_done 恒为 false ⇒ 每次重启驱动都会把已完成的 seed 全部重跑。
  #    改读 metrics.jsonl：Logger 直接落的 JSONL 文本，每行形如 {"step": 472128, ...}
  last=$(grep -o '"step": *[0-9]*' "$d"/metrics.jsonl 2>/dev/null | grep -o '[0-9]*' | sort -n | tail -1)
  [ -n "${last:-}" ] || return 1
  [ "$last" -ge $((target - EVAL_EVERY)) ]
}

for arm in $ARMS; do
  for seed in $SEEDS; do
    D="$LOGROOT/$arm/s$seed"
    if is_done "$D" "$STEPS"; then
      echo "[skip] $arm s$seed 已完成"
      log "$arm s$seed 已存在且步数达标 -> 跳过"
      continue
    fi
    echo "=== $arm seed=$seed 开始 $(date '+%H:%M:%S') ==="
    log "$arm s$seed 开始（预算 $STEPS 步，compile=True）"
    T0=$(date +%s)
    $PY train.py \
      env=dmc_proprio env.task=dmc_walker_walk model=size12M \
      model.rep_loss=$arm seed=$seed \
      env.steps=$STEPS trainer.eval_every=$EVAL_EVERY trainer.update_log_every=5000 \
      model.compile=True \
      logdir="$D" > "$LOGROOT/${arm}_s${seed}.log" 2>&1
    RC=$?
    T1=$(date +%s)
    echo "=== $arm seed=$seed 结束 rc=$RC 用时 $((T1-T0))s ==="
    log "$arm s$seed 结束 rc=$RC 用时 $((T1-T0)) 秒"
    if [ $RC -ne 0 ]; then
      echo "!! $arm s$seed 失败，日志尾部："
      grep -vE 'EXTENSION_WARNING|warnings\.warn' "$LOGROOT/${arm}_s${seed}.log" | tail -15
      log "$arm s$seed 失败 rc=$RC（详见 ${arm}_s${seed}.log）"
    fi
  done
done

echo "ALL_ARMS_DONE"
log "满预算全部跑完（arms=$ARMS seeds=$SEEDS）"
