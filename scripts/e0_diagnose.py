#!/usr/bin/env python
"""e0_diagnose.py —— 从 E0 的现场文件做「为什么只有 5 steps/s」的成本分解。

目的：门禁只回答「够不够快」，本脚本回答「慢在哪一段」，好让 停在这里 的下一步建议有依据。
所有输入都是 E0 已经落盘的文件，**不启动任何训练**：
    results\\E0_throughput.json         e0_gate.py 的口径（steps/s、eval 均值秒数等）
    logdir\\E0\\s0\\.hydra\\config.yaml   env_num / action_repeat / batch / train_ratio
    logdir\\E0\\s0\\harness\\gpu_samples.jsonl  按时间切「更新前 / 更新期」两段的 GPU 占用
    logdir\\E0\\s0\\harness\\stdout_timed.jsonl 找出第一次真正做梯度更新的时刻

要判的两件事:
  1. 慢在 GPU 算力上，还是慢在 Python/eager 调度上？→ 看更新期 GPU 利用率
     （如果利用率很低而步速也低 ⇒ 不是算力不够，是 eager 逐算子调度/同步在等 CPU）
  2. 每次 optimizer update 的隐含耗时、以及要打到 ≥30 steps/s 需要提速几倍。

用法：
    python e0_diagnose.py
"""

from __future__ import annotations

import json
import math
import re
import statistics as st
import sys
from datetime import datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

