#!/usr/bin/env python
"""table_r2d.py —— 从 results\\<algo>-<tag>-s<seed>.jsonl 现算结果表 + 三条判据 + 曲线图。

禁止手打任何数字：本脚本里出现的每个数都来自 JSONL。
判据在跑之前就写死在 实验设计_2026-11-02.md §3，本脚本只负责照着算，不许临时换口径。

输出：
  1) 每组 n / 最终 eval_score mean±sd / 最好 eval_score mean±sd / eval 点数
  2) 判据 1（曲线真的在学）、判据 2（跨种子稳定）、判据 3（两档可分）的逐条计算
  3) figs\\curves.png（横轴 step、纵轴 eval_score、mean±sd 波段，并注明 eval 点的稀疏程度）
  4) results\\table.md + results\\criteria.json

用法：
    python table_r2d.py --algos dreamer                    --task-tag dmcww --seeds 0 1 2
    python table_r2d.py --algos dreamer r2dreamer           --task-tag dmcww --seeds 0 1 2
    python table_r2d.py --algos dreamer --window 2500       # 小规模自检用
"""

from __future__ import annotations

import argparse
import json
import math
import statistics as st
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

WORK_DIR = Path(__file__).resolve().parent
SD_RATIO_LIMIT = 0.25      # 判据 2：sd/mean < 0.25
SE_MULTIPLIER = 2.0        # 判据 1/3：> 2·SE 才算过 / 才算分开
WINDOW_FLOOR = 2000        # 判据 1 窗口下限（A2 · 2026-11-02 定的新口径）
WINDOW_FRACTION = 5        # W = 预算 // 5
WINDOW_PRECONDITION = 2.5  # 前置条件：预算 ≥ 2.5·W


