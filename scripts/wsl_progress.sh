#!/usr/bin/env bash
echo "== 现在 =="; date '+%H:%M:%S'
echo
echo "== console 尾部（去警告）=="
grep -vE 'EXTENSION_WARNING|warnings\.warn|torchrl C\+\+' /root/r2d/E0c_console.log 2>/dev/null | tail -8
echo
echo "== event 文件名与大小 =="
ls -l /root/r2d/logdir/E0c/s0/events.out.tfevents.* 2>/dev/null
echo
echo "== GPU =="
nvidia-smi --query-gpu=utilization.gpu,memory.used,power.draw,temperature.gpu,clocks.sm --format=csv,noheader
echo
echo "== 训练进程 CPU 占用（前 5）=="
ps -eo pcpu,etime,args --sort=-pcpu | grep 'train.py' | grep -v grep | head -5
