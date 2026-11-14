# 导出某一阶段的完整材料供复盘：每个国家的输入提示词和原始回复。
# 用法：python3 -I dump_phase.py <run_dir> <阶段，如 S1939M> [输出目录]
import csv
import sys
from pathlib import Path

run = Path(sys.argv[1])
phase = sys.argv[2]
out = Path(sys.argv[3]) if len(sys.argv) > 3 else run / "review" / phase
out.mkdir(parents=True, exist_ok=True)

rows = [r for r in csv.DictReader(open(run / "llm_responses.csv", encoding="utf-8")) if r["phase"] == phase]
if not rows:
    raise SystemExit(f"{phase} 没有任何记录")
for i, r in enumerate(rows):
    name = f"{i:02d}_{r['power']}_{r['response_type']}_{r['success']}"
    (out / f"{name}.prompt.txt").write_text(r["raw_input"], encoding="utf-8")
    (out / f"{name}.response.txt").write_text(r["raw_response"], encoding="utf-8")
print(f"{phase}：导出 {len(rows)} 条到 {out}")
