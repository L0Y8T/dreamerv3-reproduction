#!/bin/bash
echo "=== 训练进程 ==="
pgrep -af "train\.py" | head -2 || echo "  (无 train.py !!)"
echo
echo "=== env 子进程数（应为 26）==="
pgrep -cf "multiprocessing.spawn" || echo 0
echo
echo "=== 驱动是否还活着 ==="
pgrep -af "wsl_cheetah_resume" | head -1 || echo "  (驱动不在 !!)"
echo
echo "=== 当前步数（第一次采样）==="
tail -1 /root/r2d/logdir_cheetah/dreamer/s0/metrics.jsonl 2>/dev/null | grep -o '"step": *[0-9]*'
echo
echo "=== 日志尾部有没有报错 ==="
grep -iE "error|traceback|out of memory|cuda" /root/r2d/logs_t2-dreamer-s0.log 2>/dev/null | tail -3 || echo "  (无报错关键字)"
