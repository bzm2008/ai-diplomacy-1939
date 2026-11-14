# 不依赖引擎，直接读 .map 文本：检查相邻关系是否对称、补给中心数、起始单位是否在本土
import re, sys
from pathlib import Path
path = Path.home() / "ai-diplomacy-1939/diplomacy/maps" / (sys.argv[1] if len(sys.argv) > 1 else "europe1939.map")
abut, kind, homes, units, unowned, victory = {}, {}, {}, [], [], None
power = None
for line in path.read_text().splitlines():
    w = line.split()
    if not w or w[0].startswith("#"):
        continue
    if w[0] in ("WATER", "COAST", "LAND", "SHUT") and len(w) > 2 and w[2] == "ABUTS":
        kind[w[1].upper()] = w[0]
        abut[w[1].upper()] = [x.upper() for x in w[3:]]
    elif w[0] == "VICTORY":
        victory = int(w[1])
    elif w[0] == "UNOWNED":
        unowned = w[1:]
    elif len(w) >= 2 and re.fullmatch(r"\([A-Z]+:[A-Z]\)", w[1]):
        power = w[0]; homes[power] = w[2:]
    elif w[0] in ("A", "F") and len(w) == 2 and power:
        units.append((power, w[0], w[1].upper()))
base = lambda x: x.split("/")[0]
errs = []
for a, ns in abut.items():
    for b in ns:
        if b not in abut and base(b) not in abut:
            errs.append(f"{a} 相邻的 {b} 不存在")
        elif not any(base(x) == base(a) for x in abut.get(b, abut.get(base(b), []))):
            errs.append(f"不对称：{a} → {b}，但 {b} 不连回 {a}")
for p, hs in homes.items():
    for h in hs:
        if h not in abut: errs.append(f"{p} 的本土 {h} 不存在")
for p, t, loc in units:
    if base(loc) not in homes[p]: errs.append(f"{p} 的单位 {t} {loc} 不在本土")
    k = kind.get(loc, kind.get(base(loc)))
    if t == "F" and k not in ("COAST", "WATER"): errs.append(f"舰队 {loc} 不在海岸")
    if t == "A" and k == "WATER": errs.append(f"陆军 {loc} 在海里")
allsc = [h for hs in homes.values() for h in hs] + unowned
if len(allsc) != len(set(allsc)): errs.append("补给中心有重复")
print(f"国家 {len(homes)}，补给中心 {len(allsc)}（本土 {len(allsc) - len(unowned)} + 中立 {len(unowned)}），胜利线 {victory}，过半需要 {len(allsc)//2 + 1}")
for p, hs in homes.items():
    print(f"  {p:<8} {len(hs)} 个本土：{' '.join(hs)}；单位 {sum(1 for u in units if u[0] == p)}")
print("问题：" if errs else "相邻关系对称，单位位置正确。", *errs, sep="\n  ")
