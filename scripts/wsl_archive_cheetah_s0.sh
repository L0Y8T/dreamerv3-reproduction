#!/bin/bash
# 2026-11-12 12:00 重启实验前的准备：把被 Windows 更新重启打断的 cheetah s0 归档，
# 否则 is_done 判不到完成、重跑会写进同一个目录造成新旧数据混在一起。
set -u
D=/root/r2d/logdir_cheetah/dreamer/s0
if [ -d "$D" ]; then
  last=$(tail -1 "$D/metrics.jsonl" 2>/dev/null | grep -o '"step": *[0-9]*' | grep -o '[0-9]*')
  echo "被打断的 s0 停在：step=${last:-未知}  大小=$(du -sh "$D" | cut -f1)"
  NEW="${D}_aborted_20261112_0300"
  if [ -d "$NEW" ]; then echo "归档目标已存在，跳过改名"; else mv "$D" "$NEW" && echo "已归档到 $NEW"; fi
else
  echo "(没有未完成的 s0)"
fi
echo
echo "=== 归档后 cheetah 目录树 ==="
ls -d /root/r2d/logdir_cheetah/*/*/ 2>/dev/null || echo "(空)"
echo
echo "=== 确认没有训练在跑 ==="
pgrep -af "train\.py" || echo "(无)"
echo
echo "=== GPU ==="
nvidia-smi --query-gpu=utilization.gpu,memory.used,power.draw --format=csv,noheader