def criterion1_window(budget: float) -> int:
    """判据 1 的窗口 W = max(2000, 预算 // 5)。（A2 · 2026-11-02；已同步进设计文档 §3 与交接 §3.7）

    为什么不再用固定 1e5：预算 ≤ 2e5 时，「最前 1e5 / 最后 1e5」两个窗口**都等于整条曲线**，
    d_i ≡ 0 ⇒ 判据 1 恒不通过（是口径造成的，不是没学会）。所以窗口必须随预算缩放。
    本函数与 export_r2d.py 共用同一实现，避免两处算法漂移。
    """
    return max(WINDOW_FLOOR, int(budget) // WINDOW_FRACTION)


def window_applicable(budget: float, window: float) -> bool:
    """前置条件：预算 ≥ 2.5·W，否则前后两个窗口重叠到失去意义。"""
    return budget >= WINDOW_PRECONDITION * window


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[table {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def sd(values: list[float]) -> float:
    """样本标准差 ddof=1（项目既有口径）。"""
    return st.stdev(values) if len(values) > 1 else 0.0


def fmt(value, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return "—"
        return f"{value:.{digits}f}"
    return str(value)


# --------------------------------------------------------------------------- #
# 读 JSONL
# --------------------------------------------------------------------------- #
def read_jsonl(path: Path) -> tuple[dict, list[dict], dict]:
    lines = [ln for ln in path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    records = [json.loads(ln) for ln in lines]
    config = next((r for r in records if r.get("kind") == "config"), {})
    summary = next((r for r in reversed(records) if r.get("kind") == "summary"), {})
    evals = [r for r in records if r.get("kind") == "eval" and r.get("eval_score") is not None]
    evals.sort(key=lambda r: r["step"])
    return config, evals, summary


def load_group(work: Path, algo: str, task_tag: str, seeds: list[int]) -> dict:
    members = []
    missing = []
    for seed in seeds:
        path = work / "results" / f"{algo}-{task_tag}-s{seed}.jsonl"
        if not path.exists():
            missing.append(path.name)
            continue
        config, evals, summary = read_jsonl(path)
        curves = [(int(e["step"]), float(e["eval_score"])) for e in evals]
        finals = [v for s, v in curves if s == curves[-1][0]]
        members.append(
            {
                "seed": seed,
                "file": path.name,
                "curves": curves,
                "final_step": curves[-1][0],
                "final": curves[-1][1],
                "best": max(v for _, v in curves),
                "n_eval": len(curves),
                "wall_sec": summary.get("wall_sec"),
                "git_commit": config.get("git_commit"),
                "torch_version": config.get("torch_version"),
                "eval_stride": Counter(
                    [b[0] - a[0] for a, b in zip(curves, curves[1:])]
                ).most_common(1)[0][0] if len(curves) > 1 else None,
                # 观测步长永远是 32 的倍数（eval 在 step%1e4 触发，实际落在 10032 这种点上），
                # 图注要写配置值，所以把 config 里真正的 eval_every 也带出来
                "eval_every_cfg": (config.get("config") or {}).get("trainer.eval_every"),
                # 判据 1 的窗口按**声明预算**算（A2：预算 2e4 ⇒ W=4000），不是按观测末步
                # （观测末步会因 done 环境导致增量变小而差几十步，19008//5 会算成 3801）。
                "budget_declared": config.get("budget_steps") or config.get("env_steps_budget"),
            }
        )
    return {"algo": algo, "members": members, "missing": missing}


# --------------------------------------------------------------------------- #
# 三条判据
# --------------------------------------------------------------------------- #
def criterion_1(group: dict, window: float) -> dict:
    """曲线真的在学：最后 window 步均值 − 前 window 步均值 > 2·SE（跨 seed）。"""
    members = group["members"]
    if not members:
        return {"ok": None, "reason": "没有数据"}
    budget = max(m["final_step"] for m in members)
    lo_last = budget - window
    per_seed = []
    for m in members:
        first = [v for s, v in m["curves"] if s < window]
        last = [v for s, v in m["curves"] if s >= lo_last]
        if len(first) < 1 or len(last) < 1:
            per_seed.append({"seed": m["seed"], "delta": None,
                             "n_first": len(first), "n_last": len(last)})
            continue
        mf, ml = mean(first), mean(last)
        per_seed.append({"seed": m["seed"], "mean_first": mf, "mean_last": ml,
                         "delta": ml - mf, "n_first": len(first), "n_last": len(last)})
    deltas = [r["delta"] for r in per_seed if r["delta"] is not None]
    if len(deltas) < 1:
        return {"ok": False, "reason": f"窗口内没有足够的 eval 点（window={window}，预算={budget}）",
                "per_seed": per_seed, "window": window, "budget": budget}
    delta_mean = mean(deltas)
    se = sd(deltas) / math.sqrt(len(deltas)) if len(deltas) > 1 else 0.0
    ratio = (delta_mean / se) if se > 0 else (math.inf if delta_mean > 0 else 0.0)
    ok = bool(len(deltas) >= 2 and se > 0 and delta_mean > SE_MULTIPLIER * se)
    return {
        "ok": ok,
        "window": window,
        "budget": budget,
        "n_seeds": len(deltas),
        "delta_mean": delta_mean,
        "se": se,
        "two_se": SE_MULTIPLIER * se,
        "ratio_delta_over_se": ratio,
        "per_seed": per_seed,
        "verdict": "曲线确实在学" if ok else "复现失败（曲线没在学，先查失败模式表，不调超参凑）",
    }


def criterion_2(group: dict) -> dict:
    """跨种子稳定：sd(最终 eval_score)/|mean| < 0.25 **且** mean > 0（设计文档 §3 判据 2 的绝对兜底）。"""
    members = group["members"]
    finals = [m["final"] for m in members]
    if len(finals) < 2:
        return {"ok": None, "n": len(finals), "reason": f"只有 {len(finals)} 个 seed（判据 2 需要 ≥2）"}
    mn, s = mean(finals), sd(finals)
    # mean <= 0 时 sd/mean 会爆（例如三 seed 全 0 分）——设计文档规定记「不适用」，不算出一个巨大比值当结论
    if mn <= 0:
        return {
            "ok": None,
            "n": len(finals),
            "mean": mn,
            "sd": s,
            "sd_over_mean": None,
            "limit": SD_RATIO_LIMIT,
            "finals": {m["seed"]: m["final"] for m in members},
            "verdict": "不适用（曲线本就没学：mean ≤ 0）",
        }
    ratio = abs(s / mn)
    ok = bool(ratio < SD_RATIO_LIMIT and mn > 0)
    return {
        "ok": ok,
        "n": len(finals),
        "mean": mn,
        "sd": s,
        "sd_over_mean": ratio,
        "limit": SD_RATIO_LIMIT,
        "mean_positive": True,
        "finals": {m["seed"]: m["final"] for m in members},
        "verdict": "跨种子稳定" if ok else "种子方差过大（先查 env 种子与 env_num 设置）",
    }


def criterion_3(group_a: dict, group_b: dict) -> dict:
    """两档可分：Welch |Δ| > 2·SE。"""
    a = [m["final"] for m in group_a["members"]]
    b = [m["final"] for m in group_b["members"]]
    if len(a) < 2 or len(b) < 2:
        return {"ok": None, "reason": f"{group_a['algo']} n={len(a)}, {group_b['algo']} n={len(b)}"}
    ma, mb = mean(a), mean(b)
    sa, sb = sd(a), sd(b)
    se = math.sqrt(sa**2 / len(a) + sb**2 / len(b))
    delta = ma - mb
    ratio = (abs(delta) / se) if se > 0 else math.inf
    ok = bool(se > 0 and abs(delta) > SE_MULTIPLIER * se)
    return {
        "ok": ok,
        "arm_a": group_a["algo"], "arm_b": group_b["algo"],
        "n_a": len(a), "n_b": len(b),
        "mean_a": ma, "mean_b": mb, "sd_a": sa, "sd_b": sb,
        "delta": delta,
        "se": se,
        "two_se": SE_MULTIPLIER * se,
        "ratio_abs_delta_over_se": ratio,
        "verdict": f"可分（{group_a['algo']} {'高' if delta > 0 else '低'}）" if ok else "未区分",
    }


# --------------------------------------------------------------------------- #
# 出图
# --------------------------------------------------------------------------- #
def setup_cjk_font():
    import matplotlib

    try:
        from matplotlib import font_manager

        available = {f.name for f in font_manager.fontManager.ttflist}
        for name in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Source Han Sans SC"):
            if name in available:
                matplotlib.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
                matplotlib.rcParams["axes.unicode_minus"] = False
                return name
    except Exception:  # noqa: BLE001
        pass
    return None


def plot(groups: list[dict], out_png: Path, window: float,
         task_name: str = "walker-walk") -> str | None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    font = setup_cjk_font()
    zh = font is not None
    fig, ax = plt.subplots(figsize=(9, 5.2), dpi=150)
    colors = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd"]

    for idx, group in enumerate(groups):
        members = group["members"]
        if not members:
            continue
        # 只在所有 seed 都有的 step 上做 mean±sd 波段
        step_sets = [dict(m["curves"]) for m in members]
        common = sorted(set.intersection(*[set(d) for d in step_sets]))
        if not common:
            continue
        means = [mean([d[s] for d in step_sets]) for s in common]
        sds = [sd([d[s] for d in step_sets]) for s in common]
        color = colors[idx % len(colors)]
        label = f"{group['algo']} (n={len(members)})"
        ax.plot(common, means, "-o", ms=3, lw=1.6, color=color, label=label)
        ax.fill_between(common, [m - s for m, s in zip(means, sds)],
                        [m + s for m, s in zip(means, sds)], color=color, alpha=0.18, lw=0)
        for m in members:  # 原始点：提示 eval 是离散的
            xs, ys = zip(*m["curves"])
            ax.plot(xs, ys, ".", ms=2.5, color=color, alpha=0.45)

    # 优先用 config 里配的 eval_every（1e4），没有才退到观测步长（会是 10032 这种 32 的倍数）
    strides = {
        int(m["eval_every_cfg"]) for g in groups for m in g["members"]
        if m.get("eval_every_cfg")
    } or {
        m["eval_stride"] for g in groups for m in g["members"] if m["eval_stride"]
    }
    stride_txt = "/".join(str(s) for s in sorted(strides)) if strides else "?"
    n_points = max((m["n_eval"] for g in groups for m in g["members"]), default=0)
    note = (
        f"eval 每 {stride_txt} 步一个点（每个 seed {n_points} 个点，离散标记为原始点）"
        if zh else
        f"eval every {stride_txt} steps ({n_points} points/seed; dots = raw points)"
    )
    ax.set_xlabel("env step" if not zh else "env 步数")
    ax.set_ylabel("episode/eval_score")
    title = f"DMC proprio {task_name} · eval_score（mean ± sd，跨 seed）" if zh else \
        f"DMC proprio {task_name} · eval_score (mean +/- sd across seeds)"
    ax.set_title(title)
    ax.grid(alpha=0.25)
    ax.legend(loc="lower right", fontsize=9)
    budget = max((m["final_step"] for g in groups for m in g["members"]), default=0)
    if budget > window > 0:
        for x in (window, budget - window):
            ax.axvline(x, ls=":", lw=1, color="gray")
    ax.annotate(note, xy=(0.01, 0.02), xycoords="axes fraction", fontsize=8, color="#444")
    fig.tight_layout()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png)
    plt.close(fig)
    return font


# --------------------------------------------------------------------------- #
def render_markdown(groups, crits, window, font, windowed, task_name: str = "walker-walk") -> str:
    lines = []
    lines.append(f"# 结果表（脚本生成，勿手改）\n")
    lines.append(f"- 生成时间：{now_iso()}")
    lines.append(f"- 生成脚本：`table_r2d.py`")
    lines.append(f"- 任务：DMC proprio `{task_name}`")
    lines.append(f"- 主指标：`episode/eval_score`（独立 eval envs，**不用** `episode/score` 训练回报）")
    lines.append(f"- 判据 1 窗口：最后/最前 {window:,.0f} 步（跨 seed 的 sd/√n 作 SE）\n")

    commits = {m["git_commit"] for g in groups for m in g["members"] if m["git_commit"]}
    torchs = {m["torch_version"] for g in groups for m in g["members"] if m["torch_version"]}
    if commits or torchs:
        lines.append(f"- 代码版本：git `{', '.join(sorted(c[:12] for c in commits)) or '?'}`"
                     f" / torch `{', '.join(sorted(torchs)) or '?'}`\n")

    lines.append("## 1 每组汇总\n")
    lines.append("| arm | n | eval 点/seed | 最终 eval_score mean±sd | 最好 eval_score mean±sd | 最后 eval step |")
    lines.append("|---|---|---|---|---|---|")
    for group in groups:
        members = group["members"]
        if not members:
            lines.append(f"| {group['algo']} | 0 | — | — | — | — |")
            continue
        finals = [m["final"] for m in members]
        bests = [m["best"] for m in members]
        n_eval = "/".join(str(m["n_eval"]) for m in members)
        last_step = "/".join(f"{m['final_step']:,}" for m in members)
        lines.append(
            f"| {group['algo']} | {len(members)} | {n_eval} | "
            f"{fmt(mean(finals))} ± {fmt(sd(finals))} | "
            f"{fmt(mean(bests))} ± {fmt(sd(bests))} | {last_step} |"
        )
    lines.append("")

    lines.append("## 2 判据 1 · 曲线真的在学\n")
    lines.append(f"通过条件：后 {window:,.0f} 步均值 − 前 {window:,.0f} 步均值 **> 2·SE**（SE 由跨 seed 的 Δ 求 sd/√n）\n")
    lines.append("| arm | n | 前窗口均值 | 后窗口均值 | Δ | SE | 2·SE | Δ/SE | 判定 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for group in groups:
        c = crits[group["algo"]]["c1"]
        if not c.get("ok") and c.get("delta_mean") is None:
            lines.append(f"| {group['algo']} | {c.get('n_seeds', 0)} | — | — | — | — | — | — | {c.get('reason', '—')} |")
            continue
        first_mean = mean([r["mean_first"] for r in c["per_seed"] if r.get("mean_first") is not None]) \
            if all(r.get("mean_first") is not None for r in c["per_seed"]) else None
        last_mean = mean([r["mean_last"] for r in c["per_seed"] if r.get("mean_last") is not None]) \
            if all(r.get("mean_last") is not None for r in c["per_seed"]) else None
        lines.append(
            f"| {group['algo']} | {c['n_seeds']} | {fmt(first_mean)} | {fmt(last_mean)} | "
            f"{fmt(c['delta_mean'])} | {fmt(c['se'])} | {fmt(c['two_se'])} | "
            f"{fmt(c['ratio_delta_over_se'])} | {'**通过**' if c['ok'] else '不通过：' + c['verdict']} |"
        )
    lines.append("")
    lines.append("逐 seed 的 Δ：")
    for group in groups:
        c = crits[group["algo"]]["c1"]
        detail = ", ".join(
            f"s{r['seed']}={fmt(r['delta'])}（{r.get('n_first', '?')}+{r.get('n_last', '?')} 点）"
            for r in c.get("per_seed", [])
        )
        lines.append(f"- `{group['algo']}`：{detail}")
    lines.append("")

    lines.append("## 3 判据 2 · 跨种子稳定\n")
    lines.append(f"通过条件：三 seed 最终 eval_score 的 **sd/mean < {SD_RATIO_LIMIT}**\n")
    lines.append("| arm | n | 最终分 mean | sd | sd/mean | 各 seed 最终分 | 判定 |")
    lines.append("|---|---|---|---|---|---|---|")
    for group in groups:
        c = crits[group["algo"]]["c2"]
        if c.get("mean") is None:
            lines.append(f"| {group['algo']} | {c.get('n', 0)} | — | — | — | — | {c.get('reason', '—')} |")
            continue
        finals = ", ".join(f"s{k}={fmt(v)}" for k, v in sorted(c["finals"].items()))
        lines.append(
            f"| {group['algo']} | {c['n']} | {fmt(c['mean'])} | {fmt(c['sd'])} | "
            f"{fmt(c['sd_over_mean'], 3)} | {finals} | {'**通过**' if c['ok'] else '不通过：' + c['verdict']} |"
        )
    lines.append("")

    lines.append("## 4 判据 3 · 两档可分\n")
    c3 = crits.get("c3")
    if not c3 or c3.get("delta") is None:
        lines.append(f"未做 E2 或数据不全：{c3.get('reason') if c3 else '只有一个 arm'}\n")
    else:
        lines.append(f"通过条件：Welch **|Δ| > 2·SE**，SE = √(sd₁²/n₁ + sd₂²/n₂)\n")
        lines.append(
            f"- {c3['arm_a']}：{fmt(c3['mean_a'])} ± {fmt(c3['sd_a'])}（n={c3['n_a']}）\n"
            f"- {c3['arm_b']}：{fmt(c3['mean_b'])} ± {fmt(c3['sd_b'])}（n={c3['n_b']}）\n"
            f"- Δ = {fmt(c3['delta'])}，SE = {fmt(c3['se'])}，2·SE = {fmt(c3['two_se'])}，"
            f"|Δ|/SE = {fmt(c3['ratio_abs_delta_over_se'])}\n"
            f"- **判定：{c3['verdict']}**\n"
        )

    lines.append("## 5 图\n")
    lines.append(f"- `figs/curves.png`（中文字体：{font or '未找到，图上用英文'}）")
    if windowed:
        lines.append(f"- 判据 1 的两个窗口边界在图上用灰色虚线标出（{window:,.0f} 步 / 末尾 {window:,.0f} 步）")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="JSONL → 结果表 + 三判据 + 曲线图")
    ap.add_argument("--algos", nargs="+", default=["dreamer"], help="要比较的 arm，顺序即表的顺序")
    ap.add_argument("--task-tag", default="dmcww")
    ap.add_argument("--task-name", default="walker-walk",
                    help="图上标题与表头写的环境名（默认 walker-walk，cheetah 用 cheetah-run）")
    ap.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2])
    ap.add_argument("--window", type=float, default=None,
                    help="判据 1 的窗口步数；缺省按 W = max(2000, 预算//5) 现算（A2 口径）")
    ap.add_argument("--work", type=Path, default=WORK_DIR)
    args = ap.parse_args(argv)

    args.work = args.work.resolve()
    groups = [load_group(args.work, algo, args.task_tag, args.seeds) for algo in args.algos]
    for group in groups:
        if group["missing"]:
            log(f"{group['algo']} 缺文件：{group['missing']}")
        log(f"{group['algo']}：找到 {len(group['members'])} 个 seed 的 JSONL")

    # 判据 1 的窗口：缺省按**声明预算**现算（A2 2026-11-02 口径），不要再用固定 1e5
    declared = [m.get("budget_declared") for g in groups for m in g["members"]]
    declared = [int(x) for x in declared if x]
    observed = [m["final_step"] for g in groups for m in g["members"]]
    budget = float(max(declared)) if declared else float(max(observed) if observed else 0)
    window = float(args.window) if args.window is not None else float(criterion1_window(budget))
    if budget and not window_applicable(budget, window):
        raise SystemExit(
            f"窗口不适用：预算 {budget:.0f} < {WINDOW_PRECONDITION}×窗口 {window:.0f} "
            f"⇒ 判据 1 的前后两个窗口会重叠到失去意义（这正是 1e5 降级档的坑）。"
            f"请把 --window 调到 ≤ 预算/{WINDOW_PRECONDITION:.1f}。"
        )
    log(f"判据 1 窗口 W = {window:,.0f}（预算 {budget:,.0f}，规则 W = max({WINDOW_FLOOR}, 预算//{WINDOW_FRACTION})）")

    crits = {}
    for group in groups:
        crits[group["algo"]] = {
            "c1": criterion_1(group, window),
            "c2": criterion_2(group),
        }
    if len(groups) >= 2:
        crits["c3"] = criterion_3(groups[0], groups[1])

    font = plot(groups, args.work / "figs" / "curves.png", window, args.task_name)
    md = render_markdown(groups, crits, window, font, windowed=True, task_name=args.task_name)

    results_dir = args.work / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "table.md").write_text(md, encoding="utf-8")
    (results_dir / "criteria.json").write_text(
        json.dumps(
            {
                "generated_at": now_iso(),
                "generated_by": "table_r2d.py",
                "task_tag": args.task_tag,
                "seeds": args.seeds,
                "window": window,
                "window_rule": f"W = max({WINDOW_FLOOR}, 预算//{WINDOW_FRACTION})；前置条件 预算 ≥ {WINDOW_PRECONDITION}·W",
                "window_explicit": args.window is not None,
                "budget_observed": budget,
                "sd_ratio_limit": SD_RATIO_LIMIT,
                "se_multiplier": SE_MULTIPLIER,
                "groups": [
                    {
                        "algo": g["algo"],
                        "members": [
                            {k: m[k] for k in ("seed", "file", "final_step", "final", "best", "n_eval",
                                               "eval_stride", "git_commit", "torch_version")}
                            for m in g["members"]
                        ],
                    }
                    for g in groups
                ],
                "criteria": crits,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print()
    print(md)
    print(f"[table] 表 → {results_dir / 'table.md'}")
    print(f"[table] 判据 → {results_dir / 'criteria.json'}")
    print(f"[table] 图 → {args.work / 'figs' / 'curves.png'}")

    # 有 arm 一个 seed 都没读到 ⇒ 非零退出，别让空表被当成结果
    if any(not g["members"] for g in groups):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
