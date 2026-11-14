r"""加 seed 之后判据 3 会怎么变 —— 用实测数据 + 情景推演，不手打。
读 results\*.jsonl 的 summary 行（kind=summary 的末行）。
"""
import json
import math
import statistics as st
from pathlib import Path

ROOT = Path(r"C:\Users\10348\Desktop\工作\DreamerV3复现\results")
TAG = "dmcww-e510000"


def load_finals(algo):
    out = {}
    for p in sorted(ROOT.glob(f"{algo}-{TAG}-s*.jsonl")):
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        o = json.loads(lines[-1])
        seed = int(p.stem.split("-s")[-1])
        out[seed] = float(o["final_eval_score"])
    return dict(sorted(out.items()))


def crit(d, r):
    """d/r: list of final scores。返回 mean/sd/n 与判据3 的 Δ、SE、比值。"""
    md, mr = st.fmean(d), st.fmean(r)
    sd, sr = st.stdev(d), st.stdev(r)
    delta = mr - md
    se = math.sqrt(sd ** 2 / len(d) + sr ** 2 / len(r))
    return dict(md=md, mr=mr, sd=sd, sr=sr, delta=delta, se=se, ratio=abs(delta) / se,
                c2_d=sd / md, c2_r=sr / mr)


dreamer = load_finals("dreamer")
r2 = load_finals("r2dreamer")

print("=" * 84)
print("一、实际成绩（results 里现有全部 seed）")
print("=" * 84)
print(f"dreamer   n={len(dreamer)}  {dreamer}")
print(f"r2dreamer n={len(r2)}  {r2}")

cur = crit(list(dreamer.values()), list(r2.values()))
print()
print(f"dreamer   mean={cur['md']:.2f}  sd={cur['sd']:.2f}  sd/mean={cur['c2_d']:.3f}")
print(f"r2dreamer mean={cur['mr']:.2f}  sd={cur['sr']:.2f}  sd/mean={cur['c2_r']:.3f}")
print(f"判据3：Δ={cur['delta']:.2f}  SE={cur['se']:.2f}  2SE={2*cur['se']:.2f}  |Δ|/SE={cur['ratio']:.2f}"
      f"  ⇒ {'可分' if cur['ratio'] > 2 else '未区分'}")

print()
print("=" * 84)
print("二、历史对照：n=3 时是什么样")
print("=" * 84)
d3 = {k: v for k, v in dreamer.items() if k <= 2}
c3 = crit(list(d3.values()), list(r2.values()))
print(f"n=3+3：dreamer {c3['md']:.2f}±{c3['sd']:.2f}  r2dreamer {c3['mr']:.2f}±{c3['sr']:.2f}")
print(f"        Δ={c3['delta']:.2f}  SE={c3['se']:.2f}  |Δ|/SE={c3['ratio']:.2f}")
print(f"n=4+3：Δ={cur['delta']:.2f}  SE={cur['se']:.2f}  |Δ|/SE={cur['ratio']:.2f}")
print(f"⇒ Δ 从 {c3['delta']:.2f} 缩到 {cur['delta']:.2f}（少了 {c3['delta']-cur['delta']:.2f}），"
      f"SE 从 {c3['se']:.2f} 降到 {cur['se']:.2f}（少 {c3['se']-cur['se']:.2f}）")

print()
print("=" * 84)
print("三、后面两段（seed4/seed5）会往哪走 —— 情景推演")
print("=" * 84)
print("做法：把「还没跑的 dreamer seed」假设成某个常数 x，重算均值/方差/判据3。")
print("注意 x 不是预测值，是「如果一个新 seed 拿这个分」的敏感性分析。")
print()
hdr = f"{'假设新 seed 的值 x':>18}{'dreamer mean':>14}{'dreamer sd':>12}{'Δ':>9}{'SE':>8}{'|Δ|/SE':>9}{'判据2':>8}"
print(hdr)
print("-" * 84)
for x in (500, 600, 683.27, 729.35, 800, 867.59, 950):
    for k in (1, 2):
        vals = list(dreamer.values()) + [x] * k
        c = crit(vals, list(r2.values()))
        ok2 = "通过" if c["c2_d"] < 0.25 else "不通过"
        if k == 1:
            print(f"{x:>18.2f}{c['md']:>14.2f}{c['sd']:>12.2f}{c['delta']:>9.2f}{c['se']:>8.2f}{c['ratio']:>9.2f}{ok2:>8}  ← 只补 1 个（n=5）")
        else:
            print(f"{'':>18}{c['md']:>14.2f}{c['sd']:>12.2f}{c['delta']:>9.2f}{c['se']:>8.2f}{c['ratio']:>9.2f}{ok2:>8}  ← 两个都是 x（n=6）")
print()

print("=" * 84)
print("四、如果 Δ 和 sd 都保持不变（只是 n 变大），判据3 的上限在哪")
print("=" * 84)
print("这是最理想情况：新 seed 不改变 Δ 也不改变离散度，纯靠 n 把 SE 压下去。")
print(f"{'dreamer n':>10}{'SE':>9}{'|Δ|/SE':>9}{'判据2 余量':>14}")
print("-" * 84)
for n_d in range(4, 13):
    se = math.sqrt(cur["sd"] ** 2 / n_d + cur["sr"] ** 2 / len(r2))
    ratio = abs(cur["delta"]) / se
    margin = (0.25 * cur["md"] - cur["sd"]) / (0.25 * cur["md"]) * 100 if cur["md"] > 0 else 0
    print(f"{n_d:>10}{se:>9.2f}{ratio:>9.2f}{margin:>13.1f}%")
print()
print("判据2 的余量算法：sd 需要 < 0.25×mean 才通过；上表用的是当前 sd 与当前 mean。")
print("n 变大本身不改 sd 与 mean，所以“纯 n 效应”下判据2 只会因为 mean 变化而变动。")
