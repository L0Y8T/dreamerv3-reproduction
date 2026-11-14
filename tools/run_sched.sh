#!/bin/bash
# 统排执行器 v2 · 2026-11-13 21:42（修 v1 的死循环 + 日志刷屏）
#
# v1 的两个 bug：
#   ① 在顶层用 `local`（bash 只允许函数内用）-> 变量为空 -> 时间算不出来 -> 内层 while 不等待
#      -> 外层 while 无限重跑 -> 往 进展.md 写了 8 万多行垃圾
#   ② 没有重复次数上限，一出错就是死循环
#
# v2 规则不变：
#   不在窗口期 / 剩余时间够跑完一个 run / GPU 真空闲 / gpuq 令牌
set -u
BASE="/mnt/c/Users/10348/Desktop/工作"
PROG="$BASE/DreamerV3复现/进展.md"
BOARD="$BASE/GPU统筹/任务板.md"
REPO="/mnt/c/Users/10348/Desktop/超越之径/D3/repos/r2dreamer"
PY=/root/r2d/.venv/bin/python
GPUQ="$BASE/GPU统筹/gpuq_run.sh"
LOG="/root/r2d/sched_console.log"

STEPS=510000
EVAL_EVERY=10000
TASK_ENV=dmc_cheetah_run
BLACKOUT_START=${BLACKOUT_START:-22}
BLACKOUT_END=${BLACKOUT_END:-24}
EST_SEC=${EST_SEC:-15600}
MIN_FREE_MIB=${MIN_FREE_MIB:-13500}
MAX_UTIL=${MAX_UTIL:-10}
CONFIRM=${CONFIRM:-3}

exec >> "$LOG" 2>&1
say() {
  echo "$(date '+%F %T')  $1"
  printf '%s  [sched] %s\n' "$(date '+%Y-%m-%d %H:%M')" "$1" >> "$PROG"
}
board() { printf '%s\n' "$1" >> "$BOARD"; }

in_blackout() {
  local h; h=$(date +%-H)
  if [ "$BLACKOUT_END" -le "$BLACKOUT_START" ]; then
    [ "$h" -ge "$BLACKOUT_START" ] || [ "$h" -lt "$BLACKOUT_END" ]
  else
    [ "$h" -ge "$BLACKOUT_START" ] && [ "$h" -lt "$BLACKOUT_END" ]
  fi
}

blackout_start_epoch() {
  local d
  if in_blackout; then d=$(date +%F); else d=$(date -d "today ${BLACKOUT_START}:00" +%F 2>/dev/null || date +%F); fi
  date -d "$d $(printf '%02d' "$BLACKOUT_START"):00" +%s
}

blackout_end_epoch() {
  local d
  if in_blackout; then
    d=$(date +%F)
  else
    d=$(date -d "today ${BLACKOUT_START}:00" +%F 2>/dev/null || date +%F)
  fi
  if [ "$BLACKOUT_END" -ge 24 ] || [ "$BLACKOUT_END" -eq 0 ]; then
    date -d "$d 00:00 +1 day" +%s
  else
    date -d "$d $(printf '%02d' "$BLACKOUT_END"):00" +%s
  fi
}

gpu_probe() { nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' '; }

is_done() {
  local d="/root/r2d/logdir_cheetah/$1/s$2"
  [ -d "$d" ] || return 1
  local last
  last=$(grep -o '"step": *[0-9]*' "$d"/metrics.jsonl 2>/dev/null | grep -o '[0-9]*' | sort -n | tail -1)
  [ -n "${last:-}" ] && [ "$last" -ge $((STEPS - EVAL_EVERY)) ]
}

wait_out_of_blackout() {
  local guard=0 e
  while in_blackout; do
    e=$(blackout_end_epoch)
    if [ -z "${e:-}" ] || [ "$e" -le 0 ]; then
      say "窗口期结束时间算不出来，等 10 分钟重试"
      sleep 600; guard=$((guard+1))
      [ "$guard" -ge 30 ] && return 1
      continue
    fi
    say "在窗口期内（${BLACKOUT_START}:00-${BLACKOUT_END}:00），等到 $(date -d "@$e" '+%m-%d %H:%M')"
    while [ "$(date +%s)" -lt "$e" ]; do sleep 60; done
  done
  return 0
}

