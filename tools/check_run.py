# 检查一次对局跑到哪了、有没有出错。用法：python3 -I check_run.py <run_dir>
import csv
import json
import sys
from collections import Counter
from pathlib import Path

run = Path(sys.argv[1]) if len(sys.argv) > 1 else None
if not run or not run.exists():
    raise SystemExit("用法：check_run.py <run_dir>")

log = run / "general_game.log"
print("== 日志", log, "存在" if log.exists() else "不存在")
if log.exists():
    lines = log.read_text(errors="replace").splitlines()
    bad = [l for l in lines if " ERROR " in l or "Traceback" in l or "API Error after" in l]
    print(f"  共 {len(lines)} 行，错误 {len(bad)} 条，最近 5 条：")
    for l in bad[-5:]:
        print("   ", l[:220])

csv_path = run / "llm_responses.csv"
if csv_path.exists():
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    print(f"== llm_responses.csv：{len(rows)} 条调用")
    c = Counter((r["power"], r["response_type"], r["success"]) for r in rows)
    for k in sorted(c):
        print(f"   {k[0]:<8} {k[1]:<28} {k[2]:<8} {c[k]}")
    empty = [r for r in rows if not r["raw_response"].strip()]
    print(f"  空回复 {len(empty)} 条")

save = run / "lmvsgame.json"
if save.exists():
    d = json.loads(save.read_text())
    phases = d.get("phases") or []
    state = (phases[-1].get("state") or {}) if phases else {}
    phase = state.get("phase") or state.get("name") or d.get("phase") or d.get("current_phase")
    print("== 存档阶段", phase)
    centers = state.get("centers") or d.get("centers") or {}
    for p, values in sorted(centers.items()):
        print(f"   {p:<8} 中心 {len(values or []):>2}")
    print("  控制中心总数", sum(len(values or []) for values in centers.values()))
ov = run / "overview.jsonl"
print("== overview.jsonl", "已生成（对局结束）" if ov.exists() else "未生成（对局未结束或中断）")
