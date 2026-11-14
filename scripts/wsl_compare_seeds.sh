#!/bin/bash
# 对比已完成 3 个 dreamer seed 与新跑的 s3 的 eval 曲线（只看，不改）
for s in 0 1 2 3; do
  f=/root/r2d/logdir_live/dreamer/s$s/metrics.jsonl
  [ -f "$f" ] || { echo "s$s: 无 metrics.jsonl"; continue; }
  echo "--- dreamer s$s ---"
  grep -o '"step": [0-9]*, .*"episode/eval_score": [0-9.]*' "$f" 2>/dev/null | head -0
  # 直接逐行抽 step 与 eval_score（同一行里两个键都存在才算一个 eval 点）
  python3 - "$f" <<'PY'
import json, sys
p = sys.argv[1]
pts = []
with open(p, encoding="utf-8", errors="replace") as fh:
    for line in fh:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if "episode/eval_score" in o and "step" in o:
            pts.append((int(o["step"]), float(o["episode/eval_score"])))
if not pts:
    print("  (还没有 eval 点)")
else:
    key = {}
    for st, sc in pts:
        key[st // 100000] = (st, sc)
    print("  末个 eval 点: step=%d score=%.2f   （共 %d 个 eval 点）" % (pts[-1][0], pts[-1][1], len(pts)))
    row = "  ".join("k%d:%s" % (k, ("%.1f" % v[1]) if k in key else "-") for k, v in sorted(key.items()) for _ in [0])
    for k in sorted(key):
        print("    step %7d  score %8.2f" % key[k])
PY
done
