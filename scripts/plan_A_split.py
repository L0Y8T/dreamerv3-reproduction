# -*- coding: utf-8 -*-
r"""A 方案（dreamer 补 seed）分段收益测算。
所有数字从 results\*.jsonl 读，不手打。
"""
import json
import math
import os
import statistics as st
from pathlib import Path

ROOT = Path(r"C:\Users\10348\Desktop\工作\DreamerV3复现\results")
TAG = "dmcww-e510000"


def load(algo):
    vals = []
    for s in (0, 1, 2):
        p = ROOT / f"{algo}-{TAG}-s{s}.jsonl"
        with p.open(encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip()]
        vals.append(float(json.loads(lines[-1])["final_eval_score"]))
    return vals


def ci(d, r, n_d, sd_d_override=None):
    sd_d = sd_d_override if sd_d_override is not None else r["sd_d"]
    se = math.sqrt(sd_d ** 2 / n_d + r["sd_r"] ** 2 / r["n_r"])
    return se, r["delta"] / se


dreamer = load("dreamer")
r2 = load("r2dreamer")

base = {
    "n_r": len(r2),
    "sd_r": st.stdev(r2),
    "sd_d": st.stdev(dreamer),
}
base["delta"] = st.fmean(r2) - st.fmean(dreamer)

print("=" * 78)
print("A 方案的现状基准（从 6 份 JSONL 的 summary 行现读）")
print("=" * 78)
print(f"dreamer   n={len(dreamer)}  各seed={[round(v,2) for v in dreamer]}")
print(f"           mean={st.fmean(dreamer):.2f}  sd(ddof=1)={base['sd_d']:.2f}  sd/mean={base['sd_d']/st.fmean(dreamer):.3f}")
print(f"r2dreamer n={len(r2)}  各seed={[round(v,2) for v in r2]}")
print(f"           mean={st.fmean(r2):.2f}  sd(ddof=1)={base['sd_r']:.2f}  sd/mean={base['sd_r']/st.fmean(r2):.3f}")
print(f"Δ = {base['delta']:.2f}（r2dreamer − dreamer）")
print()
se0, r0 = ci(None, base, 3)
print(f"SE 里 dreamer 的贡献 = {base['sd_d']**2/3:.0f}，r2dreamer 的贡献 = {base['sd_r']**2/3:.1f}"
      f"（dreamer 占 {base['sd_d']**2/3/(base['sd_d']**2/3+base['sd_r']**2/3)*100:.1f}%）")

print()
print("=" * 78)
print("逐段收益：每加 1 个 dreamer seed（假设新 seed 的离散度与现有三个同量级）")
print("=" * 78)
print(f"{'段':<4}{'dreamer n':>10}{'SE':>9}{'|Δ|/SE':>9}{'比上一段':>10}{'是否过 2·SE':>12}{'累计机时':>10}")
print("-" * 78)
prev = r0
for k in range(0, 7):
    n_d = 3 + k
    se, ratio = ci(None, base, n_d)
    gain = ratio - prev
    hrs = k * 4.0
    mark = "通过" if ratio > 2 else "未通过"
    seg = "现状" if k == 0 else f"+seed{2+k}"
    print(f"{seg:<4}{n_d:>10}{se:>9.2f}{ratio:>9.2f}{('' if k==0 else f'{gain:+.2f}'):>10}{mark:>12}{hrs:>9.0f}h")
    prev = ratio

print()
print("=" * 78)
print("风险：判据 2（sd/mean < 0.25）在加 seed 后会怎样")
print("=" * 78)
print("判据 2 是绝对门槛，加 seed 不会让真实方差变小，只会让 sd 的估计变准。")
print("要让它失败，只需要 6 个 seed 的 sd 涨过 0.25×mean。")
print()
target_sd = 0.25 * st.fmean(dreamer)
print(f"若 mean 不变（{st.fmean(dreamer):.2f}），sd 必须 < {target_sd:.2f} 才通过；现在 sd = {base['sd_d']:.2f}")
print(f"余量只有 {(target_sd - base['sd_d'])/target_sd*100:.1f}%\n")

# 情景：补进来的 3 个 seed 分别取什么值时，6 seed 的 sd/mean 会越过 0.25
print("情景推演（n=6，前 3 个固定为实测值，后 3 个假设为同一常数 x）：")
print(f"{'新 seed 取值 x':>16}{'6 seed mean':>14}{'6 seed sd':>12}{'sd/mean':>10}{'判据2':>8}")
print("-" * 78)
for x in (500, 550, 600, 650, 683.27, 750, 800, 900, 1000):
    allv = dreamer + [x, x, x]
    m = st.fmean(allv)
    sd = st.stdev(allv)
    ok = "通过" if (sd / m < 0.25 and m > 0) else "不通过"
    print(f"{x:>16.2f}{m:>14.2f}{sd:>12.2f}{sd/m:>10.3f}{ok:>8}")

print()
print("=" * 78)
print("B 方案里「换任务」这一段的成本不确定性")
print("=" * 78)
print("A 的机时是可数的（每个 seed 实测 ~3.8-4.7 h）；B 的新任务段目前未知，原因：")
print("  1) venv 里没有 crafter / ale_py，要先装（pip 可装，但 ale_py 装完要验 import）")
print("  2) 新任务的 env_num / time_limit / steps 预算要重新定，不能照搬 walker-walk")
print("  3) 装完之后必须重测吞吐（E0 探针），才能估出这档要跑多久")
print("  ⇒ 这三步是 GPU 之外的活，可以和 A 的 12 h 并行做，但吞吐实测要等 GPU 空出来")
