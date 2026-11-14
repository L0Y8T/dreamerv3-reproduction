#!/usr/bin/env bash
# cheetah（第二任务）剩余部分的驱动：等 2026-11-12 01:00 起跑
#
# 背景：2026-11-11 12:19 用户要求停止（cheetah dreamer/s0 停在 67,136 步，已归档），改为凌晨继续。
# 已完成、不需重跑的：2e4 补跑 6 个 run（全部 rc=0）、cheetah 冒烟（rc=0）。
# 待跑：cheetah 2 臂 × 3 seed = 6 个 run，每个约 4.4 小时。
#
# 顺序：seed0 两臂 → seed1 两臂 → seed2 两臂（任意前缀都成立：n=1 的一对已有信号）
# 免占用窗口（每天 20:00-次日 01:00）由 window_lib.sh 统一判断：
#   预计会跨进窗口的 run 直接不开，不会留下跑一半被砍死的 run。
set -uo pipefail
export PATH="/root/.local/bin:$PATH"
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

REPO=/mnt/c/Users/10348/Desktop/超越之径/D3/repos/r2dreamer
BASE=/mnt/c/Users/10348/Desktop/工作/DreamerV3复现
PROG=$BASE/进展.md
PY=/root/r2d/.venv/bin/python
CONSOLE=/root/r2d/cheetah_resume_console.log
LOG_T2=/root/r2d/logdir_cheetah
TASK=${TASK:-dmc_cheetah_run}
STEPS=${STEPS:-510000}
EVAL_EVERY=${EVAL_EVERY:-10000}
START_AT=${START_AT:-"2026-11-13 01:00:00"}
EST_MIN=${EST_MIN:-300}

mkdir -p /root/r2d
exec >> "$CONSOLE" 2>&1
echo "==================== cheetah_resume 启动 $(date '+%F %T') pid=$$ ===================="

cd "$REPO" || { echo "repo 不存在"; exit 1; }
log() { printf '%s  [cheetah] %s\n' "$(date '+%Y-%m-%d %H:%M')" "$1" >> "$PROG"; }

# shellcheck source=/dev/null
. "/mnt/c/Users/10348/Desktop/工作/DreamerV3复现/window_lib.sh"

is_done() {
  local d="$1" target="$2" gran="$3" last
  [ -d "$d" ] || return 1
  last=$(grep -o '"step": *[0-9]*' "$d"/metrics.jsonl 2>/dev/null | grep -o '[0-9]*' | sort -n | tail -1)
  [ -n "${last:-}" ] || return 1
  [ "$last" -ge $((target - gran)) ]
}

wait_no_overlap() {
  local n=0
  while pgrep -f "train\.py" > /dev/null 2>&1; do
    n=$((n+1))
    if [ $n -eq 1 ]; then log "检测到 train.py 在跑，排队等待"; fi
    sleep 60
  done
}

# 2026-11-13 00:26 用户说「显卡正在执行别的任务，把训练接到这次工作后面」。
# 只查我们自己的 train.py 不够——别人的任务不叫 train.py，会直接抢显存把我们挤爆。
# 所以加一道显存+利用率闸：连续 GPU_CONFIRM 次采样都满足「空闲显存够 + 利用率低」才放行。
GPU_MIN_FREE_MIB=${GPU_MIN_FREE_MIB:-13500}
GPU_MAX_UTIL=${GPU_MAX_UTIL:-10}
GPU_CONFIRM=${GPU_CONFIRM:-3}

gpu_probe() {  # 输出 "<空闲MiB> <利用率%>"，读不到就输出空
  local used util
  used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
  util=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
  [ -n "$used" ] && [ -n "$util" ] || return 1
  echo "$(( 16376 - used )) $util"
}

wait_gpu_idle() {
  local streak=0 probe free util announced=0
  while :; do
    probe=$(gpu_probe) || { sleep 60; continue; }
    free=${probe%% *}
    util=${probe##* }
    if [ "$free" -ge "$GPU_MIN_FREE_MIB" ] && [ "$util" -lt "$GPU_MAX_UTIL" ]; then
      streak=$((streak+1))
      if [ $streak -ge "$GPU_CONFIRM" ]; then
        if [ $announced -eq 1 ]; then log "GPU 已空闲（空闲显存 ${free} MiB，利用率 ${util}%）-> 放行"; fi
        return 0
      fi
    else
      if [ $streak -gt 0 ]; then log "GPU 又被占用，重新计数"; fi
      streak=0
      if [ $announced -eq 0 ]; then
        log "GPU 被其他任务占用（空闲显存 ${free} MiB，利用率 ${util}%），等它结束再接上"
        echo "waiting for GPU: free=${free}MiB util=${util}%"
        announced=1
      fi
    fi
    sleep 60
  done
}

run_one() {
  local arm=$1 seed=$2 D=$3 t0 t1 rc mins
  if is_done "$D" "$STEPS" "$EVAL_EVERY"; then
    echo "[skip] $arm s$seed 已达标"; log "$arm s$seed 已达标 -> 跳过"; return 0
  fi
  wait_for_window "$EST_MIN"
  wait_no_overlap
  wait_gpu_idle
  echo "=== $arm s$seed 开始 $(date '+%F %T') ==="
  log "t2-$arm-s$seed 开始（task=$TASK 步数=$STEPS eval_every=$EVAL_EVERY compile=True）"
  t0=$(date +%s)
  $PY train.py \
    env=dmc_proprio env.task="$TASK" model=size12M \
    model.rep_loss="$arm" seed="$seed" \
    env.steps="$STEPS" trainer.eval_every="$EVAL_EVERY" trainer.update_log_every=5000 \
    model.compile=True \
    logdir="$D" > "/root/r2d/logs_t2-${arm}-s${seed}.log" 2>&1
  rc=$?
  t1=$(date +%s); mins=$(( (t1 - t0) / 60 ))
  echo "=== $arm s$seed 结束 rc=$rc 用时 $((t1-t0))s ==="
  log "t2-$arm-s$seed 结束 rc=$rc 用时 $((t1-t0)) 秒（约 ${mins} 分钟）"
  if [ $rc -ne 0 ]; then log "t2-$arm-s$seed 失败 rc=$rc（详见 /root/r2d/logs_t2-${arm}-s${seed}.log）"; fi
  return 0
}

TGT=$(date -d "$START_AT" +%s)
log "cheetah 剩余队列已就位（pid=$$）：等到 $START_AT 再跑 6 个 run"
echo "等待 $START_AT 起跑（现在 $(date '+%F %T')）"
# 心跳：2026-11-12 00:42 发现上一版驱动在 12 小时空档里静默死掉了（WSL 重启或睡眠导致），
# 当时日志里停在启动那一行，看不出是死是活。现在每 60 分钟写一条，死没死一看就知道。
n=0
while [ "$(date +%s)" -lt "$TGT" ]; do
  sleep 60
  n=$((n+1))
  if [ $((n % 60)) -eq 0 ]; then
    log "仍在等待起跑（已等 ${n} 分钟，pid=$$ 存活）"
    echo "heartbeat $(date '+%F %T') 已等 ${n} 分钟"
  fi
done
log "已到 $START_AT，cheetah 队列开工"

for seed in 0 1 2; do
  for arm in dreamer r2dreamer; do
    run_one "$arm" "$seed" "$LOG_T2/$arm/s$seed"
  done
  log "第二任务 seed$seed 的两臂都跑完了"
done

echo "CHEETAH_DONE $(date '+%F %T')"
log "CHEETAH_DONE —— 第二任务 $TASK 六个 run 全部走完（含失败，失败见各 rc 记录）"
