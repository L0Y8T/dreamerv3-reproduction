# DreamerV3 / R2-Dreamer reproduction: does the representation objective matter?

A full-budget, two-task, two-arm reproduction on one machine. The code is **not** modified — the two arms differ by
one configuration string. The result is that the comparison is separable on one environment and completely absent on
another.

- Code: [`NM512/r2dreamer`](https://github.com/NM512/r2dreamer) at commit `becf338`, working tree clean, no patches.
- Hardware: one RTX 4090 Laptop (16 GB), WSL2 Ubuntu, torch `2.6.0+cu124`, triton 3.2.0, `model.compile=True`.
- Budget: 510,000 environment steps per run, evaluation every 10,000 steps × 10 episodes.

## Headline result

| arm | walker-walk (n) | cheetah-run (n) |
|---|---|---|
| A · reconstruction · `model.rep_loss=dreamer` | 684.50 ± 139.38 (6) | 665.27 ± 64.45 (3) |
| B · redundancy reduction · `model.rep_loss=r2dreamer` | 903.43 ± 19.51 (3) | 663.50 ± 15.56 (3) |
| Δ (B − A) | −218.93 | +1.77 |
| SE (Welch) | 58.01 | 38.28 |
| \|Δ\| / SE (threshold 2) | **3.77 → separable** | **0.05 → indistinguishable** |

Main metric is `episode/eval_score` (evaluation environments, not the training return).

Two things follow, and only these two:

1. On `walker-walk`, redundancy reduction is ahead by about 219 points at 3.8 standard errors.
2. On `cheetah-run`, the two cannot be told apart at this sample size — and the null is not a noise problem, because
   arm A's seed-to-seed spread is *smaller* on cheetah (64.45) than on walker (139.38).

The ranking is a property of the environment. One environment is not enough to rank two representation objectives.

## The three criteria

| criterion | rule | walker-walk | cheetah-run |
|---|---|---|---|
| 1 · the curve is learning | last `W` − first `W` > 2·SE, `W = max(2000, budget // 5)` | A 7.79 / B 16.01 → pass | A 10.42 / B 6.66 → pass |
| 2 · stable across seeds | `sd / mean < 0.25` of final scores | A 0.204 / B 0.022 → pass | A 0.097 / B 0.023 → pass |
| 3 · arms separable | `|Δ| > 2·SE`, Welch | 3.77 → **separable** | 0.05 → **indistinguishable** |

Sample standard deviation uses `ddof = 1` throughout. At budget 510,000 the criterion-1 window is 102,000 steps, so
the first and last windows cannot overlap.

## A cheap screen is not a small version of this experiment

The same comparison at 20,000 steps (4% of budget) was run twice, on two different machine configurations:

| run | arm A | arm B | Δ (B − A) | \|Δ\|/SE | direction |
|---|---|---|---|---|---|
| Windows, eager | 102.60 ± 45.54 | 106.89 ± 8.09 | +4.29 | 0.16 | B ahead |
| WSL, compiled | 108.08 ± 51.29 | 83.20 ± 11.50 | −24.88 | 0.82 | A ahead |

Both are "not separable" by the same criterion, and they disagree about sign. The two runs differ only in operating
system and `torch.compile`, so this is evidence that the ranking is unstable at that scale — not evidence about why.

A screening batch cannot even establish that an arm is stable: on the WSL run, arm A fails criterion 2 outright
(`sd / mean = 0.475` against the 0.25 threshold) while criterion 1 passes comfortably. Cheap runs give you a learning
curve, not a ranking.

## Repository layout

```
results/walker/          walker-walk, 510k steps, 6 + 3 per-seed JSONL, table.md, criteria.json
results/cheetah/         cheetah-run,  510k steps, 3 + 3 per-seed JSONL, table.md, criteria.json
results/screening-2e4/   20,000-step screening batch: windows-eager/ and wsl-compiled/
figs/                    mean ± sd curves with the criterion-1 windows marked
scripts/                 export / table / gate / scheduling scripts used for the runs above
tools/                   GPU arbiter (token protocol) used to share the card with other work
docs/                    design notes, experiment records, run log (Chinese)
LESSONS.md               what went wrong and what to do instead — the useful part
```

Each `*.jsonl` starts with a `config` line (the full hydra config of that run), then one `eval` / `train` record per
logged step, then a `summary` line. Every number in the tables is generated from these files by `scripts/table_r2d.py`.

## Reproducing

```bash
git clone https://github.com/NM512/r2dreamer && cd r2dreamer
git checkout becf338

python train.py \
  env=dmc_proprio env.task=dmc_cheetah_run model=size12M \
  model.rep_loss=dreamer            `# or: r2dreamer` \
  seed=0 env.steps=510000 \
  trainer.eval_every=10000 trainer.update_log_every=5000 \
  model.compile=True \
  logdir=/path/to/logdir/cheetah/dreamer/s0
```

Then `python scripts/export_r2d.py --work <work> --arm dreamer --algo dreamer --task-tag dmccr --seeds 0 1 2`
and `python scripts/table_r2d.py --work <work> --algos dreamer r2dreamer --task-tag dmccr --task-name cheetah-run --seeds 0 1 2`.

Cost: about 4.0–4.3 hours per 510,000-step run on this GPU; 15 full-budget runs were used for the results above
(9 walker + 6 cheetah), about 62 GPU-hours.

## What this does not show

- It does not reproduce the published numbers (different hardware and batch configuration: 16 × 64 here).
- It says nothing about pixel inputs — everything here is proprioceptive state.
- It is not a claim that either objective is better in general.
- `n = 3` on cheetah is not enough to detect a difference of a few points; it *is* enough to bound the difference
  well below the 219 points seen on walker.

## Notes

- Upstream code is not mirrored in this repository. Only my scripts, logs-derived results, and notes are here.
- `docs/` is in Chinese (working notes); `README.md` and `LESSONS.md` are in English.
- The one-page summary handed out with the 2026-11 update is `docs/onepager_2026-11-14.pdf`.
