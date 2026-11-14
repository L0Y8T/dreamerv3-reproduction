#!/usr/bin/env python
"""watch_export.py —— 每完成一个 seed 就增量导出一次 JSONL（主窗口 A2：不要等全部跑完）。

为什么要这个脚本：训练要 7 小时左右，本窗口可能中途被杀（§9.5）。
把「完成 → 导出」做成独立的后台进程后，即使 agent 死了，只要训练还在跑，导出也照样落地。

判据（不重复实现一遍完成判据，直接读 run_repro.py 写进 run_meta.json 的字段）：
    run_meta.json 里 exit_code == 0 且（completed 为真 或 末步 ≥ 预算 − 2×eval_every）
已导出的判定：results\\<algo>-<tag>-s<seed>.jsonl 的 mtime 晚于 run_meta.json 的 mtime。

用法：
    python watch_export.py --plan E1-e2e4:dreamer:dmcww-e2e4 E2-e2e4:r2dreamer:dmcww-e2e4
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

WORK_DIR = Path(__file__).resolve().parent
PYTHON = Path(r"C:\Users\10348\Desktop\超越之径\D3\venv_r2d\Scripts\python.exe")
LOG_PATH = WORK_DIR / "watch_export.log"


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    line = f"[watch {now()}] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def load_plan(items: list[str]) -> list[dict]:
    plan = []
    for item in items:
        arm, algo, tag = item.split(":")
        plan.append({"arm": arm, "algo": algo, "tag": tag})
    return plan


def run_meta_of(work: Path, arm: str, seed: int) -> dict | None:
    path = work / "logdir" / arm / f"s{seed}" / "run_meta.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None


def is_done(meta: dict) -> bool:
    if meta.get("exit_code") != 0:
        return False
    budget = meta.get("steps_budget") or 0
    eval_every = meta.get("eval_every") or 0
    max_step = meta.get("actual_steps") or 0
    if meta.get("completed"):
        return True
    return bool(budget) and max_step >= budget - 2 * int(eval_every or 0)


def already_exported(work: Path, item: dict, seed: int) -> bool:
    out = work / "results" / f"{item['algo']}-{item['tag']}-s{seed}.jsonl"
    meta = work / "logdir" / item["arm"] / f"s{seed}" / "run_meta.json"
    if not out.exists():
        return False
    try:
        return out.stat().st_mtime >= meta.stat().st_mtime
    except OSError:
        return False


def export(work: Path, item: dict, seed: int) -> bool:
    cmd = [
        str(PYTHON),
        str(WORK_DIR / "export_r2d.py"),
        "--arm", item["arm"],
        "--algo", item["algo"],
        "--task-tag", item["tag"],
        "--seeds", str(seed),
    ]
    env = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
    import os

    child_env = os.environ.copy()
    child_env.update(env)
    res = subprocess.run(
        cmd,
        cwd=str(WORK_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=child_env,
        timeout=900,
    )
    for line in (res.stdout or "").strip().splitlines():
        log(f"  export| {line}")
    if res.returncode != 0:
        for line in (res.stderr or "").strip().splitlines()[-6:]:
            log(f"  export! {line}")
    return res.returncode == 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="训练完成一个 seed 就增量导出一次")
    ap.add_argument("--plan", nargs="+", required=True, help="arm:algo:task_tag ...")
    ap.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2])
    ap.add_argument("--interval", type=float, default=60.0, help="轮询间隔秒")
    ap.add_argument("--max-hours", type=float, default=12.0, help="最长守候小时数")
    ap.add_argument("--work", type=Path, default=WORK_DIR)
    args = ap.parse_args(argv)

    plan = load_plan(args.plan)
    log(f"开始守候：{plan}｜seeds={args.seeds}｜间隔 {args.interval:.0f}s｜上限 {args.max_hours}h")

    deadline = time.time() + args.max_hours * 3600
    exported: set[tuple[str, int]] = set()
    last_note = 0.0

    while time.time() < deadline:
        pending = 0
        for item in plan:
            for seed in args.seeds:
                key = (item["arm"], seed)
                meta = run_meta_of(args.work, item["arm"], seed)
                if meta is None:
                    pending += 1
                    continue
                if not is_done(meta):
                    pending += 1
                    continue
                if key in exported or already_exported(args.work, item, seed):
                    if key not in exported:
                        exported.add(key)
                        log(f"{item['arm']} s{seed} 之前已导出过，跳过")
                    continue
                log(f"{item['arm']} s{seed} 完成（exit 0 / {meta.get('actual_steps')} 步 / "
                    f"{meta.get('wall_sec')}s / eval {meta.get('eval_points')} 点）⇒ 增量导出")
                if export(args.work, item, seed):
                    exported.add(key)
                    log(f"{item['arm']} s{seed} 导出完成")
                else:
                    log(f"{item['arm']} s{seed} 导出失败，下一轮再试")
                    pending += 1

        if pending == 0:
            log(f"全部 {len(plan) * len(args.seeds)} 个 (arm, seed) 已导出，退出")
            return 0
        if time.time() - last_note > 1800:
            done_n = len(plan) * len(args.seeds) - pending
            log(f"进度 {done_n}/{len(plan) * len(args.seeds)} 已导出，{pending} 个待完成")
            last_note = time.time()
        time.sleep(args.interval)

    log("守候超时退出")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
