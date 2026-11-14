# 自测两条二战机制（不调用大模型）：美国孤立主义、每年世界新闻
# 用法：cd ~/ai-diplomacy-1939 && PYTHONPATH=. .venv/bin/python ~/ai-diplomacy-1939-tools/test_ww2_rules.py
import random
from collections import Counter
from diplomacy import Game
from ai_diplomacy.utils import gather_possible_orders, filter_isolation_orders, us_isolation_active
from ai_diplomacy.prompt_constructor import build_context_prompt
from ai_diplomacy.game_history import GameHistory

PROMPTS = "ai_diplomacy/prompts_1939"


def foreign_targets(g):
    s = set()
    for name, p in g.powers.items():
        if name != "USA":
            s |= {u.split()[-1].split("/")[0] for u in p.units}
            s |= {c.split("/")[0] for c in p.centers}
    return s


def target(o):
    return o.rsplit(" - ", 1)[1].split()[0].split("/")[0] if " - " in o else None


def ctx(g, power):
    return build_context_prompt(g, g.get_state(), power, gather_possible_orders(g, power), GameHistory(), prompts_dir=PROMPTS)


g = Game(map_name="europe1939")
raw = sum(len(g.get_all_possible_orders()[l]) for l in g.get_orderable_locations("USA"))
filt = sum(len(v) for v in gather_possible_orders(g, "USA").values())
print(f"S1939M 美国可选指令：过滤前 {raw}，过滤后 {filt}，孤立生效 {us_isolation_active(g)}")

c = ctx(g, "USA")
assert c.startswith("# 世界新闻（1939）"), c[:60]
assert "# 美国孤立主义" in c
assert "# 美国孤立主义" not in ctx(g, "BRITAIN") and ctx(g, "BRITAIN").startswith("# 世界新闻（1939）")
print("1939 世界新闻和美国孤立说明：通过")

# 随机推进，美国专挑能打到欧洲的方向走，统计被禁止的指令
banned = Counter()
violations = 0
lifted_at = None
for seed in range(20):
    rng = random.Random(seed)
    g = Game(map_name="europe1939")
    while not g.is_game_done and int(g.get_current_phase()[1:5]) <= 1941:
        year = int(g.get_current_phase()[1:5])
        poss = g.get_all_possible_orders()
        if g.get_current_phase().endswith("M"):
            raw_usa = [o for l in g.get_orderable_locations("USA") for o in poss[l]]
            _, forb = filter_isolation_orders(g, "USA", raw_usa)
            banned[year] += len(forb)
            if us_isolation_active(g):
                ft = foreign_targets(g)
                for v in gather_possible_orders(g, "USA").values():
                    violations += sum(1 for o in v if target(o) in ft)
        for p in g.powers:
            opts = gather_possible_orders(g, p) if p == "USA" else {l: poss[l] for l in g.get_orderable_locations(p)}
            orders = []
            for l, v in opts.items():
                if not v:
                    continue
                moves = [o for o in v if " - " in o]
                orders.append(rng.choice(moves if (p == "USA" and moves) else v))
            g.set_orders(p, orders)
        g.process()
    if lifted_at is None:
        lifted_at = g.get_current_phase()
print("被禁止的美国指令数（20 局合计，按年）：", dict(sorted(banned.items())))
assert violations == 0, f"孤立期内可选指令里出现 {violations} 条进攻欧洲的指令"
assert banned[1941] == 0, "1941 年不应再有禁止"
print("孤立期内可选指令无进攻欧洲：通过；1941 年解除：通过")

g = Game(map_name="europe1939")
g.set_current_phase("S1941M") if hasattr(g, "set_current_phase") else None
if g.get_current_phase() == "S1941M":
    c = ctx(g, "USA")
    assert c.startswith("# 世界新闻（1941）") and "# 美国孤立主义" not in c
    print("1941 新闻存在、孤立说明消失：通过")
else:
    print("跳过 1941 上下文检查（引擎不支持直接设阶段）")
print("全部通过")
