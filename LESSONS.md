# Lessons from a DreamerV3 / R2-Dreamer reproduction

Everything below is something this project got wrong, measured, or had to fix between 2026-11-02 and
2026-11-14, on one RTX 4090 Laptop (16 GB) under WSL2. Each item is written as **claim → evidence from
this project → the practice I now follow**. Numbers are from the run logs in `results/`; nothing here is
an estimate unless it says so.

The headline numbers the items refer to:

| arm | walker-walk (n) | cheetah-run (n) |
|---|---|---|
| A · reconstruction (`model.rep_loss=dreamer`) | 684.50 ± 139.38 (6) | 665.27 ± 64.45 (3) |
| B · redundancy reduction (`model.rep_loss=r2dreamer`) | 903.43 ± 19.51 (3) | 663.50 ± 15.56 (3) |
| Δ (B − A) | −218.93 | +1.77 |
| SE / \|Δ\|/SE | 58.01 / **3.77** | 38.28 / **0.05** |
| verdict at threshold 2·SE | separable | indistinguishable |

---

## 1. Decide the statistical rule before you look at the curves

**L1. Write the criteria down, with the constants, before the first run finishes.**
Evidence: the three criteria used here were fixed on 2026-11-02 — criterion 1 window `W = max(2000, budget // 5)`,
sample standard deviation with `ddof = 1`, separability as `|Δ| > 2·SE` with Welch `SE = √(sd₁²/n₁ + sd₂²/n₂)`.
Because they were fixed in advance, the cheetah null (+1.77) could not be argued away by picking a friendlier window.
Practice: the rule lives in the table-generating script, not in prose, and the generated table prints the rule it used.

**L2. The window has to be much smaller than the budget, or criterion 1 proves itself.**
Evidence: at budget 510,000 the window is 102,000 steps, so the first window is steps 0–102k and the last is
408k–510k; they cannot overlap. At a 20,000-step budget the same rule gives 4,000, and a fixed 100,000-step
window would have made the two windows overlap into meaninglessness — which is exactly the trap the 2e4 batch hit
before the rule was changed.
Practice: the script refuses to run when `budget < 2.5 × window`.

**L3. "Final score" is one noisy sample. Report what you mean.**
Evidence: walker arm A, seed 0: final evaluation 653.63 but the mean of its last ten evaluations is 534.52 —
a 119-point gap between two defensible summaries of the same run.
Practice: state which summary the table uses (here: the final evaluation point, plus the per-seed values so anyone
can recompute), and never mix summaries between arms.

**L4. Keep aborted runs, do not overwrite them.**
Evidence: three interrupted cheetah runs were archived as `s0_aborted_20261111_1205`, `…_20261112_0300`,
`…_20261112_1205` (67,136 / 232,128 / 2,112 steps) instead of being deleted or resumed in place.
Practice: an interrupted run is evidence about the pipeline, not garbage; move it aside with a timestamp.

---

## 2. Compute power before you buy more seeds

**L5. Separability is a function of Δ/SE, not of how convincing the curves look.**
Evidence: walker Δ = −218.93 with SE 58.01 → 3.77, separable. cheetah Δ = +1.77 with SE 38.28 → 0.05, nothing.
Both sets of curves look like two lines climbing together; only the effect size separates them.

**L6. Adding seeds only shrinks SE by √n — check that √n is enough before spending the GPU hours.**
Evidence: the cheetah null could not be rescued by going to 6 seeds per arm. The threshold `2·SE` at n = 3 per arm
is 76.55; at n = 6 per arm it is about 54; the observed difference is 1.77. Six more runs (~25 GPU-hours) would
have bought a slightly tighter statement of the same null. The decision was made by arithmetic, in advance, and the
runs were not bought.

**L7. A single high-variance arm dominates the SE, so do not split seeds evenly.**
Evidence: arm A's seed-to-seed sd is 139.38 on walker and 64.45 on cheetah; arm B's is 19.51 and 15.56 — a factor of
4 to 7. Extra seeds on arm B would barely move the threshold; extra seeds on arm A are the only ones that would.
Practice: allocate seeds to the arm with the larger variance.

**L8. The estimate can move while seeds are being added, and the movement is not the effect changing.**
Evidence: on walker, `|Δ|/SE` went 2.57 (n=3) → 2.27 (n=4) → 3.01 (n=5) → 3.77 (n=6) while Δ itself stayed between
174 and 220. One late seed scoring 867.59 pulled arm A's mean up and the ratio down; the next seeds pulled it back.
Practice: track Δ and SE separately, and report the sequence — a ratio that dips before it rises is normal.

---

## 3. "Not separable" is a result, not a failure

