#!/bin/bash
# 证明训练真的在算：看进程累计 CPU 时间有没有增加（比看 step 更灵敏，step 每 5000 步才写一次）
P=$(pgrep -f "train\.py" | head -1)
echo "PID=$P"
if [ -z "$P" ]; then echo "进程不在了"; exit 1; fi
echo "--- 第 1 次 ---"
ps -o pid,etime,time,pcpu,rss -p "$P" --no-headers
stat -c 'metrics.jsonl 最后写: %y' /root/r2d/logdir_cheetah/dreamer/s0/metrics.jsonl
stat -c 'console.log   最后写: %y' /root/r2d/logs_t2-dreamer-s0.log
STEP1=$(tail -1 /root/r2d/logdir_cheetah/dreamer/s0/metrics.jsonl | grep -o '"step": *[0-9]*' | grep -o '[0-9]*')
echo "step=$STEP1"
sleep 45
echo "--- 第 2 次（45 秒后）---"
ps -o pid,etime,time,pcpu,rss -p "$P" --no-headers
stat -c 'metrics.jsonl 最后写: %y' /root/r2d/logdir_cheetah/dreamer/s0/metrics.jsonl
STAT2=$(tail -1 /root/r2d/logdir_cheetah/dreamer/s0/metrics.jsonl | grep -o '"step": *[0-9]*' | grep -o '[0-9]*')
echo "step=$STAT2"