fits_before_blackout() {
  local now bs
  now=$(date +%s); bs=$(blackout_start_epoch)
  [ -z "${bs:-}" ] && return 0
  if [ "$bs" -le "$now" ]; then return 0; fi
  [ $((now + EST_SEC)) -le "$bs" ]
}

wait_gpu_idle() {
  local streak=0 p used util
  while :; do
    p=$(gpu_probe); used=${p%%,*}; util=${p##*,}
    if [ -z "${used:-}" ]; then sleep 40; continue; fi
    if [ "$used" -le $((16376 - MIN_FREE_MIB)) ] && [ "$util" -lt "$MAX_UTIL" ]; then
      streak=$((streak+1))
      if [ "$streak" -ge "$CONFIRM" ]; then say "GPU 空闲确认（used=${used} MiB util=${util}%）"; return 0; fi
    else
      if [ "$streak" -ne 0 ]; then say "GPU 又忙了（used=${used} util=${util}%），重新计数"; fi
      streak=0
    fi
    sleep 40
  done
}

say "=== 统排执行器 v2 启动 pid=$$ （窗口期 ${BLACKOUT_START}:00-${BLACKOUT_END}:00）==="
say "待办：dreamer s2 -> r2dreamer s2（r2dreamer s1 已于 20:27 完成，667.31）"

for spec in "dreamer 2" "r2dreamer 2"; do
  set -- $spec; ARM=$1; SEED=$2
  if is_done "$ARM" "$SEED"; then say "$ARM s$SEED 已完成，跳过"; continue; fi

  wait_out_of_blackout || { say "窗口期等待失败，退出"; break; }

  if ! fits_before_blackout; then
    BE=$(blackout_end_epoch)
    if [ -n "${BE:-}" ] && [ "$BE" -gt 0 ]; then
      say "现在开工会跨进窗口期（需要 $((EST_SEC/3600)) 小时），推迟到 $(date -d "@$BE" '+%m-%d %H:%M')"
      while [ "$(date +%s)" -lt "$BE" ]; do sleep 60; done
      wait_out_of_blackout || { say "窗口期等待失败，退出"; break; }
    else
      say "窗口期结束时间算不出来，保守等 30 分钟"; sleep 1800
    fi
  fi

  wait_gpu_idle

  TID=""
  if [ -f "$GPUQ" ]; then
    OUT=$(timeout 150 bash "$GPUQ" acquire -Fast -Override -Label "cheetah-$ARM-s$SEED" -MemMiB 13000 -EtaMin 260 2>&1 | tail -3)
    TID=$(echo "$OUT" | grep -o 'ACQUIRED id=[a-f0-9]*' | grep -o '[a-f0-9]*$')
    if [ -n "$TID" ]; then say "令牌已取得：$TID"; else say "令牌未取得（继续跑，靠自带显存闸）"; fi
  fi

  T0=$(date +%s)
  ETA=$(date -d "@$((T0+EST_SEC))" '+%m-%d %H:%M')
  say "开始 $ARM s$SEED（预计 $ETA 收）"
  board "| $(date '+%m-%d %H:%M') | cheetah · $ARM · s$SEED | 进行中 | 预计 $ETA | 统排执行器 v2 |"

  cd "$REPO" || exit 1
  $PY train.py \
    env=dmc_proprio env.task="$TASK_ENV" model=size12M \
    model.rep_loss="$ARM" seed="$SEED" \
    env.steps=$STEPS trainer.eval_every=$EVAL_EVERY trainer.update_log_every=5000 \
    model.compile=True \
    logdir="/root/r2d/logdir_cheetah/$ARM/s$SEED" > "/root/r2d/logs_t2-${ARM}-s${SEED}.log" 2>&1
  RC=$?
  T1=$(date +%s)
  SAYMIN=$(( (T1-T0)/60 ))
  say "结束 $ARM s$SEED rc=$RC 用时 ${SAYMIN} 分钟"
  board "| $(date '+%m-%d %H:%M') | cheetah · $ARM · s$SEED | rc=$RC | 实际 ${SAYMIN} 分钟 | 统排执行器 v2 |"
  [ -n "$TID" ] && timeout 60 bash "$GPUQ" release -Id "$TID" >/dev/null 2>&1
done

say "统排执行器完成：所有任务走完"
board "| $(date '+%m-%d %H:%M') | — | 全部完成 | — | 统排执行器 v2 |"
