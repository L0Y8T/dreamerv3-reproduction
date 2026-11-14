#!/bin/bash
# Thin wrapper so WSL-side scripts can call the GPU arbiter without typing the Windows path.
# Written 2026-11-13. Keep this file pure ASCII.
#
# Usage from WSL:
#   bash /mnt/c/Users/10348/.dsh/skills/gpu-arbiter/gpuq.sh status
#   bash /mnt/c/Users/10348/.dsh/skills/gpu-arbiter/gpuq.sh acquire -Label x -MemMiB 13000
#   bash /mnt/c/Users/10348/.dsh/skills/gpu-arbiter/gpuq.sh doctor
set -u
PS1='C:\Users\10348\.dsh\skills\gpu-arbiter\gpuq.ps1'
exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$PS1" "$@"
