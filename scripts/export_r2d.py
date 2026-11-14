#!/usr/bin/env python
"""export_r2d.py —— tensorboard event / metrics.jsonl → results\<algo>-<tag>-s<seed>.jsonl

为什么要这层：r2dreamer 用 torch.utils.tensorboard.SummaryWriter 写 event 文件，
本项目的硬约束是「结果表由脚本从 JSONL 生成、禁止手打」，所以必须有导出环节。

输出格式（沿用既有审计口径）：
    首行  {"kind": "config", ...}     hydra config 扁平化 + git commit + torch 版本
    中段  {"kind": "eval",  "step": N, "eval_score": x, "eval_length": n, ...}
          {"kind": "train", "step": N, "train/...": v, "fps/fps": f, ...}
    末行  {"kind": "summary", ...}    n_eval / 最终分 / 最好分 / 步数 / 墙钟

数据源优先级：
    1) logdir\metrics.jsonl  —— Logger.write() 直接落的 JSONL（tools.py:226），逐条与 tensorboard 同源
    2) tensorboard EventAccumulator —— 兜底（metrics.jsonl 不在时）

用法：
    python export_r2d.py --arm E1 --algo dreamer   --task-tag dmcww --seeds 0 1 2
    python export_r2d.py --arm E2 --algo r2dreamer --task-tag dmcww --seeds 0 1 2
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

WORK_DIR = Path(__file__).resolve().parent
DEFAULT_D3_ROOT = Path(r"C:\Users\10348\Desktop\超越之径\D3")

EVAL_KEY = "episode/eval_score"
EVAL_LEN_KEY = "episode/eval_length"
TRAIN_RETURN_KEY = "episode/score"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[export {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def flatten(obj, prefix: str = "", out: dict | None = None) -> dict:
    if out is None:
        out = {}
    if isinstance(obj, dict):
        for key, val in obj.items():
            flatten(val, f"{prefix}.{key}" if prefix else str(key), out)
    elif isinstance(obj, (list, tuple)):
        out[prefix] = json.dumps(obj, ensure_ascii=False)
    elif isinstance(obj, (str, int, float, bool)) or obj is None:
        out[prefix] = obj
    else:
        out[prefix] = str(obj)
    return out


def load_hydra_config(logdir: Path) -> tuple[dict, list[str], str]:
    """返回 (扁平化 config, overrides 原文列表, 来源说明)。"""
    cfg_path = logdir / ".hydra" / "config.yaml"
    ov_path = logdir / ".hydra" / "overrides.yaml"
    overrides: list[str] = []
    if ov_path.exists():
        overrides = [
            line.strip()
            for line in ov_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        ]
    source = "none"
    flat: dict = {}
    if cfg_path.exists():
        raw = cfg_path.read_text(encoding="utf-8", errors="replace")
        try:  # omegaconf 能正确解析 hydra 写出来的 yaml
            from omegaconf import OmegaConf

            container = OmegaConf.to_container(OmegaConf.load(str(cfg_path)), resolve=True)
            flat = flatten(container)
            source = ".hydra/config.yaml (omegaconf)"
        except Exception:  # noqa: BLE001
            try:
                import yaml

                flat = flatten(yaml.safe_load(raw))
                source = ".hydra/config.yaml (pyyaml)"
            except Exception:  # noqa: BLE001
                flat = {"_raw_yaml": raw}
                source = ".hydra/config.yaml (raw)"
    return flat, overrides, source


def torch_version_of(py: Path | None) -> str:
    if py is None:
        return ""
    try:
        res = subprocess.run(
            [str(py), "-c", "import torch;print(torch.__version__)"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        return res.stdout.strip() if res.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# 读数据
# --------------------------------------------------------------------------- #
def read_metrics_jsonl(path: Path) -> list[dict]:
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def read_event_files(logdir: Path) -> dict[str, list[tuple[float, float]]]:
    """兜底：EventAccumulator 按 tag 取 (step, value)。返回 {tag: [(step, value), ...]}。"""
    from tensorboard.backend.event_processing.event_accumulator import EventAccumulator

    acc = EventAccumulator(str(logdir), size_guidance={"scalars": 0})
    acc.Reload()
    tags = acc.Tags().get("scalars", [])
    out = {}
    for tag in tags:
        out[tag] = [(float(ev.step), float(ev.value)) for ev in acc.Scalars(tag)]
    return out


def records_from_events(event_scalars: dict[str, list[tuple[float, float]]]) -> list[dict]:
    by_step: dict[float, dict] = {}
    for tag, points in event_scalars.items():
        for step, value in points:
            by_step.setdefault(step, {})[tag] = value
    return [{"step": step, **vals} for step, vals in sorted(by_step.items())]


# --------------------------------------------------------------------------- #
# 逐 seed 导出
# --------------------------------------------------------------------------- #
def classify(rec: dict) -> str:
    keys = set(rec)
    if EVAL_KEY in keys:
        return "eval"
    if any(k.startswith("train/") for k in keys) or "fps/fps" in keys:
        return "train"
    if TRAIN_RETURN_KEY in keys:
        return "train"
    return "misc"


def export_seed(args, seed: int) -> dict:
    logdir = (args.work / "logdir" / args.arm / f"s{seed}").resolve()
    if not logdir.exists():
        raise FileNotFoundError(f"logdir 不存在：{logdir}")

    metrics_path = logdir / "metrics.jsonl"
    run_meta_path = logdir / "run_meta.json"
    run_meta: dict = {}
    if run_meta_path.exists():
        try:
            run_meta = json.loads(run_meta_path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            run_meta = {}

    if metrics_path.exists() and metrics_path.stat().st_size > 0:
        raw_records = read_metrics_jsonl(metrics_path)
        source = "metrics.jsonl"
    else:
        raw_records = records_from_events(read_event_files(logdir))
        source = "tensorboard-event"

    config_flat, overrides, cfg_source = load_hydra_config(logdir)
    torch_ver = (run_meta.get("torch_version") or "") or torch_version_of(args.python)

    out_file = args.work / "results" / f"{args.algo}-{args.task_tag}-s{seed}.jsonl"
    out_file.parent.mkdir(parents=True, exist_ok=True)

    evals: list[dict] = []
    trains: list[dict] = []
    misc: list[dict] = []
    for rec in raw_records:
        kind = classify(rec)
        step = rec.get("step")
        if kind == "eval":
            entry = {
                "kind": "eval",
                "step": int(step) if step is not None else None,
                "eval_score": rec.get(EVAL_KEY),
                "eval_length": rec.get(EVAL_LEN_KEY),
            }
            for key, val in rec.items():
                if key.startswith("episode/eval_") and key not in (EVAL_KEY, EVAL_LEN_KEY):
                    entry[key.split("/", 1)[1]] = val
            evals.append(entry)
        elif kind == "train":
            entry = {"kind": "train", "step": int(step) if step is not None else None}
            entry.update({k: v for k, v in rec.items() if k != "step"})
            trains.append(entry)
        else:
            entry = {"kind": "misc", "step": int(step) if step is not None else None}
            entry.update({k: v for k, v in rec.items() if k != "step"})
            misc.append(entry)

    evals.sort(key=lambda r: (r["step"] is None, r["step"]))
    trains.sort(key=lambda r: (r["step"] is None, r["step"]))
    if not evals:
        raise RuntimeError(f"{logdir} 里没有 {EVAL_KEY} 记录：训练没跑到第一次 eval，或日志格式变了")

    scores = [e["eval_score"] for e in evals if e["eval_score"] is not None]
    best_idx = max(range(len(evals)), key=lambda i: evals[i]["eval_score"] if evals[i]["eval_score"] is not None else -1e18)

    # 预算与判据 1 的窗口写进 JSONL 的 config 行（主窗口 A2 · 2026-11-02 要求）：
    # 这样任何一份 JSONL 自己就能说清"多少步预算、按哪个窗口判"，事后不必回查 logdir。
    budget_steps = run_meta.get("steps_budget")
    if budget_steps is None:
        try:
            budget_steps = int(float(config_flat.get("env.steps")))
        except (TypeError, ValueError):
            budget_steps = int(evals[-1]["step"]) if evals[-1].get("step") else None
    try:
        from table_r2d import criterion1_window  # 与出表脚本共用同一实现，避免口径漂移

        window_W = criterion1_window(budget_steps) if budget_steps else None
        window_rule = "W = max(2000, 预算//5)；前置条件 预算 ≥ 2.5·W（A2 2026-11-02）"
    except Exception:  # noqa: BLE001
        window_W = None
        window_rule = "table_r2d 不可导入，未计算窗口"

    config_line = {
        "kind": "config",
        "algo": args.algo,
        "arm": args.arm,
        "task_tag": args.task_tag,
        "seed": seed,
        "logdir": str(logdir),
        "data_source": source,
        "config_source": cfg_source,
        "hydra_overrides": overrides,
        "git_commit": run_meta.get("git_commit", ""),
        "torch_version": torch_ver,
        "command": run_meta.get("command_str", ""),
        "env_steps_budget": budget_steps,
        "budget_steps": budget_steps,
        "window_W": window_W,
        "window_rule": window_rule,
        "generated_at": now_iso(),
        "generated_by": "export_r2d.py",
        "config": config_flat,
    }

    summary = {
        "kind": "summary",
        "algo": args.algo,
        "arm": args.arm,
        "task_tag": args.task_tag,
        "seed": seed,
        "data_source": source,
        "n_eval": len(evals),
        "n_train": len(trains),
        "n_misc": len(misc),
        "first_eval_step": evals[0]["step"],
        "last_eval_step": evals[-1]["step"],
        "n_eval_points": len(scores),
        "final_eval_step": evals[-1]["step"],
        "final_eval_score": evals[-1]["eval_score"],
        "best_eval_score": evals[best_idx]["eval_score"],
        "best_eval_step": evals[best_idx]["step"],
        "mean_eval_score": (sum(scores) / len(scores)) if scores else None,
        "actual_steps": run_meta.get("actual_steps"),
        "wall_sec": run_meta.get("wall_sec"),
        "exit_code": run_meta.get("exit_code"),
        "gpu_mem_peak_mib": (run_meta.get("gpu") or {}).get("mem_peak_mib"),
        "logdir": str(logdir),
        "generated_at": now_iso(),
        "generated_by": "export_r2d.py",
    }

    lines = [json.dumps(config_line, ensure_ascii=False)]
    lines += [json.dumps(r, ensure_ascii=False) for r in evals]
    lines += [json.dumps(r, ensure_ascii=False) for r in trains]
    lines += [json.dumps(r, ensure_ascii=False) for r in misc]
    lines.append(json.dumps(summary, ensure_ascii=False))

    out_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 自检：末行必须是 summary（既有审计口径）
    last = json.loads(out_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    if last.get("kind") != "summary":
        raise AssertionError(f"{out_file} 末行不是 summary")

    log(
        f"s{seed}: {len(evals)} 个 eval 点（{evals[0]['step']}→{evals[-1]['step']} 步）"
        f" 最终 {summary['final_eval_score']:.2f}  最好 {summary['best_eval_score']:.2f}"
        f"  源={source}  → {out_file.name}"
    )
    return {
        "seed": seed,
        "out": str(out_file),
        "n_eval": len(evals),
        "final_eval_score": summary["final_eval_score"],
        "best_eval_score": summary["best_eval_score"],
        "source": source,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="event/metrics.jsonl → results/*.jsonl")
    ap.add_argument("--arm", required=True, help="logdir 下的实验臂目录名，例如 E1")
    ap.add_argument("--algo", required=True, choices=["dreamer", "r2dreamer", "infonce", "dreamerpro"])
    ap.add_argument("--task-tag", default="dmcww", help="文件名里的任务标签，DMC walker-walk 用 dmcww")
    ap.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2])
    ap.add_argument("--work", type=Path, default=WORK_DIR)
    ap.add_argument("--d3-root", type=Path, default=DEFAULT_D3_ROOT)
    args = ap.parse_args(argv)

    args.work = args.work.resolve()
    py = (args.d3_root / "venv_r2d" / "Scripts" / "python.exe").resolve()
    args.python = py if py.exists() else None

    results = []
    for seed in args.seeds:
        results.append(export_seed(args, seed))

    out = args.work / "results" / f"export_{args.algo}_{args.task_tag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"generated_at": now_iso(), "algo": args.algo, "arm": args.arm, "seeds": results},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log(f"导出小结 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