WORK_DIR = Path(__file__).resolve().parent
GATE_TIERS = {"local_full": 30.0, "degraded": 10.0}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[e0_diag {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def percentile(values, q):
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def load_hydra_config(path: Path) -> dict:
    from omegaconf import OmegaConf

    return OmegaConf.to_container(OmegaConf.load(str(path)), resolve=True)


def first_update_time(stdout_timed: Path) -> float | None:
    """第一条 train/opt/updates > 0 的 [step] 写点的相对时刻。"""
    if not stdout_timed.exists():
        return None
    for raw in stdout_timed.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        line = str(rec.get("line", ""))
        if not line.startswith("["):
            continue
        match = re.search(r"train/opt/updates\s+([\d.]+)", line)
        if match and float(match.group(1)) > 0.5:
            return float(rec.get("t", 0.0))
    return None


def gpu_window(samples: list[dict], lo: float | None, hi: float | None) -> dict:
    sel = [s for s in samples if (lo is None or s["t"] >= lo) and (hi is None or s["t"] <= hi)]
    if not sel:
        return {"n": 0}
    util = [s["util_pct"] for s in sel]
    mem = [s["mem_used_mib"] for s in sel]
    return {
        "n": len(sel),
        "t_from": round(sel[0]["t"], 1),
        "t_to": round(sel[-1]["t"], 1),
        "util_mean": round(st.fmean(util), 2),
        "util_median": round(percentile(util, 0.5), 2),
        "util_p90": round(percentile(util, 0.9), 2),
        "util_max": max(util),
        "util_frac_below_10pct": round(sum(1 for u in util if u < 10) / len(util), 3),
        "mem_mean_mib": round(st.fmean(mem), 1),
        "mem_max_mib": max(mem),
    }


def main() -> int:
    work = WORK_DIR
    gate_path = work / "results" / "E0_throughput.json"
    logdir = work / "logdir" / "E0" / "s0"
    if not gate_path.exists():
        log("先跑 e0_gate.py 生成 results/E0_throughput.json")
        return 2
    gate = json.loads(gate_path.read_text(encoding="utf-8", errors="replace"))
    cfg = load_hydra_config(logdir / ".hydra" / "config.yaml")

    env_num = int(cfg["env"]["env_num"])
    action_repeat = int(cfg["env"]["action_repeat"])
    time_limit = int(cfg["env"]["time_limit"])
    eval_episode_num = int(cfg["env"]["eval_episode_num"])
    batch_size = int(cfg["batch_size"])
    batch_length = int(cfg["batch_length"])
    train_ratio = int(cfg["trainer"]["train_ratio"])
    steps_per_iter = env_num * action_repeat

    thr = gate["throughput"]
    timing = gate["timing"]
    sps_loop = float(thr["sps_train_loop"] or 0.0)
    sps_med = float(thr["sps_interval_median"] or 0.0)
    eval_sec_mean = float(timing["eval_sec_mean"] or 0.0)
    train_only_sec = float(timing["train_only_sec"] or 0.0)
    steps_in_loop = int(timing["steps_in_loop"] or 0)

    # ---- 1. eval 段给出的纯环境步进速率（eval 不做梯度更新，只有 env.step + agent.act）----
    env_steps_per_eval = eval_episode_num * (time_limit // action_repeat)
    eval_env_step_rate = (env_steps_per_eval / eval_sec_mean) if eval_sec_mean > 0 else float("nan")

    # ---- 2. 训练循环每轮的成本分解 ----
    iterations = steps_in_loop / steps_per_iter if steps_per_iter else float("nan")
    per_iter_sec = (train_only_sec / iterations) if iterations else float("nan")
    env_share_sec = (steps_per_iter / eval_env_step_rate) if eval_env_step_rate else float("nan")
    # trainer.py:21  Every(batch_size*batch_length / train_ratio * action_repeat)
    every_every = batch_size * batch_length / train_ratio * action_repeat
    updates_per_iter = steps_per_iter / every_every if every_every else float("nan")
    implied_update_sec = (
        max(per_iter_sec - env_share_sec, 0.0) / updates_per_iter
        if updates_per_iter else float("nan")
    )
    implied_updates_per_sec = (1.0 / implied_update_sec) if implied_update_sec > 0 else float("nan")

    # ---- 3. GPU 两段占用：更新前 vs 更新期 ----
    samples = []
    gpu_path = logdir / "harness" / "gpu_samples.jsonl"
    for raw in gpu_path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            samples.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    t_first_update = first_update_time(logdir / "harness" / "stdout_timed.jsonl")
    before = gpu_window(samples, None, t_first_update)
    during = gpu_window(samples, t_first_update, None)

    # ---- 4. 门禁缺口：需要几倍提速，以及各预算档在当前速率下的墙钟 ----
    steady = min(x for x in (sps_loop, sps_med) if x > 0) if (sps_loop > 0 or sps_med > 0) else float("nan")
    needed = {name: (tier / steady if steady else float("inf")) for name, tier in GATE_TIERS.items()}
    budgets = {"5.1e5(设计预算)": 5.1e5, "1e5(设计降级档)": 1e5, "5e4": 5e4, "2e4": 2e4}
    wallclock = {
        name: {
            "steps": int(steps),
            "hours_one_seed": round(steps / steady / 3600.0, 2) if steady else None,
            "hours_three_seeds": round(3 * steps / steady / 3600.0, 2) if steady else None,
            "hours_six_seeds": round(6 * steps / steady / 3600.0, 2) if steady else None,
        }
        for name, steps in budgets.items()
    }

    report = {
        "generated_at": now_iso(),
        "generated_by": "e0_diagnose.py",
        "source": {
            "gate_json": str(gate_path),
            "gpu_samples": str(gpu_path),
            "hydra_config": str(logdir / ".hydra" / "config.yaml"),
        },
        "config": {
            "env_num": env_num,
            "action_repeat": action_repeat,
            "steps_per_iteration": steps_per_iter,
            "batch_size": batch_size,
            "batch_length": batch_length,
            "train_ratio": train_ratio,
            "every_every_data_steps": every_every,
            "updates_per_iteration": updates_per_iter,
            "eval_episode_num": eval_episode_num,
            "time_limit": time_limit,
            "model_compile": cfg["model"].get("compile"),
            "rep_loss": cfg["model"].get("rep_loss"),
        },
        "measured": {
            "sps_train_loop": sps_loop,
            "sps_interval_median": sps_med,
            "steady_used_for_min": steady,
            "steps_in_loop": steps_in_loop,
            "train_only_sec": train_only_sec,
            "eval_sec_mean": eval_sec_mean,
            "iterations": round(iterations, 1),
            "per_iteration_sec": round(per_iter_sec, 3),
        },
        "decomposition": {
            "eval_env_steps_per_sec": round(eval_env_step_rate, 1) if eval_env_step_rate else None,
            "env_share_of_iteration_sec": round(env_share_sec, 3) if env_share_sec else None,
            "env_share_pct": round(100 * env_share_sec / per_iter_sec, 1)
            if env_share_sec and per_iter_sec else None,
            "implied_sec_per_update": round(implied_update_sec, 3) if implied_update_sec else None,
            "implied_updates_per_sec": round(implied_updates_per_sec, 2) if implied_updates_per_sec else None,
            "note": (
                "env_share 用 eval 段（只步进环境、不做梯度更新）的速率外推；"
                "剩余时间全部归给 optimizer update。这是估算，不是 profile。"
            ),
        },
        "gpu": {
            "first_update_t_sec": t_first_update,
            "before_first_update": before,
            "during_updates": during,
        },
        "gap_to_gate": {
            "thresholds": GATE_TIERS,
            "speedup_needed_for_local_full_30sps": round(needed["local_full"], 2)
            if needed["local_full"] != float("inf") else None,
            "speedup_needed_for_degraded_10sps": round(needed["degraded"], 2)
            if needed["degraded"] != float("inf") else None,
            "wallclock_at_current_rate": wallclock,
        },
        "verdict_inputs": [
            "更新期 GPU 利用率若长期低（util_frac_below_10pct 高、util_median 低），说明不是算力不足，"
            "而是 eager 逐算子调度/同步在等 CPU —— 这正是 torch.compile 要消掉的开销。",
            "本机 venv_r2d 无 triton（Windows 官方不发），configs\\model\\_base_.yaml 的 compile: True 一旦启用就崩，"
            "所以只能用 eager 跑，拿不到作者的加速。",
        ],
    }
    out = work / "results" / "E0_diagnose.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    d = report["decomposition"]
    print()
    print("=== E0 成本分解 ===")
    print(f"每轮 = {steps_per_iter} env 步（{env_num} env × action_repeat {action_repeat}）"
          f"，每轮 {report['measured']['per_iteration_sec']:.2f}s，共 {report['measured']['iterations']} 轮")
    print(f"每轮 {updates_per_iter:.0f} 次 optimizer update（Every({every_every:g} data step)）")
    print(f"纯环境步进速率（eval 段实测）：{d['eval_env_steps_per_sec']} env 步/s "
          f"⇒ 每轮环境只占 {d['env_share_of_iteration_sec']}s（{d['env_share_pct']}%）")
    print(f"⇒ 反推每次 update ≈ {d['implied_sec_per_update']}s（{d['implied_updates_per_sec']} update/s）")
    print()
    print("=== GPU 占用（nvidia-smi 1s 采样）===")
    for label, w in (("更新开始前", report["gpu"]["before_first_update"]), ("更新期", report["gpu"]["during_updates"])):
        if w.get("n"):
            print(f"{label}：util 中位 {w['util_median']}% / 均值 {w['util_mean']}% / p90 {w['util_p90']}% / "
                  f"max {w['util_max']}% ｜ <10% 采样占比 {w['util_frac_below_10pct']:.0%} ｜ 显存峰值 {w['mem_max_mib']:.0f} MiB")
    print()
    print("=== 打到门禁要几倍 ===")
    print(f"当前稳态 {steady:.2f} steps/s ｜ ≥30 需提速 {report['gap_to_gate']['speedup_needed_for_local_full_30sps']}× ｜ "
          f"≥10 需提速 {report['gap_to_gate']['speedup_needed_for_degraded_10sps']}×")
    for name, w in wallclock.items():
        print(f"  {name}：单 seed {w['hours_one_seed']}h ｜ 3 seeds {w['hours_three_seeds']}h ｜ 6 seeds {w['hours_six_seeds']}h")
    print(f"→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