**L9. Say what was not shown, and at what precision.**
Evidence: the cheetah conclusion is written as "at n = 3 per arm, in this environment, the two objectives are
statistically indistinguishable", not as "the two objectives are equivalent". The distinction is the whole point:
the same pair of objectives differs by 219 points on walker.

**L10. Distinguish "the effect is absent" from "the instrument is too coarse".**
Evidence: the usual explanation for a null is noise, and here it is falsifiable. Arm A's spread on cheetah (64.45)
is *less than half* its spread on walker (139.38) — the measurement got tighter, and the effect still went to zero.
That is why the null is reported as a property of the environment, not of the measurement.
Practice: whenever a null appears, check whether the variance shrank too. If it did, the null is real.

**L11. A cheap screen can flip sign, so a cheap screen is not a small version of the experiment.**
Evidence: the same comparison at 20,000 steps returned **+4.29** (`|Δ|/SE` 0.16, B ahead) on Windows eager mode and
**−24.88** (`|Δ|/SE` 0.82, A ahead) under WSL with `model.compile=True`. Both are "not separable" by the same
criterion and they disagree about direction. Worse, the screening batch fails to establish that an arm is even stable:
on the WSL run arm A fails criterion 2 outright (`sd / mean = 0.475` against a 0.25 threshold) while criterion 1
passes comfortably (ratio 4.71). A cheap run gives you a learning curve; it does not give you a ranking.

**L12. Only compare arms inside one configuration; change one thing at a time.**
Evidence: the two 2e4 batches differ in operating system *and* `torch.compile`, so the sign flip above cannot be
attributed to the budget alone. It is evidence that the ranking is unstable at that scale, not evidence about why.
Practice: within a batch, arms differ only by the arm variable; across batches, say what else changed.

**L13. Both arms can be learning well and still be indistinguishable — check that separately.**
Evidence: cheetah criterion 1 (early-to-late improvement over the first/last 102,000 steps) passes for both arms,
413.85 with SE 39.71 (ratio 10.42) and 421.11 with SE 63.19 (ratio 6.66); criterion 2 (sd/mean) passes at 0.097
and 0.023. So the null is about the comparison, not about either arm working.

---

## 4. Reproducibility: one variable, pinned code, config as evidence

**L14. Reproduce with upstream code unmodified.**
Evidence: the training code is `NM512/r2dreamer` at commit `becf338`, working tree clean, no local patches.
The two arms differ only by the string `model.rep_loss ∈ {dreamer, r2dreamer}`.
Practice: if a patch is ever needed, it goes in the repo and in the README, and the commit hash changes with it.

**L15. `.hydra/config.yaml` is the experiment record; keep it with the run.**
Evidence: the only difference between `dreamer/s1` and `dreamer/s2` in their saved configs is `logdir` and `seed`.
That is what makes "the two runs are comparable" a checked statement rather than a claim.

**L16. Generate every number in the paper from a script, from the raw logs.**
Evidence: the pipeline is `logdir/metrics.jsonl → export_r2d.py → results/*.jsonl (with a config header line) →
table_r2d.py → table.md + criteria.json`. The tables in this repo were produced by those scripts; none were typed.
Practice: no hand-typed numbers anywhere, including in slides.

**L17. Record the environment with the result, not in someone's memory.**
Evidence: torch `2.6.0+cu124`, triton 3.2.0, WSL2 Ubuntu, `model.compile=True`, `env_num=16`, `action_repeat=2`,
`batch_size=16`, `batch_length=64`, `eval_episode_num=10`, `steps=510000`, `eval_every=10000`,
`update_log_every=5000`.

---

## 5. Measurement hygiene: know what the logs actually do

**L18. Know the logging interval before you use the log to decide whether the run is alive.**
Evidence: `metrics.jsonl` is only flushed every `update_log_every = 5000` steps, which is about two minutes at
34 steps/s. Sampling a 90-second window can show "no progress" on a perfectly healthy run.
Practice: judge liveness by cumulative CPU time (`ps -o etime=,time=`) plus the latest `step` in the log, not by a
short snapshot of the file.

**L19. Do not extrapolate a schedule from one slow minute.**
Evidence: throughput on 2026-11-14 dropped to 24.5 steps/s for roughly the first hour of a run and I re-planned the
night around it; the run then finished at 34.3 steps/s over 248 minutes, and the next one at 34.8 steps/s over
242 minutes — the same as every earlier run. The revised schedule was wrong by two hours in the safe direction.
Practice: quote throughput as a whole-run average, and treat a one-hour dip as a dip until it persists.

**L20. Measure the cost of a run, then multiply — do not guess.**
Evidence: 510,000 steps costs 4.0–4.3 hours here; 100,000 steps costs about 49 minutes. Both figures came from
`ps` timings on finished runs.

