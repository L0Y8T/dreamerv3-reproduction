#!/bin/bash
# WSL 侧调用 gpuq 的薄封装（Windows 的 gpuq.ps1 从 WSL 里调）
set -u
exec powershell.exe -NoProfile -ExecutionPolicy Bypass \
  -File 'C:\Users\10348\.dsh\skills\gpu-arbiter\gpuq.ps1' "$@"
