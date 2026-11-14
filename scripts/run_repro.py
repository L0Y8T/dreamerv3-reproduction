#!/usr/bin/env python
"""run_repro.py —— 跑一个 (rep_loss, seed) 组合，落到 logdir\<tag>\s<seed>\。

设计约束（来自 交接_下一步.md §3.1）：
  * 子进程解释器固定为 D3\venv_r2d\Scripts\python.exe，cwd = D3\repos\r2dreamer
  * 子进程环境必须带 PYTHONIOENCODING=utf-8 + PYTHONUTF8=1；
    父进程读子进程输出一律 errors="replace"（2026-10-28 被 GBK 打死过一次）
  * 每个 seed 跑完写 run_meta.json（exit code、实际步数、墙钟秒、命令原文）
  * 断点续跑：run_meta.json 存在 且 exit==0 且 实际步数 ≈ 预算×0.99 ⇒ 跳过

额外做了两件交接文档要求之外、但 E0 门禁需要的事（不改训练逻辑）：
  * --sample-gpu：父进程每秒采一次 nvidia-smi，写 harness\gpu_samples.jsonl（显存峰值在这里）
  * 子进程每一行 stdout/stderr 都带相对时间戳写 harness\stdout_timed.jsonl
    （eval 窗口 vs 训练窗口的时间切分靠它，否则 steps/s 会被 eval 和启动开销拉低）

用法：
    python run_repro.py --tag E0 --rep-loss dreamer --seed 0 \
        --steps 5000 --eval-every 2500 --update-log-every 250 --sample-gpu
    python run_repro.py --tag E1 --rep-loss dreamer --seeds 0 1 2
    python run_repro.py --tag E2 --rep-loss r2dreamer --seeds 0 1 2
    python run_repro.py ... --dry-run          # 只打印命令，不启动
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 老 Python / 被重定向时忽略
        pass

WORK_DIR = Path(__file__).resolve().parent
DEFAULT_D3_ROOT = Path(r"C:\Users\10348\Desktop\超越之径\D3")

FIXED_OVERRIDES = [
    "env=dmc_proprio",
    "env.task=dmc_walker_walk",
    "model=size12M",
    # 2026-11-02 03:1x 实测：configs\model\_base_.yaml 里 compile: True，
    # 而本机 venv_r2d 没有 triton（Windows 上官方也不发 triton），走到第一次真正的梯度更新必崩：
    #   torch._dynamo.exc.BackendCompilerFailed: backend='inductor' raised:
    #   RuntimeError: Cannot find a working triton installation.
    # 这是环境约束，不是超参：E1/E2/后续所有 arm 都必须带这一条，两臂才逐项相同。
    "model.compile=False",
]

COMPLETION_TOLERANCE = 0.99


# --------------------------------------------------------------------------- #
# 全局单训练进程互斥锁
#   2026-11-02 03:0x 实测到**两个 agent 同时在这台机器上按同一份任务跑**，
#   两边的 run_repro.py 都会把目标 seed 的 logdir 改名归档再新建 —— 撞上就是互相毁数据。
#   所以锁放在这里（而不是放在调用方脚本），任何一方用同一个 run_repro.py 都会被序列化。
#   已知的进程是活的就不抢；持锁进程已经死了就接管（机器重启/进程被杀后不会永久卡死）。
# --------------------------------------------------------------------------- #
def _pid_alive(pid: int) -> bool:
    """Windows 上判断 pid 是否还活着（os.kill(pid,0) 在这边语义不可靠，用 OpenProcess）。"""
    if pid <= 0:
        return False
    try:
        import ctypes

        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not handle:
            return False
        try:
            # WAIT_TIMEOUT(258) 说明进程还活着
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 258
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return False


class RunLock:
    def __init__(self, path: Path, wait_sec: float):
        self.path = path
        self.wait_sec = wait_sec
        self.held = False
        self._announced = False

    def _holder(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            return {}

    def acquire(self) -> bool:
        deadline = time.time() + max(self.wait_sec, 0)
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"pid": os.getpid(), "since": now_iso(),
                               "argv": " ".join(sys.argv)}, fh, ensure_ascii=False)
                self.held = True
                return True
            except FileExistsError:
                info = self._holder()
                pid = int(info.get("pid") or 0)
                if not _pid_alive(pid):
                    log(f"发现残留锁（pid {pid} 已不在），接管")
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                if time.time() >= deadline:
                    log(f"锁被 pid {pid}（{info.get('since')}）占着，等待超时 → 本次不启动训练")
                    return False
                if not self._announced:
                    log(f"另一个训练进程在跑（pid {pid}，{info.get('since')} 起），排队等它结束…")
                    self._announced = True
                time.sleep(10)

    def release(self) -> None:
        if self.held:
            try:
                self.path.unlink()
            except OSError:
                pass
            self.held = False


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[run_repro {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def file_nonempty(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_size > 0
    except OSError:
        return False


def summarize_logdir(logdir: Path) -> dict:
    """从 logdir 里的 metrics.jsonl 数实际跑到多少步、有几个 eval 点。"""
    metrics = logdir / "metrics.jsonl"
    out = {
        "metrics_jsonl": str(metrics),
        "metrics_lines": 0,
        "max_step": 0,
        "eval_points": 0,
        "fps_logged": [],
        "has_episode_score": False,
        "last_update_count": None,
    }
    if not file_nonempty(metrics):
        return out
    steps = []
    text = metrics.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        out["metrics_lines"] += 1
        if "step" in rec and isinstance(rec["step"], (int, float)):
            steps.append(float(rec["step"]))
        if "episode/eval_score" in rec:
            out["eval_points"] += 1
        if "episode/score" in rec:
            out["has_episode_score"] = True
        if "fps/fps" in rec:
            out["fps_logged"].append(rec["fps/fps"])
        if "train/opt/updates" in rec:
            out["last_update_count"] = rec["train/opt/updates"]
    out["max_step"] = int(max(steps)) if steps else 0
    # 尾部（后 25%）写点之间的典型间隔：跑完后「最后一个写点」到真实结束最多差这么一个间隔。
    # 预算判定必须用它当容差：E0（update_log_every=250）收在 4864/5000，
    # E1（eval_every=1e4）会收在 5.0e5/5.1e5 —— 用固定的 0.99 会把两次正常跑完都判成没跑完。
    ordered = sorted(set(steps))
    tail = ordered[len(ordered) * 3 // 4:] if ordered else []
    gaps = sorted(b - a for a, b in zip(tail, tail[1:]))
    out["max_log_gap"] = int(max((b - a for a, b in zip(ordered, ordered[1:])), default=0))
    out["tail_gap_median"] = int(gaps[len(gaps) // 2]) if gaps else 0
    return out


def completion_rule(steps_budget: int | None, loginfo: dict, eval_every: int | None) -> dict:
    """跑完的判据：exit 0 + eval 点数达到预期 + 最后一个写点落在预算的日志粒度以内。

    为什么不能只看 0.99：日志只在 eval / update-log 时落盘，最后一个写点天然会早于预算
    （E1 满预算 5.1e5 时最后一个 eval 在 5.0e5 = 98.0%）。所以容差取「日志粒度」，
    再用 eval 点数的精确预期当第二道闸，防止早期崩掉被误判成跑完。
    """
    if not steps_budget:
        return {"applicable": False}
    budget = int(steps_budget)
    slack = max(int(eval_every or 0), int(loginfo.get("tail_gap_median") or 0)) + 64
    expected_evals = int((budget - 1) // int(eval_every)) + 1 if eval_every else None
    return {
        "applicable": True,
        "threshold_steps": max(budget - slack, 0),
        "slack": slack,
        "tail_gap_median": loginfo.get("tail_gap_median"),
        "max_log_gap": loginfo.get("max_log_gap"),
        "expected_eval_points": expected_evals,
        "observed_eval_points": loginfo.get("eval_points"),
        "note": "跑完 = exit 0 且 eval 点数 ≥ 预期 且 最后写点 ≥ 预算 − max(eval_every, 尾部日志间隔) − 64",
    }


def is_complete(info: dict, exit_code, loginfo: dict) -> bool:
    if not info.get("applicable") or exit_code != 0:
        return False
    if loginfo.get("max_step", 0) < info["threshold_steps"]:
        return False
    if info.get("expected_eval_points") is not None:
        return int(loginfo.get("eval_points") or 0) >= int(info["expected_eval_points"])
    return True


def git_commit(repo: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        return res.stdout.strip() if res.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def torch_version(py: Path) -> str:
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


def host_info() -> dict:
    import platform

    info = {
        "node": platform.node(),
        "platform": platform.platform(),
        "python_parent": sys.version.split()[0],
        "cpu_logical": os.cpu_count(),
    }
    return info


def power_status() -> dict:
    """记录当时是 AC 还是电池——睡眠策略只在 AC 下有效（交接文档 §5 第一条坑）。"""
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$b = Get-CimInstance Win32_Battery; "
             "if ($b) { $b.BatteryStatus } else { 'NoBattery' }"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        raw = res.stdout.strip()
    except Exception:  # noqa: BLE001
        return {"raw": "unavailable", "ac_expected": None}
    # Win32_Battery.BatteryStatus: 2 = 接交流电, 1 = 电池供电
    ac = {"2": True, "1": False}.get(raw.splitlines()[0].strip() if raw else "", None)
    return {"raw": raw, "ac_expected": ac}


# --------------------------------------------------------------------------- #
# GPU 采样
# --------------------------------------------------------------------------- #
class GpuSampler(threading.Thread):
    QUERY = "name,memory.used,memory.total,utilization.gpu"

    def __init__(self, out_path: Path, interval: float = 1.0):
        super().__init__(daemon=True)
        self.out_path = out_path
        self.interval = interval
        self._t0 = time.time()
        # 注意：不能叫 _stop —— 那是 threading.Thread 的内部方法，覆盖掉线程收尾会炸
        self._stop_evt = threading.Event()
        self.samples: list[dict] = []
        self._lock = threading.Lock()

    def _query(self) -> dict | None:
        try:
            res = subprocess.run(
                ["nvidia-smi", f"--query-gpu={self.QUERY}", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if res.returncode != 0:
                return None
            first = res.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in first.split(",")]
            if len(parts) < 4:
                return None
            return {
                "t": round(time.time() - self._t0, 3),
                "name": parts[0],
                "mem_used_mib": float(parts[1]),
                "mem_total_mib": float(parts[2]),
                "util_pct": float(parts[3]),
            }
        except Exception:  # noqa: BLE001
            return None

    def baseline(self) -> dict | None:
        """训练进程还没起来时的占用（CUDA 上下文/桌面占用的底数）。"""
        return self._query()

    def run(self) -> None:
        with self.out_path.open("w", encoding="utf-8") as fh:
            while not self._stop_evt.is_set():
                rec = self._query()
                if rec is not None:
                    with self._lock:
                        self.samples.append(rec)
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fh.flush()
                self._stop_evt.wait(self.interval)

    def stop(self) -> None:
        self._stop_evt.set()
        self.join(timeout=10)

    def summary(self) -> dict:
        with self._lock:
            samples = list(self.samples)
        if not samples:
            return {"n_samples": 0}
        mem = [s["mem_used_mib"] for s in samples]
        util = [s["util_pct"] for s in samples]
        peak = max(samples, key=lambda s: s["mem_used_mib"])
        return {
            "n_samples": len(samples),
            "gpu_name": samples[0]["name"],
            "mem_total_mib": samples[0]["mem_total_mib"],
            "mem_peak_mib": max(mem),
            "mem_peak_t": peak["t"],
            "mem_median_mib": sorted(mem)[len(mem) // 2],
            "util_max_pct": max(util),
            "util_median_pct": sorted(util)[len(util) // 2],
        }


# --------------------------------------------------------------------------- #
# 命令拼装
# --------------------------------------------------------------------------- #
def build_overrides(args, seed: int, logdir: Path) -> list[str]:
    overrides = list(FIXED_OVERRIDES)
    overrides.append(f"model.rep_loss={args.rep_loss}")
    overrides.append(f"seed={seed}")
    if args.steps is not None:
        overrides.append(f"env.steps={args.steps}")
    if args.eval_every is not None:
        overrides.append(f"trainer.eval_every={args.eval_every}")
    if args.update_log_every is not None:
        overrides.append(f"trainer.update_log_every={args.update_log_every}")
    if args.device is not None:
        overrides.append(f"device={args.device}")
    overrides.extend(args.extra)
    # 必须加引号：Hydra 的 override 词法分析器不认非 ASCII 字符，未加引号的中文路径会报
    # LexerNoViableAltException（2026-11-02 实测，见交接文档 §5 之外的第一个新坑）
    overrides.append(f"logdir='{logdir.as_posix()}'")
    return overrides


def build_cmd(args, seed: int, logdir: Path) -> list[str]:
    return [str(args.python), "train.py", *build_overrides(args, seed, logdir)]


def run_one(args, seed: int, env_meta: dict, py_version: str, commit: str) -> dict:
    logdir = args.work / "logdir" / args.tag / f"s{seed}"
    run_meta_path = logdir / "run_meta.json"
    cmd = build_cmd(args, seed, logdir)

    # ---- 断点续跑判断 -------------------------------------------------- #
    if not args.force and run_meta_path.exists():
        try:
            prev = json.loads(run_meta_path.read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError:
            prev = {}
        prev_steps = int(prev.get("actual_steps") or 0)
        prev_loginfo = summarize_logdir(logdir)
        prev_rule = completion_rule(args.steps, prev_loginfo, args.eval_every or prev.get("eval_every"))
        if prev.get("exit_code") == 0 and is_complete(prev_rule, prev.get("exit_code"), prev_loginfo):
            log(f"SKIP s{seed}：exit=0，最后写点 {prev_steps} ≥ 判定线 {prev_rule.get('threshold_steps')}，"
                f"eval 点 {prev_loginfo.get('eval_points')}/{prev_rule.get('expected_eval_points')}（预算 {args.steps}）")
            if prev.get("completed") is not True:
                # 老判定（固定 0.99）在日志粒度粗时会把跑完的 seed 记成没跑完，这里就地校正并留痕
                prev["completed"] = True
                prev["completion_rule"] = prev_rule
                prev["rechecked_at"] = now_iso()
                run_meta_path.write_text(json.dumps(prev, ensure_ascii=False, indent=2), encoding="utf-8")
                log(f"s{seed} 的 run_meta.json 已按新判定线校正为 completed=true")
            return {"seed": seed, "status": "skipped", "run_meta": prev}

    if args.dry_run:
        log("DRY-RUN 命令：" + " ".join(cmd))
        return {"seed": seed, "status": "dry-run", "command": cmd, "logdir": str(logdir)}

    # ---- logdir 清理（保持工作目录干净，不当场删证据） ---------------- #
    if logdir.exists() and any(logdir.iterdir()):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archived = logdir.parent / f"s{seed}__prev{stamp}"
        if args.keep_old_logdir:
            shutil.move(str(logdir), str(archived))
            log(f"旧 logdir 已改名归档：{archived.name}（不删，留着对比）")
        else:
            shutil.rmtree(logdir)
            log("旧 logdir 已删除（--keep-old-logdir 关闭）")
    logdir.mkdir(parents=True, exist_ok=True)
    harness = logdir / "harness"
    harness.mkdir(exist_ok=True)
    stdout_timed = harness / "stdout_timed.jsonl"
    gpu_path = harness / "gpu_samples.jsonl"

    # ---- 子进程环境（GBK 坑） ----------------------------------------- #
    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONUNBUFFERED"] = "1"
    child_env["HYDRA_FULL_ERROR"] = "1"

    log(f"START s{seed}  rep_loss={args.rep_loss}  steps={args.steps}  logdir={logdir}")
    log("CMD " + " ".join(cmd))

    sampler = GpuSampler(gpu_path, interval=args.gpu_interval) if args.sample_gpu else None
    gpu_baseline = sampler.baseline() if sampler else None
    if sampler:
        sampler.start()

    t0 = time.time()
    started_at = now_iso()
    exit_code = None
    tail: list[str] = []
    with stdout_timed.open("w", encoding="utf-8") as tfile:
        proc = subprocess.Popen(
            cmd,
            cwd=str(args.repo),
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",  # 关键：父进程侧不因解码把整条链带死
            bufsize=1,
        )
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                elapsed = round(time.time() - t0, 3)
                tfile.write(json.dumps({"t": elapsed, "line": line.rstrip("\n")}, ensure_ascii=False) + "\n")
                tfile.flush()
                stripped = line.rstrip("\n")
                print(f"  | {stripped}", flush=True)
                tail.append(stripped)
                if len(tail) > 40:
                    tail.pop(0)
            exit_code = proc.wait()
        except KeyboardInterrupt:
            log("收到 Ctrl-C，终止子进程树")
            proc.kill()
            exit_code = proc.wait()
            raise
    wall_sec = round(time.time() - t0, 3)
    finished_at = now_iso()

    gpu_summary = {}
    if sampler:
        sampler.stop()
        gpu_summary = sampler.summary()
        if gpu_baseline:
            gpu_summary["mem_baseline_mib"] = gpu_baseline["mem_used_mib"]
            gpu_summary["mem_peak_over_baseline_mib"] = round(
                gpu_summary.get("mem_peak_mib", 0) - gpu_baseline["mem_used_mib"], 1
            )
        gpu_summary["samples_file"] = str(gpu_path)

    loginfo = summarize_logdir(logdir)
    rule = completion_rule(args.steps, loginfo, args.eval_every)
    completed = is_complete(rule, exit_code, loginfo)

    meta = {
        "tag": args.tag,
        "rep_loss": args.rep_loss,
        "seed": seed,
        "steps_budget": args.steps,
        "eval_every": args.eval_every,
        "update_log_every": args.update_log_every,
        "logdir": str(logdir),
        "python": str(args.python),
        "repo": str(args.repo),
        "git_commit": commit,
        "torch_version": py_version,
        "command": cmd,
        "command_str": " ".join(cmd),
        "overrides": build_overrides(args, seed, logdir),
        "child_env": {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"},
        "started_at": started_at,
        "finished_at": finished_at,
        "wall_sec": wall_sec,
        "exit_code": exit_code,
        "actual_steps": loginfo["max_step"],
        "eval_points": loginfo["eval_points"],
        "metrics_lines": loginfo["metrics_lines"],
        "fps_logged": loginfo["fps_logged"],
        "last_update_count": loginfo["last_update_count"],
        "budget_reached_ratio": (
            round(loginfo["max_step"] / args.steps, 4) if args.steps else None
        ),
        "completed": bool(completed),
        "completion_rule": rule,
        "gpu": gpu_summary,
        "power": env_meta.get("power"),
        "host": env_meta.get("host"),
        "stdout_timed": str(stdout_timed),
        "console_log": str(logdir / "console.log"),
        "tail": tail[-15:],
    }
    run_meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    if completed:
        log(f"DONE s{seed}：{loginfo['max_step']} 步 / {wall_sec:.0f}s / eval 点 {loginfo['eval_points']} / exit 0")
    else:
        log(f"NOT-COMPLETE s{seed}：exit={exit_code} 实际 {loginfo['max_step']} 步（预算 {args.steps}）")
    return {"seed": seed, "status": "completed" if completed else "incomplete", "run_meta": meta}


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="跑一个 (rep_loss, seed) 组合并落 logdir")
    ap.add_argument("--tag", required=True, help="实验臂标签，例如 E0 / E1 / E2")
    ap.add_argument("--rep-loss", required=True, choices=["dreamer", "r2dreamer", "infonce", "dreamerpro"])
    ap.add_argument("--seed", type=int, default=None, help="单个 seed")
    ap.add_argument("--seeds", type=int, nargs="+", default=None, help="串行跑多个 seed")
    ap.add_argument("--steps", type=int, default=None, help="覆盖 env.steps（trainer.steps 跟着联动）")
    ap.add_argument("--eval-every", type=int, default=None, help="覆盖 trainer.eval_every")
    ap.add_argument("--update-log-every", type=int, default=None, help="覆盖 trainer.update_log_every（只影响日志密度）")
    ap.add_argument("--device", default=None, help="覆盖 device，默认 cuda:0")
    ap.add_argument("--extra", nargs="*", default=[], help="额外 hydra 覆盖，例如 buffer.max_size=1e5")
    ap.add_argument("--work", type=Path, default=WORK_DIR, help="工作目录（默认本文件所在目录）")
    ap.add_argument("--d3-root", type=Path, default=DEFAULT_D3_ROOT, help="D3 根目录（只读）")
    ap.add_argument("--sample-gpu", action="store_true", help="训练期间采 nvidia-smi（显存峰值）")
    ap.add_argument("--gpu-interval", type=float, default=1.0, help="GPU 采样间隔秒")
    ap.add_argument("--force", action="store_true", help="忽略 run_meta.json，强制重跑")
    ap.add_argument("--keep-old-logdir", action="store_true", default=True)
    ap.add_argument("--no-keep-old-logdir", dest="keep_old_logdir", action="store_false")
    ap.add_argument("--lock-wait", type=float, default=21600.0,
                    help="等别的训练进程退出的最长秒数，默认 21600（6h）；0 = 不排队直接放弃")
    ap.add_argument("--dry-run", action="store_true", help="只打印命令不启动")
    args = ap.parse_args(argv)

    args.work = args.work.resolve()
    args.repo = (args.d3_root / "repos" / "r2dreamer").resolve()
    args.python = (args.d3_root / "venv_r2d" / "Scripts" / "python.exe").resolve()

    seeds = args.seeds if args.seeds else ([args.seed] if args.seed is not None else [0])

    if not args.dry_run:
        if not args.python.exists():
            log(f"子进程解释器不存在：{args.python}")
            return 2
        if not (args.repo / "train.py").exists():
            log(f"train.py 不存在：{args.repo}")
            return 2

    env_meta = {"host": host_info(), "power": power_status()}
    py_version = torch_version(args.python) if not args.dry_run else ""
    commit = git_commit(args.repo)
    log(f"torch={py_version or '?'} git={commit[:12] or '?'} work={args.work}")
    if env_meta["power"].get("ac_expected") is False:
        log("警告：当前是电池供电（BatteryStatus=1）。AC 下关掉的睡眠策略在电池下会重新生效。")

    results = []
    lock = None
    try:
        if not args.dry_run:
            lock = RunLock(args.work / ".run_repro.lock", args.lock_wait)
            if not lock.acquire():
                log("没拿到锁：本轮不启动训练（已有训练进程在跑）。稍后重试或按断点续跑判据跳过已完成的 seed。")
                return 3
        for seed in seeds:
            results.append(run_one(args, seed, env_meta, py_version, commit))
    finally:
        if lock is not None:
            lock.release()

    # ---- 串行调度的小结 ------------------------------------------------ #
    summary = {
        "tag": args.tag,
        "rep_loss": args.rep_loss,
        "seeds": seeds,
        "generated_at": now_iso(),
        "runs": [
            {
                "seed": r["seed"],
                "status": r["status"],
                "wall_sec": (r.get("run_meta") or {}).get("wall_sec"),
                "actual_steps": (r.get("run_meta") or {}).get("actual_steps"),
                "exit_code": (r.get("run_meta") or {}).get("exit_code"),
                "eval_points": (r.get("run_meta") or {}).get("eval_points"),
                "logdir": (r.get("run_meta") or {}).get("logdir"),
            }
            for r in results
        ],
    }
    if not args.dry_run:
        out = args.work / "results"
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"runs_{args.tag}_{args.rep_loss}.json"
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"调度小结 → {path}")

    bad = [r for r in results if r["status"] in ("incomplete",)]
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
