#!/bin/bash
# 前台运行队列（wsl.exe 会话要一直挂着，进程才不会被回收）
# 教训：16:02 那次用 `nohup bash 队列 &` 起，父脚本一退出会话就关了，队列当场没了。
cd "/mnt/c/Users/10348/Desktop/工作/DreamerV3复现" || { echo "目录不存在"; exit 1; }
export NO_WINDOW=1
export GPU_CONFIRM=2
export START_AT="2026-11-13 00:00:00"
echo "=== run_queue 启动 $(date '+%F %T') ==="
exec bash ./wsl_cheetah_resume.sh
