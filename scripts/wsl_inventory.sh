#!/bin/bash
echo "=== cheetah logdir 树 ==="
for d in /root/r2d/logdir_cheetah/*/*/; do
  [ -d "$d" ] || continue
  n=$(ls -1 "$d" 2>/dev/null | wc -l)
  last=""
  if [ -f "$d/metrics.jsonl" ]; then
    last=$(tail -1 "$d/metrics.jsonl" 2>/dev/null | grep -o '"step": *[0-9]*' | grep -o '[0-9]*')
    sz=$(du -sh "$d" 2>/dev/null | cut -f1)
  fi
  echo "  $d  文件数=$n  大小=${sz:-?}  末步=${last:-无}"
done
echo
echo "=== 各自的创建/修改时间 ==="
for d in /root/r2d/logdir_cheetah/*/*/; do
  [ -d "$d" ] || continue
  stat -c '  %w  最后写 %y  %n' "$d" 2>/dev/null
done
echo
echo "=== 2e4 / smoke / logdir_live 是否完好 ==="
ls -d /root/r2d/logdir_2e4wsl/*/s* 2>/dev/null | wc -l | xargs echo "  2e4 目录数:"
ls -d /root/r2d/logdir_smoke_cheetah 2>/dev/null || echo "  smoke: 无"
ls -d /root/r2d/logdir_live/*/s[0-9] 2>/dev/null | wc -l | xargs echo "  walker 主目录数:"
echo
echo "=== WSL 内存与磁盘 ==="
free -g | head -2
df -h / | tail -1
echo
echo "=== 最近一次 WSL 启动时间 ==="
uptime -s 2>/dev/null || uptime
