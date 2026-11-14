#!/usr/bin/env python
"""e0_gate.py —— 从 E0 那一次探针的现场文件算出吞吐 / 显存峰值 / 预计墙钟 / 门禁分档。

全部数字由脚本算，不手打。输入（都在 logdir\\E0\\s<seed>\\ 下，由 run_repro.py 产出）：
    run_meta.json                 exit code、墙钟、实际步数、命令原文
    harness\\stdout_timed.jsonl    带相对时间戳的 stdout（用来切分 启动 / eval / 训练 三段）
    harness\\gpu_samples.jsonl     每秒一条 nvidia-smi 采样（显存峰值在这里）
    metrics.jsonl                  Logger.write 落的标量（train/opt/updates、fps/fps 等）

门禁分档写死在 实验设计_2026-11-02.md §2：
    steps/s ≥ 30        → 本地跑完整预算 5.1e5
    10 ≤ steps/s < 30   → 先降到 1e5 出曲线，完整预算改投 HPC
    steps/s < 10        → 停，改走 WSL2（路线 A）或 HPC（路线 B）

两条吞吐口径都给：
    sps_train_loop  训练循环均速（总步数 / 扣掉启动与 eval 的训练时间）—— 表示「机器能跑多快」
    sps_interval    更新期区间中位数（每个区间 = 一次 [step] 日志到下一次）—— 表示「稳态能跑多快」
分档用两者中**较小**的那个（保守），避免用一个好看的数放自己过关。

用法：
    python e0_gate.py --arm E0 --seed 0 --budget 510000 --evals-default 10000
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

WORK_DIR = Path(__file__).resolve().parent

# 门禁分档（设计文档 §2 原文）
TIER_LOCAL_FULL = 30.0
TIER_DEGRADED = 10.0
FULL_BUDGET_STEPS = 5.1e5
DESIGN_EVAL_EVERY = 1e4


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[e0_gate {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def percentile(values: list[float], q: float) -> float:
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


# --------------------------------------------------------------------------- #
def parse_stdout(path: Path) -> dict:
    """把带时间戳的 stdout 切成 eval 窗口 / 训练写点 / 启动阶段。"""
    if not path.exists():
        return {"events": [], "error": f"缺少 {path}"}
    events = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        text = str(rec.get("line", ""))
        t = float(rec.get("t", 0.0))
        match = re.match(r"^\[(\d+)\]\s*(.*)$", text)
        if match:
            events.append({"kind": "write", "t": t, "step": int(match.group(1)), "line": text})
        elif text.startswith("Evaluating the policy"):
            events.append({"kind": "eval_start", "t": t, "line": text})
        elif text.startswith("Compiling update function"):
            events.append({"kind": "compile", "t": t, "line": text})
        elif text.startswith(("Logdir", "Create envs", "Simulate agent")):
            events.append({"kind": "phase", "t": t, "line": text})
    return {"events": events}


def analyse(events: list[dict]) -> dict:
    writes = [e for e in events if e["kind"] == "write" and e["step"] > 0]
    eval_starts = [e for e in events if e["kind"] == "eval_start"]
    compiles = [e for e in events if e["kind"] == "compile"]
    phases = {e["line"].split(" ")[0]: e["t"] for e in events if e["kind"] == "phase"}

    # ---- eval 窗口：从 "Evaluating the policy..." 到下一条 [step] 写点 -------
    eval_windows = []
    for es in eval_starts:
        nxt = next((w for w in writes if w["t"] >= es["t"]), None)
        if nxt is not None:
            eval_windows.append({"start_t": es["t"], "end_t": nxt["t"],
                                 "sec": round(nxt["t"] - es["t"], 3), "write_step": nxt["step"]})

    first_eval_t = eval_windows[0]["start_t"] if eval_windows else (writes[0]["t"] if writes else 0.0)
    last_write = writes[-1] if writes else None

    # ---- 写点之间的区间 ---------------------------------------------------- #
    intervals = []
    for prev, cur in zip(writes, writes[1:]):
        delta_step = cur["step"] - prev["step"]
        sec = cur["t"] - prev["t"]
        if delta_step <= 0 or sec <= 0:
            continue
        contaminated = any(prev["t"] < es["t"] <= cur["t"] for es in eval_starts)
        warmup_compile = any(prev["t"] < c["t"] <= cur["t"] for c in compiles)
        upd = re.search(r"train/opt/updates\s+([0-9.]+)", cur["line"])
        updates_logged = float(upd.group(1)) if upd else None
        # 第一次进 update 块时 pretrain 的 Once() 会吃掉一次（update_num=0），该区间不算训练样本
        warmup_zero_update = updates_logged == 0.0
        update_phase = ("train/" in cur["line"]) or ("fps/fps" in cur["line"])
        intervals.append({
            "step_from": prev["step"], "step_to": cur["step"], "steps": delta_step,
            "sec": round(sec, 3), "sps": round(delta_step / sec, 3),
            "update_phase": update_phase,
            "contaminated_by_eval": contaminated,
            "warmup_compile": warmup_compile,
            "warmup_zero_update": warmup_zero_update,
            "train_updates_logged": updates_logged,
            "representative": bool(update_phase and not contaminated and not warmup_compile
                                   and not warmup_zero_update),
            "line_to": cur["line"][:80],
        })

    return {
        "writes": writes,
        "eval_windows": eval_windows,
        "compiles": compiles,
        "phases": phases,
        "first_eval_t": first_eval_t,
        "last_write": last_write,
        "intervals": intervals,
    }


def gate_tier(sps: float | None) -> tuple[str, str]:
    if sps is None or (isinstance(sps, float) and math.isnan(sps)):
        return "NO_DATA", "测不到足够的数据，不能做门禁判定"
    if sps >= TIER_LOCAL_FULL:
        return "PASS_LOCAL_FULL", "本地跑完整预算 5.1e5 步"
    if sps >= TIER_DEGRADED:
        return "DEGRADED_LOCAL", "先降到 1e5 步出曲线，完整预算改投 HPC"
    return "STOP", "停，改走 WSL2（路线 A）或 HPC（路线 B）"


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="E0 吞吐/显存/门禁报告")
    ap.add_argument("--arm", default="E0")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--budget", type=float, default=FULL_BUDGET_STEPS, help="完整预算步数")
    ap.add_argument("--evals-per-run", type=int, default=None,
                    help="满预算下的 eval 次数，默认按 eval_every 推算")
    ap.add_argument("--eval-every", type=float, default=DESIGN_EVAL_EVERY)
    ap.add_argument("--work", type=Path, default=WORK_DIR)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    args.work = args.work.resolve()
    logdir = args.work / "logdir" / args.arm / f"s{args.seed}"
    meta_path = logdir / "run_meta.json"
    if not meta_path.exists():
        log(f"找不到 {meta_path}，E0 还没跑")
        return 2
    meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))

    analysis = analyse(parse_stdout(logdir / "harness" / "stdout_timed.jsonl")["events"])
    wall_sec = float(meta.get("wall_sec") or 0.0)
    actual_steps = int(meta.get("actual_steps") or 0)
    exit_code = meta.get("exit_code")
    target_steps = int(meta.get("steps_budget") or actual_steps)

    eval_windows = analysis["eval_windows"]
    eval_sec_total = sum(w["sec"] for w in eval_windows)
    eval_sec_mean = (eval_sec_total / len(eval_windows)) if eval_windows else float("nan")

    startup_sec = analysis["first_eval_t"]
    last_write = analysis["last_write"]
    loop_sec = (last_write["t"] - analysis["first_eval_t"]) if last_write else float("nan")
    steps_in_loop = last_write["step"] if last_write else 0
    # 最后一个 [step] 写点之后还有一点尾巴（补步 + 存 latest.pt），单列，不从训练时间里扣
    tail_sec = round(wall_sec - (last_write["t"] if last_write else 0.0), 2) if last_write else float("nan")
    evals_inside_loop = sum(1 for w in eval_windows if last_write and w["start_t"] <= last_write["t"])
    eval_sec_in_loop = sum(w["sec"] for w in eval_windows if last_write and w["start_t"] <= last_write["t"])
    train_only_sec = round(loop_sec - eval_sec_in_loop, 2) if last_write else float("nan")

    sps_train_loop = (steps_in_loop / train_only_sec) if train_only_sec and train_only_sec > 0 else float("nan")
    sps_wall = (actual_steps / wall_sec) if wall_sec > 0 else float("nan")

    clean_update = [i for i in analysis["intervals"] if i["representative"]]
    excluded = [i for i in analysis["intervals"] if i["update_phase"] and not i["representative"]]
    sps_values = [i["sps"] for i in clean_update]
    sps_interval_median = percentile(sps_values, 0.5) if sps_values else float("nan")
    sps_interval_p10 = percentile(sps_values, 0.10) if sps_values else float("nan")
    sps_interval_p90 = percentile(sps_values, 0.90) if sps_values else float("nan")

    # ---- 显存 -------------------------------------------------------------- #
    gpu = dict(meta.get("gpu") or {})
    gpu_samples_path = logdir / "harness" / "gpu_samples.jsonl"
    if gpu_samples_path.exists():
        samples = []
        for raw in gpu_samples_path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                samples.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
        if samples:
            mem = [s["mem_used_mib"] for s in samples]
            util = [s["util_pct"] for s in samples]
            gpu.update({
                "n_samples": len(samples),
                "gpu_name": samples[0]["name"],
                "mem_total_mib": samples[0]["mem_total_mib"],
                "mem_peak_mib": max(mem),
                "mem_median_mib": percentile(mem, 0.5),
                "util_median_pct": percentile(util, 0.5),
                "util_max_pct": max(util),
            })
            baseline = gpu.get("mem_baseline_mib")
            if baseline is not None:
                gpu["mem_peak_over_baseline_mib"] = round(max(mem) - float(baseline), 1)
                gpu["peak_pct_of_total"] = round(100.0 * max(mem) / samples[0]["mem_total_mib"], 1)

    # ---- 预计墙钟 ---------------------------------------------------------- #
    evals_per_run = args.evals_per_run
    if evals_per_run is None:
        evals_per_run = int(math.ceil(args.budget / args.eval_every)) + 1  # 含 step 0 那次
    conservative = min(x for x in (sps_train_loop, sps_interval_median) if x and not math.isnan(x)) \
        if any(x and not math.isnan(x) for x in (sps_train_loop, sps_interval_median)) else float("nan")
    optimistic = max(x for x in (sps_train_loop, sps_interval_median) if x and not math.isnan(x)) \
        if any(x and not math.isnan(x) for x in (sps_train_loop, sps_interval_median)) else float("nan")

    def project(sps: float) -> dict | None:
        if not sps or math.isnan(sps):
            return None
        train_h = args.budget / sps / 3600.0
        eval_h = (evals_per_run * eval_sec_mean / 3600.0) if not math.isnan(eval_sec_mean) else 0.0
        per_seed = train_h + eval_h
        return {
            "steps_per_sec": round(sps, 3),
            "train_hours": round(train_h, 3),
            "eval_hours": round(eval_h, 3),
            "per_seed_hours": round(per_seed, 3),
            "three_seeds_hours": round(3 * per_seed, 3),
            "six_seeds_hours": round(6 * per_seed, 3),
        }

    projection = {
        "budget_steps": args.budget,
        "evals_per_run": evals_per_run,
        "eval_sec_mean_from_E0": None if math.isnan(eval_sec_mean) else round(eval_sec_mean, 2),
        "conservative": project(conservative),
        "optimistic": project(optimistic),
        "note": "conservative = min(训练循环均速, 更新期区间中位数)；optimistic = max(同样两者)",
    }

    verdict_code, verdict_text = gate_tier(conservative)
    # 两条口径跨档时明确标出来，别让分档被一个好看的数蒙过去
    tier_train, _ = gate_tier(sps_train_loop)
    tier_interval, _ = gate_tier(sps_interval_median)
    ambiguous = tier_train != tier_interval

    report = {
        "generated_at": now_iso(),
        "generated_by": "e0_gate.py",
        "arm": args.arm,
        "seed": args.seed,
        "source": {
            "logdir": str(logdir),
            "run_meta": str(meta_path),
            "stdout_timed": str(logdir / "harness" / "stdout_timed.jsonl"),
            "gpu_samples": str(gpu_samples_path),
            "metrics_jsonl": str(logdir / "metrics.jsonl"),
            "command": meta.get("command_str"),
            "git_commit": meta.get("git_commit"),
            "torch_version": meta.get("torch_version"),
            "started_at": meta.get("started_at"),
            "finished_at": meta.get("finished_at"),
            "power": meta.get("power"),
            "host": meta.get("host"),
        },
        "run": {
            "exit_code": exit_code,
            "completed": meta.get("completed"),
            "budget_steps": target_steps,
            "actual_steps": actual_steps,
            "eval_points": meta.get("eval_points"),
            "wall_sec": wall_sec,
            "budget_reached_ratio": meta.get("budget_reached_ratio"),
        },
        "timing": {
            "wall_sec": wall_sec,
            "startup_sec_until_first_eval": round(startup_sec, 2),
            "loop_sec": round(loop_sec, 2) if last_write else None,
            "eval_count": len(eval_windows),
            "eval_sec_total": round(eval_sec_total, 2),
            "eval_sec_each": [w["sec"] for w in eval_windows],
            "eval_sec_mean": None if math.isnan(eval_sec_mean) else round(eval_sec_mean, 2),
            "train_only_sec": train_only_sec,
            "tail_after_last_write_sec": tail_sec,
            "steps_in_loop": steps_in_loop,
            "compile_warmup_interval": next(
                ({"step_from": i["step_from"], "step_to": i["step_to"], "sec": i["sec"]}
                 for i in excluded if i["warmup_compile"]), None),
        },
        "throughput": {
            "sps_train_loop": None if math.isnan(sps_train_loop) else round(sps_train_loop, 3),
            "sps_wall_end_to_end": None if math.isnan(sps_wall) else round(sps_wall, 3),
            "sps_interval_median": None if math.isnan(sps_interval_median) else round(sps_interval_median, 3),
            "sps_interval_p10": None if math.isnan(sps_interval_p10) else round(sps_interval_p10, 3),
            "sps_interval_p90": None if math.isnan(sps_interval_p90) else round(sps_interval_p90, 3),
            "n_clean_update_intervals": len(clean_update),
            "n_excluded_intervals": [
                {"step_from": i["step_from"], "step_to": i["step_to"],
                 "reason": ("eval" if i["contaminated_by_eval"] else
                            "torch.compile 预热" if i["warmup_compile"] else
                            "pretrain=0 的空 update" if i["warmup_zero_update"] else "其他")}
                for i in excluded
            ],
            "fps_logged": meta.get("fps_logged"),
            "last_update_count": meta.get("last_update_count"),
            "intervals": analysis["intervals"],
        },
        "gpu": gpu,
        "projection": projection,
        "gate": {
            "thresholds": {"local_full": TIER_LOCAL_FULL, "degraded": TIER_DEGRADED},
            "tier_train_loop": tier_train,
            "tier_interval": tier_interval,
            "tiers_disagree": ambiguous,
            "verdict": verdict_code,
            "decision": verdict_text,
            "basis": "保守口径 = min(训练循环均速, 更新期区间中位数)",
            "measured_sps_conservative": None if math.isnan(conservative) else round(conservative, 3),
        },
        "caveats": [
            "显存是 nvidia-smi 的设备级占用（含 CUDA 上下文/桌面底数），不是 torch.cuda.max_memory_allocated。",
            "steps/s 里的 step 是 env 侧步数（每轮 += env_num×action_repeat = 32），与 trainer.steps 的预算同口径。",
            "E0 预算只有 5000 步，更新从 step > 2080 才开始（trainer.py:170）；更新期区间样本数见 n_clean_update_intervals。",
            "统计里剔除了三类区间：eval 落在其中的、torch.compile 首次编译所在的、pretrain=0 吃掉的那一次空 update（见 n_excluded_intervals）。",
            "本次 E0 额外加了 trainer.update_log_every=250（只改日志密度，不改训练逻辑），否则 5000 步里只有一个 fps 采样点。",
            "若 tiers_disagree 为 true，说明样本太少或波动大，需要更长的第二探针才能定档。",
        ],
    }

    out_path = args.out or (args.work / "results" / "E0_throughput.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print("=== E0 吞吐探针 ===")
    print(f"退出码 {exit_code} / 实际 {actual_steps} 步 / 预算 {target_steps} / 墙钟 {wall_sec:.1f}s")
    print(f"启动 {startup_sec:.1f}s ｜ 循环 {loop_sec:.1f}s（其中 eval {eval_sec_total:.1f}s / {len(eval_windows)} 次，"
          f"均值 {eval_sec_mean:.1f}s）｜ 训练 {train_only_sec:.1f}s")
    print(f"吞吐：训练循环 {sps_train_loop:.2f} steps/s ｜ 更新期区间中位数 {sps_interval_median:.2f} "
          f"(p10 {sps_interval_p10:.2f} / p90 {sps_interval_p90:.2f}, n={len(sps_values)}) ｜ 端到端 {sps_wall:.2f}")
    if gpu.get("mem_peak_mib"):
        print(f"显存：峰值 {gpu['mem_peak_mib']:.0f} / {gpu['mem_total_mib']:.0f} MiB "
              f"({gpu.get('peak_pct_of_total', float('nan')):.1f}%)，底数 {gpu.get('mem_baseline_mib')} MiB")
    cons = projection["conservative"]
    if cons:
        print(f"预计：单 seed {cons['per_seed_hours']:.2f} h（训练 {cons['train_hours']:.2f} + eval {cons['eval_hours']:.2f}）"
              f"｜ 3 seeds {cons['three_seeds_hours']:.1f} h ｜ 6 seeds {cons['six_seeds_hours']:.1f} h")
    print(f"门禁：{verdict_code} —— {verdict_text}（两条口径：{tier_train} / {tier_interval}"
          f"{'，跨档，需更长探针' if ambiguous else ''}）")
    print(f"→ {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
