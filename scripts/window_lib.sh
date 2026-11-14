#!/usr/bin/env bash
# 免占用窗口逻辑（用户 2026-11-10 规定：每天 20:00 → 次日 01:00 不占 GPU）
#
# 独立成文件是为了能单独测：wsl_night.sh source 它；wsl_test_window2.sh 也 source 它，
# 再用 FORCE_NOW 假装不同时刻，验证边界判断对不对（不靠猜）。
#
# 时间边界全部由「本地日期的 20:00」推出来，避免 "today/tomorrow" 这类相对写法在多天循环里出错：
#   今天 20:00 = t20
#   今天 01:00 = t20 - 19h        （= 昨天 20:00 那个窗口的结束时刻）
#   明天 01:00 = t20 + 5h         （= 今天 20:00 那个窗口的结束时刻）
#
# 判定：
#   ① now < 今天01:00              → 在「昨天 20:00 起」的窗口内，等到今天 01:00
#   ② now >= 今天20:00             → 在今天窗口内，等到明天 01:00
#   ③ now + 预计耗时 > 今天20:00    → 会跨进窗口，等到明天 01:00
#   ④ 其余                          → 可以直接开跑

# 打印边界：t20 今天20:00 / prev01 今天01:00 / next01 明天01:00
window_bounds() {
  local now day t20
  now=${FORCE_NOW:-$(date +%s)}
  day=$(date -d "@$now" +%F)
  t20=$(date -d "$day 20:00" +%s)
  echo "$t20 $(( t20 - 19 * 3600 )) $(( t20 + 5 * 3600 ))"
}

# 返回值：0=可以开跑；1=需要等待（dryrun 时也会返回 0 但打印 plan）
# 用法：wait_for_window <预计分钟数>
wait_for_window() {
  # 2026-11-13 13:32 用户明确说「不管窗口，一空就接」-> NO_WINDOW=1 时直接放行
  if [ "${NO_WINDOW:-0}" = "1" ]; then return 0; fi
  local est_min=${1:-300}
  local now day t20 prev01 next01 why t01
  while :; do
    read -r t20 prev01 next01 <<< "$(window_bounds)"
    now=${FORCE_NOW:-$(date +%s)}
    why=""
    if [ "$now" -lt "$prev01" ]; then
      why="当前在 20:00-01:00 免占用窗口内"; t01=$prev01
    elif [ "$now" -ge "$t20" ]; then
      why="当前在 20:00-01:00 免占用窗口内"; t01=$next01
    elif [ $(( now + est_min * 60 )) -gt "$t20" ]; then
      why="本 run 预计 ${est_min} 分钟，会跨进 20:00 免占用窗口"; t01=$next01
    else
      return 0
    fi

    if [ "${WINDOW_DRYRUN:-0}" = "1" ]; then
      echo "[dryrun] $why ⇒ 若真要跑会等到 $(date -d "@$t01" '+%F %H:%M')"
      return 0
    fi

    if type log > /dev/null 2>&1; then
      log "$why ⇒ 等到 $(date -d "@$t01" '+%F %H:%M') 再开工"
    fi
    echo "等待免占用窗口结束：$why（目标 $(date -d "@$t01" '+%F %H:%M')）"
    while [ "$(date +%s)" -lt "$t01" ]; do sleep 60; done
    # 醒来后重新判断（处理跨天、以及 FORCE_NOW 之外的时钟漂移）
    unset FORCE_NOW
  done
}