**L21. No checkpoints means a killed run is unrecoverable — so acquire the resource before starting, not after.**
Evidence: several early runs were lost to concurrent GPU use and to the WSL VM being shut down mid-run
(one died at 2,112 steps).

---

## 6. Tooling traps that cost hours (Windows + WSL + PowerShell 5.1)

**L22. Never pass a Chinese path inside `wsl.exe … bash -c "…"`; write a `.sh` file and run it.**
Evidence: `wsl.exe -d Ubuntu -- bash -c "cd '…/工作/…' && …"` fails silently — PowerShell eats the quotes and the
path arrives mangled. Every reliable script in `scripts/` is a file invoked as `bash /path/script.sh`.

**L23. PowerShell here-strings write CRLF into files consumed by bash.**
Evidence: a generated script reached bash with `do\r`, and bash reported `unexpected end of file from 'for' command`.
Practice: pipe the body through `tr -d '\r'` before running it, or write the file with explicit LF.

**L24. `nohup … &` does not survive the `wsl.exe` session; `exec` in the foreground does.**
Evidence: a queued runner started under `nohup` died with its parent and the queue looked "started but instantly killed".

**L25. Killing a parent shell with SIGTERM does not kill its foreground child.**
Evidence: verified directly here. Stop scripts must target `train.py` explicitly.

**L26. A background watchdog that watches for a log line that is never written is not a watchdog.**
Evidence: two watchers ran all night looking for `CHEETAH_DONE`, a marker no script ever emitted; they reported
nothing and both exited on their own time limits. The real completion signal was `统排执行器完成` in a different log.
Practice: when you add a watcher, verify its trigger fires at least once on a known-good run.

**L27. An unattended scheduler needs an attempt cap, and its logs must be bounded.**
Evidence: the first scheduler version used `local` at the top level of a bash script (only legal inside functions),
so its time variables were empty, the inner wait was skipped, and the outer loop spun. It appended about 83,000
junk lines (~6.9 MB) to the progress log before it was caught. The rewrite logs three lines at startup and each
waiting loop has an attempt counter.
Also in PowerShell 5.1: `$open` and `$Open` are the same variable (case-insensitive); `(if …)` expressions are
PowerShell 7 only; `[byte[]]` returned from a function unrolls, so `return ,$bytes` is required; `.ps1` files
containing CJK must be saved as UTF-8 **with BOM** or 5.1 reads them as GBK and fails.

**L28. Verify rendered documents by page count, not by eye.**
Evidence: a "one page" A4 summary was rendered three times before it actually was one page; the versions that
overflowed carried only a few hundred characters onto the second page, so nothing *looked* wrong. A browser
screenshot taken at a viewport wider than the printable area also makes a document look far shorter than it prints.
`len(PdfReader(path).pages)` is the check; a tiny amount of overflow is easy to miss and easy to ship.

---

## 7. Shared-GPU discipline

**L29. A resource lock must be acquired before the work starts, and it must not be blocked by your own work.**
Evidence: the arbiter's VRAM gate refuses a token when 12 GB is already used — including by the very run that is
asking. Acquiring after launch never succeeds. Acquire first, launch second, release on exit.

**L30. Mutex timeouts must be loud.**
Evidence: the arbiter's lock timeout was 30 s and returned an empty result silently; it is now 120 s with an
explicit `WARN: mutex timeout`.

**L31. Put the forbidden hours in the scheduler, not in your head.**
Evidence: a nightly 22:00–24:00 window where the machine must not be used is now a constant in the scheduler,
which defers a run rather than starting one that would cross into it.

---

## 8. Reporting

**L32. A number in a document is a claim that has to be re-checked when the data changes.**
Evidence: a figure computed at n = 6 / n = 3 for one arm pair (683.27, ratio 2.57) was superseded by the full-seed
values (684.50, ratio 3.77) within days. Any document quoting the old numbers is now wrong.
Practice: quote the script-generated table by its generation timestamp.

**L33. Report the cost next to the result.**
Evidence: 15 full-budget runs (9 walker + 6 cheetah) at 4.0–4.3 h each is about 62 GPU-hours on one laptop GPU.
That is the number that decides whether a follow-up is worth doing.

---

## What I would do differently

1. Fix the logging interval *before* the first long run, not after being confused by a quiet log.
2. Run the small-budget screening twice from the start — one run of a screening batch cannot tell you that screening
   is unreliable, and the second run cost 6 hours versus the 25 hours a wrong decision would have cost.
3. Write the lessons document alongside the experiment, not two weeks later, because the failure modes are the part
   that does not survive.
